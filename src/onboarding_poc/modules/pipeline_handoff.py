"""Hand-off between Phase 1 and Phase 2: Reckitt's Databricks pipeline.

After Gate A the mapping JSON and SQL sit in ADLS. Reckitt runs its existing pipeline by hand
(POC rule: the app never triggers it), which loads the bronze tables. This step:

  * shows, per published mapping, the SQL file, the bronze table and the submission id;
  * checks Unity Catalog (SQL warehouse) for the rows of that submission: loaded or not yet;
  * lets a person confirm the load, which opens Phase 2 (harmonisation).

pipeline.mode decides how the load is detected:
  manual       a person runs the SQL, checks, and confirms (the original POC rule).
  job          after Gate A the app starts a Databricks job (stand-in for Reckitt's pipeline) on
               one script with every mapping's SQL, watches the run, and confirms on success.
  watch_table  the app starts nothing; it confirms once this submission's rows are in bronze.
With pipeline.auto_phase2 the service then starts harmonisation by itself (up to Gate B).

For local demos, `run_locally` executes the generated SQL on the DuckDB stand-in, playing the
part of Reckitt's pipeline. On Databricks it is refused unless warehouse.allow_pipeline_run.
"""
from __future__ import annotations

import threading

import pandas as pd

from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.mapping.sqlgen import Source, ddl, generate
from onboarding_poc.core.quality.layout import to_logical
from onboarding_poc.session import Session, now


def _filter(table, d: dict, s: Session) -> str:
    lay = table.physical
    if lay:
        sub_col = next((c for c, r in lay.system_columns.items() if r == "submission_id"), None)
        if sub_col:
            return f"`{sub_col}` = '{d['submission_id']}'"
    return f"`load_batch_id` = '{s.session_id}' AND `source_file` = '{s.file(d['file_id']).name}'"


def load_bronze(ctx, s: Session, d: dict) -> pd.DataFrame:
    """Rows this session's file loaded into its bronze table, with logical column names."""
    key = (s.session_id, d["spec"]["mapping_id"], d.get("submission_id"))
    if key in ctx.bronze_cache:
        return ctx.bronze_cache[key]
    table = ctx.contracts(s).tables[d["spec"]["target_table"]]
    fqn = d["published"]["target_fqn"]
    df = ctx.warehouse.query(f"SELECT * FROM {fqn} WHERE {_filter(table, d, s)}")
    df = to_logical(df, table)
    df = df.astype(object).where(df.notna(), None)
    ctx.bronze_cache[key] = df
    return df


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def session_lock(sid: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(sid, threading.Lock())


class PipelineHandoff:
    module = "M8"     # file journey: the step between the two phases

    def __init__(self, ctx, mapping, values):
        self.ctx, self.mapping, self.values = ctx, mapping, values

    # ── job mode ───────────────────────────────────────────────────────────
    def script(self, s: Session) -> str:
        """One script for the job: CREATE TABLE IF NOT EXISTS per target, then each mapping's SQL."""
        parts = [f"-- pipeline script for onboarding session {s.session_id} ({s.retailer} {s.market})"]
        for d in self._published(s):
            spec = MappingSpec.model_validate(d["spec"])
            table = self.ctx.contracts(s).tables[spec.target_table]
            parts.append(ddl(table, d["published"]["target_fqn"]).rstrip(";") + ";")
            parts.append(self.sql(s, spec.mapping_id).strip())
        return "\n\n".join(parts) + "\n"

    def start_job(self, s: Session, user: str) -> dict:
        """Write the run script next to the session outputs and start the pipeline job on it."""
        prev = s.outputs.get("pipeline_run") or {}
        if prev.get("state") in ("pending", "running"):
            raise ValueError(f"a pipeline run is already in progress (run {prev.get('run_id')})")
        attempt = int(prev.get("attempt", 0)) + 1
        key = f"{self.ctx.cfg.paths.outputs}/{s.session_id}/pipeline/run-{attempt}.sql"
        self.ctx.storage.write_text(key, self.script(s))
        params = {"session_id": s.session_id, "sql_path": self.ctx.storage.uri(key)}
        work = (lambda: self.run_locally(s, "pipeline-job")) if self.ctx.jobs.kind == "local" else None
        info = self.ctx.jobs.start(params, work)
        info.update(attempt=attempt, sql_key=key, sql_path=params["sql_path"], started_by=user, runner=self.ctx.jobs.kind)
        s.outputs["pipeline_run"] = info
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "pipeline.job_started", user, subject=info["run_id"],
                             after={k: info.get(k) for k in ("run_id", "job_id", "state", "url", "sql_path", "attempt")},
                             why="Gate A approved: the pipeline job loads bronze from the generated SQL")
        return info

    def refresh_run(self, s: Session) -> dict | None:
        """Latest state of the pipeline run (from Databricks unless already final)."""
        run = s.outputs.get("pipeline_run")
        if not run or run.get("state") in ("success", "failed"):
            return run
        try:
            new = self.ctx.jobs.status(run["run_id"])
        except Exception as exc:
            run["poll_error"] = str(exc)[:300]
            return run
        changed = new.get("state") != run.get("state")
        run.update({k: v for k, v in new.items() if v is not None or k == "message"})
        run.pop("poll_error", None)
        s.outputs["pipeline_run"] = run
        self.ctx.store.save(s)
        if changed and run["state"] in ("success", "failed"):
            self.ctx.bronze_cache.clear()
            self.ctx.store.trace(s, self.module, f"pipeline.job_{run['state']}", "pipeline-job", subject=run["run_id"],
                                 after={k: run.get(k) for k in ("state", "result", "message", "url")},
                                 why="pipeline job finished")
        return run

    def _published(self, s: Session) -> list[dict]:
        docs = [d for d in self.mapping.all(s) if d.get("published")]
        if not docs:
            raise ValueError("nothing published yet: approve the mappings (Gate A) first")
        return docs

    def status(self, s: Session, check: bool = True) -> dict:
        items = []
        for d in self._published(s):
            table = self.ctx.contracts(s).tables[d["spec"]["target_table"]]
            fqn = d["published"]["target_fqn"]
            loaded, error = None, None
            if check:
                try:
                    if self.ctx.warehouse.table_exists(fqn):
                        r = self.ctx.warehouse.query(f"SELECT count(*) AS n FROM {fqn} WHERE {_filter(table, d, s)}")
                        loaded = int(r.iloc[0, 0])
                    else:
                        loaded = 0
                        error = "table not found in Unity Catalog yet"
                except Exception as exc:   # no access, warehouse stopped
                    error = str(exc)[:300]
            items.append({"mapping_id": d["spec"]["mapping_id"], "file": d["file_name"], "sheet": d["sheet"],
                          "target_table": table.name, "target_fqn": fqn, "submission_id": d["submission_id"],
                          "sql_uri": d["published"]["uris"]["sql"], "sql_key": d["published"]["keys"]["sql"],
                          "loaded_rows": loaded, "loaded": bool(loaded), "error": error,
                          "warnings": d["published"]["warnings"]})
        return {"items": items, "all_loaded": all(i["loaded"] for i in items),
                "mode": self.ctx.cfg.pipeline.mode, "auto_phase2": self.ctx.cfg.pipeline.auto_phase2,
                "poll_seconds": self.ctx.cfg.pipeline.poll_seconds, "run": s.outputs.get("pipeline_run"),
                "warehouse": self.ctx.warehouse.describe() if check else None,
                "can_run_locally": self.ctx.warehouse.dialect == "duckdb" or self.ctx.cfg.warehouse.allow_pipeline_run,
                "stage": s.stage}

    def sql(self, s: Session, mapping_id: str) -> str:
        d = self.mapping.load(s, mapping_id)
        return self.ctx.storage.read_text(d["published"]["keys"]["sql"])

    def run_locally(self, s: Session, user: str) -> dict:
        wh = self.ctx.warehouse
        if wh.dialect != "duckdb" and not self.ctx.cfg.warehouse.allow_pipeline_run:
            raise ValueError("the app does not run Reckitt's pipeline in this environment; run the SQL files in Databricks "
                             "(set warehouse.allow_pipeline_run to simulate it)")
        out = []
        decimals = self.values.decimals(s)
        for d in self._published(s):
            spec = MappingSpec.model_validate(d["spec"])
            table = self.ctx.contracts(s).tables[spec.target_table]
            fqn = d["published"]["target_fqn"]
            f = s.file(d["file_id"])
            if wh.dialect == "duckdb":
                frame = self.ctx.frame(s, d["file_id"], d["sheet"]).astype(object)
                wh.register("__src", frame)
                b = generate(spec, table, target_fqn=fqn, source=Source(mode="table", table="__src"),
                             submission_id=d["submission_id"], batch_id=s.session_id, retailer=s.retailer,
                             source_file=f.name, decimals=decimals.get(spec.mapping_id, {}))
                wh.execute(ddl(table, fqn))
                if b.delete_sql:
                    wh.execute(b.delete_sql)
                wh.execute(f"INSERT INTO {fqn} ({', '.join('`' + c + '`' for c in b.columns)})\n{b.select_sql}")
            else:
                from onboarding_poc.warehouse import split_script
                wh.execute(ddl(table, fqn))                  # simulation: create the bronze table if missing
                for st in split_script(self.sql(s, spec.mapping_id)):
                    wh.execute(st)
            out.append({"target_fqn": fqn, "file": f.name})
        self.ctx.bronze_cache.clear()
        self.ctx.store.trace(s, self.module, "pipeline.run_locally", user, after=out,
                             why="local stand-in for Reckitt's pipeline (development only)")
        return self.status(s)

    def confirm(self, s: Session, user: str, note: str | None = None) -> dict:
        st = self.status(s)
        missing = [f"{i['file']} -> {i['target_fqn']}" for i in st["items"] if not i["loaded"]]
        if missing:
            raise ValueError(f"no rows found yet for: {missing}. Run the pipeline on the SQL files, then check again")
        self.ctx.bronze_cache.clear()
        s.stage = "harmonisation"
        s.outputs["pipeline"] = {"confirmed_by": user, "confirmed_at": now(), "note": note,
                                 "tables": {i["target_fqn"]: i["loaded_rows"] for i in st["items"]}}
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "pipeline.loaded_confirmed", user, why=note or "pipeline run checked",
                             after={i["target_fqn"]: i["loaded_rows"] for i in st["items"]})
        return st

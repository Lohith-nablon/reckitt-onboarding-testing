"""M7 Output and sign-off (Gate C, end of Phase 2).

Sign-off is refused while Gate A or B is open, the pipeline load is unconfirmed, validation has
not run since the last decision, or any blocking check fails. On sign-off:

  silver (Unity Catalog, through the SQL warehouse)
    <silver table per target>        harmonised rows: bronze columns + RB codes + master attributes
    <crosswalk table>                retailer code -> RB code decisions of this session
  storage (ADLS) outputs/<session>/
    discovery/<file>.json|.md        discovery report
    validation/<table>__<file>.json  checks and Veritas compare
    trace.json, manifest.json        every decision, and what was written where

Then M8 learns: the signed-off mappings go to the mapping library (the mapping JSON and SQL
were already published to ADLS at Gate A).
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.session import Session, now


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


class OutputSignoff:
    module = "M7"

    def __init__(self, ctx, discovery, mapping, validate, codes, library):
        self.ctx, self.discovery, self.mapping, self.validate = ctx, discovery, mapping, validate
        self.codes, self.library = codes, library

    def readiness(self, s: Session) -> dict:
        problems = []
        for g, label in (("A", "mapping approval"), ("B", "harmonisation review")):
            if s.gates[g].status != "approved":
                problems.append(f"Gate {g} ({label}) is {s.gates[g].status.replace('_', ' ')}")
        if not s.outputs.get("pipeline"):
            problems.append("the pipeline load has not been confirmed")
        reps = {r["mapping_id"]: r for r in self.validate.reports(s)}
        last_b = s.gates["B"].decided_at or ""
        for d in self.mapping.all(s):
            r = reps.get(d["spec"]["mapping_id"])
            tag = f"{d['file_name']} -> {d['spec']['target_table']}"
            if r is None:
                problems.append(f"{tag}: not validated yet")
            elif r.get("generated_at", "") < last_b:
                problems.append(f"{tag}: harmonisation changed after validation; validate again")
            elif r["blocking"]:
                problems.append(f"{tag}: blocking checks {r['blocking']}")
        return {"ready": not problems, "problems": problems,
                "rows": sum(r["rows"] for r in reps.values()),
                "targets": sorted({r["silver_fqn"] for r in reps.values()}),
                "crosswalk_table": self.ctx.cfg.uc.crosswalk()}

    def signoff(self, s: Session, user: str, note: str | None = None) -> dict:
        r = self.readiness(s)
        if not r["ready"]:
            raise ValueError("not ready for sign-off: " + "; ".join(r["problems"]))
        st, wh, base = self.ctx.storage, self.ctx.warehouse, f"{self.ctx.cfg.paths.outputs}/{s.session_id}"
        written, learned = [], []
        frames = self.validate.frames(s)
        for d, table, bronze in self.validate.loaded(s):
            out, _ = self.validate.build(s, d, table, bronze, frames)
            out["harmonised_by"] = user
            out["harmonised_at"] = now()
            fqn = self.ctx.cfg.uc.silver(table.name)
            n = wh.write_frame(out, fqn, delete_where=f"`onboarding_session_id` = '{s.session_id}' "
                                                      f"AND `source_file` = '{d['file_name'].replace(chr(39), '')}'")
            written.append({"kind": "silver", "table": table.name, "fqn": fqn, "rows": n, "file": d["file_name"]})
            spec = MappingSpec.model_validate(d["spec"])
            lib = self.library.save(spec, d.get("veritas"), s, d["file_name"], user)
            learned.append({"target_table": spec.target_table, "file": d["file_name"], **lib})
            self.ctx.store.trace(s, "M8", "library.mapping_saved", user, subject=f"{d['file_id']}:{spec.target_table}",
                                 after=lib, why="signed off: next file with this layout skips discovery and mapping")
        xw = [{"market": s.market, "retailer": s.retailer, "entity": c["entity"], "retailer_code": c["source_key"],
               "retailer_label": c["source_label"], "rb_code": c["final_code"], "confidence": c["confidence"],
               "method": c["method"], "decision": c["decision"], "decided_by": c["decided_by"],
               "onboarding_session_id": s.session_id, "signed_off_by": user, "signed_off_at": now()}
              for c in self.codes.load(s) if c.get("decision")]
        if xw:
            fqn = self.ctx.cfg.uc.crosswalk()
            n = wh.write_frame(pd.DataFrame(xw), fqn, delete_where=f"`onboarding_session_id` = '{s.session_id}'")
            written.append({"kind": "crosswalk", "fqn": fqn, "rows": n})
        for f in s.files:
            rep = self.discovery.load(s, f.file_id)["report"]
            k = f"{base}/discovery/{_slug(Path(f.name).stem)}.json"
            st.write_json(k, {kk: vv for kk, vv in rep.items() if kk != "markdown"})
            st.write_text(f"{base}/discovery/{_slug(Path(f.name).stem)}.md", rep.get("markdown") or "", "text/markdown")
            written.append({"kind": "discovery_report", "key": k, "uri": st.uri(k)})
            f.status = "signed_off"
        for v in self.validate.reports(s):
            k = f"{base}/validation/{_slug(v['target_table'])}__{_slug(Path(v['file_name']).stem)}.json"
            st.write_json(k, {kk: vv for kk, vv in v.items() if kk != "preview"})
            written.append({"kind": "validation", "key": k, "uri": st.uri(k)})
        s.gates["C"] = s.gates["C"].model_copy(update={"status": "approved", "decided_by": user, "decided_at": now(),
                                                       "note": note})
        s.status, s.stage = "signed_off", "done"
        manifest = {"session_id": s.session_id, "market": s.market, "retailer": s.retailer, "signed_off_by": user,
                    "signed_off_at": now(), "note": note, "files": [f.model_dump() for f in s.files],
                    "gates": {k: g.model_dump() for k, g in s.gates.items()}, "pipeline": s.outputs.get("pipeline"),
                    "mapping_output": f"{base}/mapping/manifest.json", "outputs": written, "library": learned,
                    "llm": self.ctx.llm.label}
        st.write_json(f"{base}/manifest.json", manifest)
        s.outputs.update({"base": st.uri(base), "items": written, "library": learned})
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "gate_c.signed_off", user, why=note or "harmonised output signed off",
                             after={"written": [w.get("fqn") or w.get("uri") for w in written], "rows": r["rows"]})
        st.write_json(f"{base}/trace.json", self.ctx.store.events(s.session_id))
        return manifest

    def outputs(self, s: Session) -> list[dict]:
        base = f"{self.ctx.cfg.paths.outputs}/{s.session_id}/"
        return [{"key": k, "name": k[len(base):], "uri": self.ctx.storage.uri(k)} for k in self.ctx.storage.list(base)]

    def silver_preview(self, s: Session, rows: int = 50) -> list[dict]:
        out = []
        for w in (s.outputs.get("items") or []):
            if w["kind"] in ("silver", "crosswalk"):
                df = self.ctx.warehouse.query(f"SELECT * FROM {w['fqn']} WHERE `onboarding_session_id` = '{s.session_id}' LIMIT {rows}")
                from onboarding_poc.session import rows_jsonable
                out.append({"fqn": w["fqn"], "kind": w["kind"], "columns": list(df.columns), "rows": rows_jsonable(df)})
        return out

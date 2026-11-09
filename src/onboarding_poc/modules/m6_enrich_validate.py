"""M6 Enrich and validate (Phase 2): build the silver rows from the loaded bronze rows and prove
they are right before sign-off.

Per loaded table (read from Unity Catalog):
  1. value standards (M5) applied: EA/Each/Unit -> EA, £ -> GBP, Yes/1 -> Y
  2. RB product and outlet codes from code harmonisation (M4): the table's own harmonised
     columns when it has them, otherwise rb_product_code / rb_outlet_code added
  3. master attributes joined from MDM on the RB code
  4. checks on the bronze contract (mandatory, keys, types, ranges) plus RB-code coverage and
     existence in MDM; errors block sign-off, warnings are shown
The Veritas compare (POC KPI) is computed when the mapping is published and shown here too.
Nothing is written to silver until sign-off (M7).
"""
from __future__ import annotations

import json

import pandas as pd

from onboarding_poc.core.quality.checks import CheckResult, blocking, run_checks
from onboarding_poc.core.text import canon_header
from onboarding_poc.session import Session, now, rows_jsonable


def _norm_op(op: dict | None) -> object:
    """Operator tree without ids/positions, column names compared case-insensitively."""
    if not op:
        return None
    det = dict(op.get("Details") or {})
    if "ColumnName" in det:
        det = {"ColumnName": canon_header(det["ColumnName"])}
    det.pop("ColumnType", None)
    return (op.get("Name"), json.dumps(det, sort_keys=True), tuple(_norm_op(i) for i in (op.get("Inputs") or [])))


def _describe_op(op: dict | None) -> str:
    if not op:
        return "(unmapped)"
    name, det = op.get("Name"), op.get("Details") or {}
    if name == "DirectColumn":
        return f"[{det.get('ColumnName')}]"
    if name == "DirectText":
        return f"'{det.get('Text')}'"
    inner = ", ".join(_describe_op(i) for i in (op.get("Inputs") or []))
    extra = ", ".join(f"{k}={v}" for k, v in det.items() if k not in ("ColumnType",))
    return f"{name}({inner}{'; ' + extra if extra else ''})"


def compare_veritas(ours: dict, ref: dict) -> dict:
    a = {m["DatasetColumn"].lower(): m.get("Transformations") for m in ours.get("Mappings", [])}
    b = {m["DatasetColumn"].lower(): m.get("Transformations") for m in ref.get("Mappings", [])}
    cols = sorted(set(a) | set(b))
    rows, same = [], 0
    for c in cols:
        eq = _norm_op(a.get(c)) == _norm_op(b.get(c))
        same += eq
        rows.append({"column": c, "match": eq, "ours": _describe_op(a.get(c)), "veritas": _describe_op(b.get(c))})
    ft_a, ft_b = ours.get("FileType", {}), ref.get("FileType", {})
    ft = [{"field": k, "ours": ft_a.get(k), "veritas": ft_b.get(k), "match": ft_a.get(k) == ft_b.get(k)}
          for k in ("Format", "Delimiter", "SkipFirstRows", "SkipLastRows", "HasHeader")]
    return {"reference_id": ref.get("id"), "reference_name": ref.get("Name"), "columns_compared": len(cols),
            "columns_matching": same, "match_rate": round(same / len(cols), 3) if cols else 0.0,
            "columns": rows, "file_type": ft}


def pick_reference(ctx, s: Session, doc: dict, refs: list[dict]) -> dict | None:
    """The existing Veritas mapping for this retailer and dataset that is closest to ours."""
    display = ctx.markets.get(s.market).display_name(s.retailer).upper().replace(" ", "")
    cands = []
    for r in refs:
        if r.get("Dataset") != doc.get("Dataset"):
            continue
        rr = str((r.get("Metadata") or {}).get("RetailerName") or "").upper().replace(" ", "")
        if rr in (display, s.retailer.upper()):
            cands.append((compare_veritas(doc, r)["match_rate"], r))
    return max(cands, key=lambda x: x[0])[1] if cands else None


HARM = {"product": ("rb_product_code", "rb_product_match_confidence", "rb_product_method"),
        "outlet": ("rb_outlet_code", "rb_outlet_match_confidence", "rb_outlet_method")}
KEY_SEM = {"product": "retailer_sku_code", "outlet": "retailer_outlet_code"}
MASTER = {"product": "erp_product", "outlet": "rb_customer"}


class EnrichValidate:
    module = "M6"

    def __init__(self, ctx, mapping, codes, values, standards, library):
        self.ctx, self.mapping, self.codes, self.values = ctx, mapping, codes, values
        self.standards, self.library = standards, library

    def loaded(self, s: Session) -> list[tuple[dict, object, pd.DataFrame]]:
        from onboarding_poc.modules.pipeline_handoff import load_bronze
        contracts = self.ctx.contracts(s)
        return [(d, contracts.tables[d["spec"]["target_table"]], load_bronze(self.ctx, s, d))
                for d in self.mapping.all(s) if d.get("published")]

    def frames(self, s: Session) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for d, t, df in self.loaded(s):
            out[t.name] = pd.concat([out[t.name], df], ignore_index=True) if t.name in out else df
        return out

    def build(self, s: Session, d: dict, table, bronze: pd.DataFrame, frames: dict) -> tuple[pd.DataFrame, list]:
        contracts = self.ctx.contracts(s)
        acc = self.codes.accepted_codes(s)
        detail = {(r["entity"], r["source_key"]): r for r in self.codes.load(s)}
        mdm = self.ctx.mdm.frames()
        out = self.standards.apply(s, table.name, bronze, frames)
        keep = [c.name for c in table.columns if c.name in out.columns]
        out = out[keep].copy()
        extra_checks: list[CheckResult] = []
        for ent in ("product", "outlet"):
            key = next((c.name for c in table.columns if c.semantic == KEY_SEM[ent]), None)
            if not key or key not in out.columns:
                continue
            own = [c for c in table.columns if c.harmonised == ent]
            code_col = next((c.name for c in own if c.dtype == "string"), HARM[ent][0])
            conf_col = next((c.name for c in own if c.dtype != "string"), HARM[ent][1])
            codes = acc[ent]
            out[code_col] = out[key].map(lambda v: codes.get(str(v), (None, None))[0] if v is not None else None)
            out[conf_col] = out[key].map(lambda v: codes.get(str(v), (None, None))[1] if v is not None else None)
            if not own:
                out[HARM[ent][2]] = out[key].map(lambda v: (detail.get((ent, str(v))) or {}).get("decision") if v is not None else None)
            # master attributes: the table's enrich columns
            m = MASTER[ent]
            mdf, mkey = mdm.get(m), contracts.masters[m].key
            for c in table.columns:
                if c.enrich and c.enrich.master == m and mdf is not None and len(mdf) and c.enrich.column in mdf.columns:
                    lut = mdf.drop_duplicates(mkey).set_index(mkey)[c.enrich.column]
                    out[c.name] = out[code_col].map(lambda v: lut.get(v) if v is not None else None)
            n = len(out)
            missing = out[code_col].isna()
            share = 1 - (missing.mean() if n else 1)
            extra_checks.append(CheckResult(check=f"harmonised:{code_col}", template="completeness", column=code_col,
                                            severity="warning", passed=share >= 0.95, failed_rows=int(missing.sum()),
                                            total_rows=n, detail=f"{share:.1%} of rows carry an RB code"))
            if mdf is not None and len(mdf):
                known = set(mdf[mkey].dropna())
                bad = out[code_col].notna() & ~out[code_col].astype(str).isin(known)
                extra_checks.append(CheckResult(check=f"in_master:{code_col}", template="referential_integrity",
                                                column=code_col, severity="error", passed=not bad.any(),
                                                failed_rows=int(bad.sum()), total_rows=n))
        base = run_checks(bronze[[c.name for c in table.columns if c.name in bronze.columns]].copy(), table)
        base = [c for c in base if not c.check.startswith(("harmonised:", "in_master:"))]
        out["onboarding_session_id"] = s.session_id
        out["submission_id"] = d.get("submission_id")
        out["source_file"] = d["file_name"]
        return out, base + extra_checks

    def run(self, s: Session, user: str) -> list[dict]:
        if s.gates["B"].status != "approved":
            raise ValueError("Gate B (harmonisation review) must be approved first")
        n = self.ctx.cfg.output.preview_rows
        loaded = self.loaded(s)
        frames = self.frames(s)
        reps = []
        for d, table, bronze in loaded:
            out, checks = self.build(s, d, table, bronze, frames)
            block = blocking(checks)
            prev = out.head(n)
            rep = {"mapping_id": d["spec"]["mapping_id"], "file_name": d["file_name"], "sheet": d["sheet"],
                   "target_table": table.name, "bronze_fqn": d["published"]["target_fqn"],
                   "silver_fqn": self.ctx.cfg.uc.silver(table.name), "rows": len(out), "bronze_rows": len(bronze),
                   "generated_at": now(), "checks": [c.model_dump() for c in checks],
                   "blocking": [c.check for c in block],
                   "warnings": [c.check for c in checks if not c.passed and c.severity == "warning"],
                   "veritas_compare": d.get("veritas_compare"),
                   "kpi_veritas_match": (d["veritas_compare"]["match_rate"] >= self.ctx.cfg.thresholds.veritas_match_target)
                   if d.get("veritas_compare") else None,
                   "preview": {"columns": list(prev.columns), "rows": rows_jsonable(prev)}}
            self.ctx.store.put(s.session_id, f"validation/{d['spec']['mapping_id']}.json", rep)
            self.ctx.store.trace(s, self.module, "validation.completed", "system", subject=f"{d['file_id']}:{table.name}",
                                 source=f"{rep['bronze_fqn']} ({len(bronze)} rows)",
                                 after={"rows": len(out), "blocking": rep["blocking"], "warnings": rep["warnings"]},
                                 why="silver rows built from the loaded bronze rows, codes and value standards")
            reps.append(rep)
        s.stage = "validate"
        for f in s.files:
            f.status = "validated"
        if s.gates["C"].status == "not_started":
            s.gates["C"].status = "open"
        self.ctx.store.save(s)
        return reps

    def reports(self, s: Session) -> list[dict]:
        return [r for r in (self.ctx.store.get(s.session_id, f"validation/{m}.json") for m in s.mapping_ids) if r]

"""M6 Enrich and validate: build the bronze-ready rows and prove they are right before sign-off.

Per approved mapping:
  1. value rules (M5) applied to the source, then the mapping (M3)
  2. RB codes from code harmonisation (M4), then master attributes joined from MDM
  3. checks: mandatory columns, data types, primary key, negatives, date window, RB-code
     coverage and existence in MDM (errors block sign-off, warnings are shown)
  4. Veritas compare (POC KPI): the generated Veritas JSON against the existing Veritas mapping
     for this retailer and dataset, column by column (same operator, same inputs, same values)

Nothing is written to bronze here; the result is a preview plus the validation report.
"""
from __future__ import annotations

import json

import pandas as pd

from onboarding_poc.core.mapping.apply import MissingSourceColumn, apply_mapping
from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.quality.checks import blocking, run_checks
from onboarding_poc.core.quality.enrich import fill_lookups
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


class EnrichValidate:
    module = "M6"

    def __init__(self, ctx, mapping, codes, values, library):
        self.ctx, self.mapping, self.codes, self.values, self.library = ctx, mapping, codes, values, library

    def build(self, s: Session, d: dict) -> tuple[pd.DataFrame, list, MappingSpec]:
        contracts = self.ctx.contracts(s)
        spec = MappingSpec.model_validate(d["spec"])
        table = contracts.tables[spec.target_table]
        src = self.values.prepare_source(s, spec.mapping_id, self.ctx.frame(s, d["file_id"], d["sheet"]))
        tgt = apply_mapping(src, spec, table)
        errors = tgt.attrs.get("coercion_errors", {})
        acc = self.codes.accepted_codes(s)
        mdm = self.ctx.mdm.frames()
        tgt = fill_lookups(tgt, src, spec, table, acc["product"], acc["outlet"], mdm, contracts)
        tgt.attrs["coercion_errors"] = errors
        if table.kind == "dimension" and table.pk:
            tgt = tgt.drop_duplicates(subset=table.pk, keep="first")
        master_codes = {}
        for ent, m in (("product", "erp_product"), ("outlet", "rb_customer")):
            if len(mdm.get(m, [])):
                master_codes[ent] = set(mdm[m][contracts.masters[m].key].dropna())
        return tgt, run_checks(tgt, table, master_codes), spec

    def _reference(self, s: Session, doc: dict | None) -> dict | None:
        if not doc:
            return None
        display = self.ctx.markets.get(s.market).display_name(s.retailer).upper().replace(" ", "")
        cands = []
        for r in self.library.reference_docs():
            if r.get("Dataset") != doc.get("Dataset"):
                continue
            rr = str((r.get("Metadata") or {}).get("RetailerName") or "").upper().replace(" ", "")
            if rr not in (display, s.retailer.upper()):
                continue
            score = compare_veritas(doc, r)["match_rate"]
            cands.append((score, r))
        return max(cands, key=lambda x: x[0])[1] if cands else None

    def run(self, s: Session, user: str) -> list[dict]:
        if s.gates["A"].status != "approved":
            raise ValueError("Gate A must be approved first")
        if s.gates["B"].status != "approved":
            raise ValueError("Gate B (harmonisation review) must be approved first")
        n = self.ctx.cfg.output.preview_rows
        out = []
        for d in self.mapping.all(s):
            try:
                tgt, checks, spec = self.build(s, d)
            except MissingSourceColumn as exc:
                rep = {"mapping_id": d["spec"]["mapping_id"], "file_name": d["file_name"], "sheet": d["sheet"],
                       "target_table": d["spec"]["target_table"], "rows": 0, "error": str(exc), "checks": [],
                       "blocking": ["missing_source_column"], "veritas_compare": None, "preview": {"columns": [], "rows": []}}
                out.append(rep)
                continue
            ref = self._reference(s, d.get("veritas"))
            cmp = compare_veritas(d["veritas"], ref) if ref and d.get("veritas") else None
            block = blocking(checks)
            prev = tgt.head(n)
            rep = {"mapping_id": spec.mapping_id, "file_name": d["file_name"], "sheet": d["sheet"],
                   "target_table": spec.target_table, "rows": len(tgt), "generated_at": now(),
                   "checks": [c.model_dump() for c in checks], "blocking": [c.check for c in block],
                   "warnings": [c.check for c in checks if not c.passed and c.severity == "warning"],
                   "veritas_compare": cmp,
                   "kpi_veritas_match": (cmp["match_rate"] >= self.ctx.cfg.thresholds.veritas_match_target) if cmp else None,
                   "preview": {"columns": list(prev.columns),
                               "rows": rows_jsonable(prev)}}
            self.ctx.store.put(s.session_id, f"validation/{spec.mapping_id}.json", rep)
            self.ctx.store.trace(s, self.module, "validation.completed", "system", subject=f"{d['file_id']}:{spec.target_table}",
                                 source=f"{d['file_name']} [{d['sheet'] or 'data'}] mapping v{spec.version}",
                                 after={"rows": len(tgt), "blocking": rep["blocking"], "warnings": rep["warnings"],
                                        "veritas_match": cmp["match_rate"] if cmp else None},
                                 why=f"compared with Veritas #{ref.get('id')}" if ref else "no Veritas reference for this retailer")
            out.append(rep)
        s.stage = "validate"
        for f in s.files:
            f.status = "validated"
        if s.gates["C"].status in ("not_started",):
            s.gates["C"].status = "open"
        self.ctx.store.save(s)
        return out

    def reports(self, s: Session) -> list[dict]:
        return [r for r in (self.ctx.store.get(s.session_id, f"validation/{m}.json") for m in s.mapping_ids) if r]

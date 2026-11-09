"""M5 Value harmonisation: make values mean the same thing in every retailer file (Gate B, with M4).

Rules are derived from the approved mappings and the file itself, shown with before/after
examples, and approved or changed by a person:

  date_format   source date pattern -> target pattern (e.g. dd-MM-yyyy -> yyyyMMdd)
  number_format decimal separator, thousands separators, currency symbols, (negatives)
  code_suffix   retailer code + suffix convention (e.g. 12345 -> 12345_Tesco)
  currency      currency written vs currency found in headers / market default
  unit          unit of measure written vs units seen in the file

Scope note: Jonathan's second harmonisation type (column/value harmonisation beyond these)
is still to be confirmed with Reckitt; new rule kinds plug in here.

Changing a date format or suffix edits the mapping link (M3) so the Veritas JSON stays the
single source of truth; number rules are applied to the source before the mapping runs.
"""
from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.mapping.veritas import java_to_strftime, strftime_to_java
from onboarding_poc.core.text import canon_header
from onboarding_poc.session import Session, now

_SYM = re.compile(r"[£$€₹¥\s ]|(?i:gbp|eur|usd|inr)")


def normalise_number(s: pd.Series, decimal: str = ".") -> pd.Series:
    def one(v):
        if v is None or v != v:
            return None
        x = _SYM.sub("", str(v))
        neg = x.startswith("(") and x.endswith(")")
        x = x.strip("()")
        if x.endswith("-"):
            neg, x = True, x[:-1]
        if decimal == ",":
            x = x.replace(".", "").replace(",", ".")
        else:
            x = x.replace(",", "")
        try:
            f = float(x)
        except ValueError:
            return None
        return -f if neg else f
    return s.map(one)


def detect_decimal(values: list[str]) -> tuple[str, float]:
    """',' when values look like 1.234,56 or 12,5; '.' otherwise. Returns (sep, share of evidence)."""
    comma = dot = 0
    for v in values[:500]:
        v = str(v).strip()
        if re.search(r",\d{1,2}$", v) and not re.search(r"\.\d{1,2}$", v):
            comma += 1
        elif re.search(r"\.\d+$", v):
            dot += 1
    total = comma + dot
    if not total:
        return ".", 1.0
    return ("," if comma > dot else "."), max(comma, dot) / total


class ValueHarmonisation:
    module = "M5"

    def __init__(self, ctx, mapping, discovery):
        self.ctx, self.mapping, self.discovery = ctx, mapping, discovery

    def _decisions(self, s: Session) -> dict:
        return self.ctx.store.get(s.session_id, "harmonisation/value_rules.json", {})

    def rules(self, s: Session) -> list[dict]:
        decisions = self._decisions(s)
        contracts = self.ctx.contracts(s)
        out = []
        for d in self.mapping.all(s):
            spec = MappingSpec.model_validate(d["spec"])
            table = contracts.tables[spec.target_table]
            frame = self.ctx.frame(s, d["file_id"], d["sheet"])
            cols = {canon_header(c): c for c in frame.columns}
            disc = next((x for x in self.discovery.load(s, d["file_id"])["report"]["sheets"] if x["sheet"] == d["sheet"]), {})
            for c in spec.columns:
                tc = table.column(c.target_column) if table.has(c.target_column) else None
                base = {"mapping_id": spec.mapping_id, "file_name": d["file_name"], "sheet": d["sheet"],
                        "target_table": spec.target_table, "target_column": c.target_column}
                if c.action == "DATE" and c.source_columns:
                    src = cols.get(canon_header(c.source_columns[0]))
                    vals = frame[src].dropna().astype(str).head(200).tolist() if src else []
                    fmt_in = c.date_format
                    fmt_out = java_to_strftime(c.target_date_format) if c.target_date_format else "%Y-%m-%d"
                    ok, ex = 0, []
                    for v in vals:
                        try:
                            dt = datetime.strptime(v.split("T")[0].strip(), fmt_in) if fmt_in else None
                        except ValueError:
                            dt = None
                        ok += dt is not None
                        if len(ex) < 4 and v not in [e["before"] for e in ex]:
                            ex.append({"before": v, "after": dt.strftime(fmt_out) if dt else "(not a date)"})
                    rate = ok / len(vals) if vals else 0.0
                    out.append({**base, "rule_id": f"{spec.mapping_id}:{c.target_column}:date_format", "kind": "date_format",
                                "source_column": src, "from": strftime_to_java(fmt_in) if fmt_in else None,
                                "to": c.target_date_format or "date", "parse_rate": round(rate, 3), "examples": ex,
                                "issue": None if rate >= 0.98 else f"only {rate:.0%} of values parse with this format",
                                "editable": "from"})
                elif c.action == "DIRECT_COLUMN" and tc and tc.dtype in ("decimal", "integer") and c.source_columns:
                    src = cols.get(canon_header(c.source_columns[0]))
                    if not src:
                        continue
                    vals = frame[src].dropna().astype(str).tolist()
                    sep, ev = detect_decimal(vals)
                    has_sym = any(_SYM.search(v) for v in vals[:200])
                    rid = f"{spec.mapping_id}:{c.target_column}:number_format"
                    chosen = (decisions.get(rid) or {}).get("value") or sep
                    conv = normalise_number(pd.Series(vals[:200]), chosen)
                    fail = float(conv.isna().mean()) if len(conv) else 0.0
                    ex = [{"before": b, "after": "" if a is None else f"{a:g}"} for b, a in
                          list(dict.fromkeys(zip(vals[:200], conv.tolist())))[:4]]
                    issue = None
                    if fail > 0.02:
                        issue = f"{fail:.0%} of values are not numbers"
                    elif sep == "," or has_sym or ev < 0.9:
                        issue = "values need conversion: confirm the decimal separator"
                    out.append({**base, "rule_id": rid, "kind": "number_format", "source_column": src,
                                "from": f"decimal '{sep}'" + (", symbols" if has_sym else ""), "to": "decimal '.'",
                                "value": chosen, "parse_rate": round(1 - fail, 3), "examples": ex, "issue": issue,
                                "editable": "value"})
                elif c.action == "CONCATENATION" and c.parts and c.parts[-1].text and c.parts[0].column:
                    src = cols.get(canon_header(c.parts[0].column))
                    vals = frame[src].dropna().astype(str).drop_duplicates().head(4).tolist() if src else []
                    out.append({**base, "rule_id": f"{spec.mapping_id}:{c.target_column}:code_suffix", "kind": "code_suffix",
                                "source_column": src, "from": "retailer code", "to": f"code + '{c.parts[-1].text}'",
                                "value": c.parts[-1].text, "examples": [{"before": v, "after": v + c.parts[-1].text} for v in vals],
                                "issue": None if c.confidence >= self.ctx.cfg.thresholds.mapping_review else c.rationale,
                                "editable": "value"})
                elif c.action == "DIRECT_TEXT" and tc and tc.context in ("currency", "currency_code"):
                    found = (disc.get("table") or {}).get("currency")
                    issue = f"file says {found}, mapping writes {c.value}" if found and c.value and found != c.value else None
                    out.append({**base, "rule_id": f"{spec.mapping_id}:{c.target_column}:currency", "kind": "currency",
                                "source_column": None, "from": found or "not stated", "to": c.value, "value": c.value,
                                "examples": [], "issue": issue, "editable": None})
                elif c.action == "DIRECT_TEXT" and tc and tc.context == "uom":
                    seen = sorted({u for x in (disc.get("columns") or []) if x.get("unit") for u in [x["unit"]]})
                    issue = f"file shows units {seen}, mapping writes {c.value}" if seen and c.value not in seen else None
                    out.append({**base, "rule_id": f"{spec.mapping_id}:{c.target_column}:unit", "kind": "unit",
                                "source_column": None, "from": ", ".join(seen) or "not stated", "to": c.value,
                                "value": c.value, "examples": [], "issue": issue, "editable": None})
        for r in out:
            dec = decisions.get(r["rule_id"]) or {}
            r["status"] = dec.get("status", "proposed")
            r["decided_by"], r["decided_at"] = dec.get("by"), dec.get("at")
        return out

    def decide(self, s: Session, rule_id: str, user: str, approve: bool = True, value: str | None = None,
               note: str | None = None) -> dict:
        rule = next((r for r in self.rules(s) if r["rule_id"] == rule_id), None)
        if rule is None:
            raise KeyError(f"rule {rule_id} not found")
        decisions = self._decisions(s)
        status = "approved"
        if value is not None and value != (rule.get("value") or rule.get("from")):
            status = "changed"
            if rule["kind"] == "date_format":
                self.mapping.update_link(s, rule["mapping_id"], rule["target_column"],
                                         {"date_format": java_to_strftime(value)}, user, note or "date format corrected")
            elif rule["kind"] == "code_suffix":
                spec = MappingSpec.model_validate(self.mapping.load(s, rule["mapping_id"])["spec"])
                parts = [p.model_dump() for p in spec.column(rule["target_column"]).parts]
                parts[-1]["text"] = value
                self.mapping.update_link(s, rule["mapping_id"], rule["target_column"], {"parts": parts}, user,
                                         note or "code suffix corrected")
            elif rule["kind"] == "number_format":
                if value not in (".", ","):
                    raise ValueError("decimal separator must be '.' or ','")
            else:
                raise ValueError(f"{rule['kind']} is changed on the Start onboarding screen or the mapping canvas")
        elif not approve:
            status = "rejected"
        if status != "approved" and rule["kind"] == "number_format":
            self.mapping._reopen(s, user, f"number format of {rule['target_column']} changed")
        decisions[rule_id] = {"status": status, "value": value if value is not None else rule.get("value"),
                              "by": user, "at": now(), "note": note}
        self.ctx.store.put(s.session_id, "harmonisation/value_rules.json", decisions)
        self.ctx.store.trace(s, self.module, f"value_rule.{status}", user, subject=rule_id,
                             before=rule.get("value") or rule.get("from"), after=value or rule.get("to"),
                             why=note or rule.get("issue") or "confirmed")
        return next(r for r in self.rules(s) if r["rule_id"] == rule_id)

    def prepare_source(self, s: Session, mapping_id: str, frame: pd.DataFrame) -> pd.DataFrame:
        """Apply approved number rules to the source before the mapping runs."""
        dec = self._decisions(s)
        rules = [r for r in self.rules(s) if r["mapping_id"] == mapping_id and r["kind"] == "number_format"]
        out = frame.copy()
        for r in rules:
            if (dec.get(r["rule_id"]) or {}).get("status") == "rejected":
                continue
            col = r["source_column"]
            if col in out.columns:
                conv = normalise_number(out[col], r.get("value") or ".")
                out[col] = conv.map(lambda v: None if v is None or v != v else repr(float(v)))
        return out

    def open_issues(self, s: Session) -> list[dict]:
        return [r for r in self.rules(s) if r["issue"] and r["status"] == "proposed"]

    def decimals(self, s: Session) -> dict[str, dict[str, str]]:
        """mapping id -> {target column: ','} for number rules decided as decimal comma; fed to the SQL."""
        out: dict[str, dict[str, str]] = {}
        for r in self.rules(s):
            if r["kind"] == "number_format" and r.get("value") == "," and r["status"] != "rejected":
                out.setdefault(r["mapping_id"], {})[r["target_column"]] = ","
        return out


# ── Phase 2: value standardisation on the loaded tables ─────────────────────────────────────
# The same thing written differently by different retailers becomes one standard value in
# silver. Rules are proposed from the distinct values found in bronze and confirmed once.
STANDARD = {
    "uom": {"EA": ["EA", "EACH", "EACHES", "UNIT", "UNITS", "PC", "PCS", "PIECE", "PIECES", "U"],
            "CS": ["CS", "CASE", "CASES", "CTN", "CARTON"], "KG": ["KG", "KGS", "KILO", "KILOGRAM"],
            "L": ["L", "LTR", "LITRE", "LITER", "LITRES"]},
    "currency": {"GBP": ["GBP", "£", "UKP", "STERLING"], "EUR": ["EUR", "€", "EURO"], "USD": ["USD", "$", "US$"],
                 "INR": ["INR", "₹", "RS", "RUPEE"]},
    "flag": {"Y": ["Y", "YES", "TRUE", "T", "1"], "N": ["N", "NO", "FALSE", "F", "0"]},
}
_KIND_OF = {"uom": "uom", "currency": "currency", "currency_code": "currency"}


def _standard(kind: str, value: str) -> str | None:
    v = str(value).strip().upper()
    for std, syn in STANDARD[kind].items():
        if v in syn:
            return std
    return None


class ValueStandardisation:
    module = "M5"

    def __init__(self, ctx, mapping):
        self.ctx, self.mapping = ctx, mapping

    def _decisions(self, s: Session) -> dict:
        return self.ctx.store.get(s.session_id, "harmonisation/standard_rules.json", {})

    def rules(self, s: Session, frames: dict[str, pd.DataFrame]) -> list[dict]:
        """frames: target table -> loaded bronze rows (logical names)."""
        contracts, dec, out = self.ctx.contracts(s), self._decisions(s), []
        for tname, df in frames.items():
            table = contracts.tables[tname]
            for tc in table.columns:
                kind = _KIND_OF.get(tc.context or "") or ("flag" if tc.dtype == "boolean" or tc.semantic == "promotion_flag" else None)
                if not kind or tc.name not in df.columns:
                    continue
                vals = df[tc.name].dropna().astype(str)
                if vals.empty:
                    continue
                counts = vals.value_counts()
                mapping = {}
                for v in counts.index[:50]:
                    std = _standard(kind, v)
                    mapping[v] = std
                rid = f"{tname}:{tc.name}"
                d = dec.get(rid) or {}
                mapping.update(d.get("mapping", {}))
                changes = {k: v for k, v in mapping.items() if v and v != k}
                unknown = [k for k, v in mapping.items() if not v]
                out.append({"rule_id": rid, "kind": kind, "target_table": tname, "column": tc.name,
                            "values": [{"value": k, "rows": int(counts.get(k, 0)), "standard": mapping[k]} for k in mapping],
                            "changes": len(changes), "unknown": unknown,
                            "issue": f"{len(unknown)} value(s) with no standard: {unknown[:5]}" if unknown else None,
                            "status": d.get("status", "proposed" if (changes or unknown) else "no_change"),
                            "decided_by": d.get("by"), "decided_at": d.get("at")})
        return out

    def decide(self, s: Session, rule_id: str, user: str, mapping: dict[str, str] | None = None,
               note: str | None = None) -> dict:
        dec = self._decisions(s)
        prev = dec.get(rule_id, {})
        dec[rule_id] = {"status": "changed" if mapping else "approved", "mapping": {**prev.get("mapping", {}), **(mapping or {})},
                        "by": user, "at": now(), "note": note}
        self.ctx.store.put(s.session_id, "harmonisation/standard_rules.json", dec)
        if s.gates["B"].status == "approved":
            s.gates["B"].status = "reopened"
            self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "standard_rule." + dec[rule_id]["status"], user, subject=rule_id,
                             after=mapping or "as proposed", why=note or "value standard confirmed")
        return dec[rule_id]

    def apply(self, s: Session, tname: str, df: pd.DataFrame, frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
        out = df.copy()
        for r in self.rules(s, frames):
            if r["target_table"] != tname or r["status"] == "rejected":
                continue
            m = {v["value"]: v["standard"] for v in r["values"] if v["standard"]}
            out[r["column"]] = out[r["column"]].map(lambda x: m.get(str(x), x) if x is not None else None)
        return out

    def open_issues(self, s: Session, frames: dict[str, pd.DataFrame]) -> list[dict]:
        return [r for r in self.rules(s, frames) if r["status"] == "proposed" and (r["issue"] or r["changes"])]

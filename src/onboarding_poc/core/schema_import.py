"""Import target schemas so new business entities need no code change.

Accepted inputs
  * CSV / XLSX: one row per column. Recognised headers (any spelling / case):
      table | column | data_type | mandatory (or nullable) | pk | description | field_source |
      onboarding_key | dataset_column | veritas_dataset | default | semantic
    Only table and column are required.
  * SQL DDL: CREATE TABLE statements (NOT NULL, PRIMARY KEY inline or as a constraint).
  * JSON: {"tables": [{"name": ..., "veritas_dataset": ..., "columns": [{...}]}]}

For every column the importer works out where its value will come from and says why:
  source      matched to retailer file columns (semantic type guessed from the name)
  onboarding  business field captured once per retailer on the onboarding form
              (legal entity, management entity, transaction id, UoM, ...)
  system      generated at load (submission id, insert timestamp, status, retailer)
  optional    free slot (measure_n, attribute_n), filled only when a mapping says so
Anything guessed is listed in the report so a person can correct the YAML or re-import
with an explicit field_source column. Output: one YAML per table in the schema folder.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, Field

from onboarding_poc.core.contracts import Catalogue
from onboarding_poc.core.discovery.hints import HintsInferencer
from onboarding_poc.core.text import canon_header

_HEADER_ALIASES = {
    "table": ["table", "table_name", "entity", "target_table", "tablename", "object"],
    "column": ["column", "column_name", "field", "field_name", "target_column", "columnname", "attribute"],
    "data_type": ["data_type", "datatype", "type", "column_type"],
    "mandatory": ["mandatory", "required", "not_null", "is_mandatory"],
    "nullable": ["nullable", "is_nullable", "null"],
    "pk": ["pk", "primary_key", "is_pk", "key", "is_primary_key"],
    "description": ["description", "desc", "comment", "business_description", "definition"],
    "field_source": ["field_source", "source_type", "fill_from", "value_source", "populated_by"],
    "onboarding_key": ["onboarding_key", "profile_key", "business_key"],
    "dataset_column": ["dataset_column", "veritas_column", "logical_name", "mapping_column"],
    "veritas_dataset": ["veritas_dataset", "dataset"],
    "default": ["default", "default_value", "constant"],
    "semantic": ["semantic", "semantic_type"],
}

# column name -> (generator) for load / audit columns written by the pipeline
_SYSTEM = {
    "submission_id": "submission_id", "row_insert_ts": "insert_ts", "insert_ts": "insert_ts", "load_ts": "insert_ts",
    "load_timestamp": "insert_ts", "created_at": "insert_ts", "created_ts": "insert_ts", "updated_at": "insert_ts",
    "modified_ts": "insert_ts", "row_update_ts": "insert_ts", "current_status": "const:I", "record_status": "const:I",
    "is_current": "const:Y", "batch_id": "batch_id", "load_batch_id": "batch_id", "run_id": "batch_id",
    "source_file": "source_file", "file_name": "source_file", "record_hash": "record_hash", "row_hash": "record_hash",
    "retailer": "retailer", "created_by": "const:onboarding_poc", "updated_by": "const:onboarding_poc",
}
# column name -> onboarding field key
_ONBOARDING = {
    "legal_entity_code": "legal_entity_code", "legal_entity": "legal_entity_code", "le_code": "legal_entity_code",
    "management_entity_code": "mgmt_entity_code", "mgmt_entity_code": "mgmt_entity_code",
    "legc": "legc", "transaction_id": "transaction_id", "uom": "uom", "unit_of_measure": "uom",
    "data_provider": "data_platform", "source_system": "data_platform", "client_id": "client_id",
    "retailer_code": "retailer", "market_code": "market", "country_code": "country_code",
}
_SOURCE_ALIASES = {"source": "source", "file": "source", "retailer_file": "source", "mapping": "source",
                   "onboarding": "onboarding", "business": "onboarding", "retailer": "onboarding", "profile": "onboarding",
                   "client": "onboarding", "system": "system", "audit": "system", "load": "system",
                   "derived": "derived", "lookup": "derived", "optional": "optional"}
_FREE_SLOT = re.compile(r"^(measure|attribute|kpi|custom|extra|flex)_?\d+$")


class ImportReport(BaseModel):
    tables: dict[str, dict[str, Any]] = Field(default_factory=dict)    # table -> summary
    notes: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)


def _dtype(t: str | None) -> str:
    t = (t or "").lower()
    if any(k in t for k in ("int", "long")):
        return "integer"
    if any(k in t for k in ("dec", "num", "float", "double", "real", "money")):
        return "decimal"
    if "bool" in t or t == "bit":
        return "boolean"
    if t.startswith("date") and "time" not in t:
        return "date"
    return "string"


def _truthy(v) -> bool | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v).strip().lower() in {"y", "yes", "true", "1", "x", "pk", "required", "not null", "mandatory"}


def _rows_from_frame(df: pd.DataFrame) -> list[dict]:
    rename = {}
    canon = {canon_header(c): c for c in df.columns}
    for key, aliases in _HEADER_ALIASES.items():
        for a in aliases:
            if a in canon:
                rename[canon[a]] = key
                break
    df = df.rename(columns=rename)
    if "table" not in df.columns or "column" not in df.columns:
        raise ValueError("schema file needs at least 'table' and 'column' headers")
    return df.where(df.notna(), None).to_dict("records")


def _rows_from_ddl(sql: str) -> list[dict]:
    rows = []
    for m in re.finditer(r"create\s+(?:or\s+replace\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)\s*\((.*?)\)\s*[;)]?\s*(?=create|\Z)",
                         sql, flags=re.IGNORECASE | re.DOTALL):
        table = re.sub(r"[\"`\[\]]", "", m.group(1))
        body = m.group(2)
        parts, depth, cur = [], 0, ""
        for ch in body:
            depth += ch == "("
            depth -= ch == ")"
            if ch == "," and depth == 0:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        pk: list[str] = []
        cols = []
        for p in (x.strip() for x in parts if x.strip()):
            if re.match(r"(constraint\s+\w+\s+)?primary\s+key", p, re.IGNORECASE):
                pk += [re.sub(r"[\"`\[\]\s]", "", c) for c in re.search(r"\((.*)\)", p).group(1).split(",")]
                continue
            if re.match(r"(constraint|unique|foreign|check|index)\b", p, re.IGNORECASE):
                continue
            tokens = p.split()
            name = re.sub(r"[\"`\[\]]", "", tokens[0])
            rest = " ".join(tokens[1:])
            comment = re.search(r"comment\s+'([^']*)'", rest, re.IGNORECASE)
            cols.append({"table": table, "column": name, "data_type": tokens[1] if len(tokens) > 1 else "",
                         "mandatory": bool(re.search(r"not\s+null", rest, re.IGNORECASE)),
                         "pk": bool(re.search(r"primary\s+key", rest, re.IGNORECASE)),
                         "description": comment.group(1) if comment else None})
        for c in cols:
            if c["column"] in pk:
                c["pk"] = True
        rows += cols
    if not rows:
        raise ValueError("no CREATE TABLE statements found")
    return rows


def read_schema(content: bytes | str, filename: str) -> list[dict]:
    name = filename.lower()
    if name.endswith((".xlsx", ".xls")):
        data = content if isinstance(content, bytes) else content.encode()
        frames = pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str)
        rows = []
        for sheet, df in frames.items():
            if "table" not in {canon_header(c) for c in df.columns} and {"column", "column_name", "field"} & \
                    {canon_header(c) for c in df.columns}:
                df["table"] = sheet            # one sheet per table
            rows += _rows_from_frame(df)
        return rows
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    if name.endswith(".json"):
        data = json.loads(text)
        rows = []
        for t in data.get("tables", data if isinstance(data, list) else []):
            for c in t.get("columns", []):
                rows.append({"table": t["name"], "veritas_dataset": t.get("veritas_dataset"), **c,
                             "column": c.get("column") or c.get("name")})
        return rows
    if name.endswith(".sql") or re.search(r"create\s+table", text[:2000], re.IGNORECASE):
        return _rows_from_ddl(text)
    return _rows_from_frame(pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python"))


def build_tables(rows: list[dict], catalogue: Catalogue) -> tuple[dict[str, dict], ImportReport]:
    hints = HintsInferencer(catalogue)
    report = ImportReport()
    by_table: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("table") and r.get("column"):
            by_table.setdefault(str(r["table"]).strip(), []).append(r)
    tables: dict[str, dict] = {}
    for tname, cols in by_table.items():
        upper = sum(str(c["column"]).isupper() for c in cols) > len(cols) / 2
        dataset = next((c.get("veritas_dataset") for c in cols if c.get("veritas_dataset")), None)
        spec_cols, rename, system, order = [], {}, {}, []
        summary: dict[str, Any] = {"columns": 0, "source": [], "onboarding": [], "system": [], "optional": [],
                                   "derived": [], "guesses": []}
        for c in cols:
            phys = str(c["column"]).strip()
            order.append(phys)
            logical = canon_header(c.get("dataset_column") or phys)
            if canon_header(phys) != logical:
                rename[logical] = phys.upper() if upper else phys
            fs = _SOURCE_ALIASES.get(canon_header(c.get("field_source") or ""), None)
            key = canon_header(c.get("onboarding_key") or "") or None
            col: dict[str, Any] = {"name": logical, "dtype": _dtype(c.get("data_type"))}
            mand = _truthy(c.get("mandatory"))
            if mand is None and _truthy(c.get("nullable")) is not None:
                mand = not _truthy(c.get("nullable"))
            if mand:
                col["mandatory"] = True
            if _truthy(c.get("pk")):
                col["pk"] = True
                col["mandatory"] = True
            if c.get("description"):
                col["description"] = str(c["description"])
            if c.get("default"):
                col["default"] = str(c["default"])
            why = "given in schema file" if fs else ""
            if fs is None:
                if logical in _SYSTEM:
                    fs, why = "system", f"name '{phys}' looks like a load/audit column"
                elif logical in _ONBOARDING:
                    fs, key, why = "onboarding", _ONBOARDING[logical], f"name '{phys}' is a business field"
                elif _FREE_SLOT.match(logical):
                    fs, why = "optional", "numbered free slot"
                else:
                    fs = "source"
            if fs == "system":
                system[phys.upper() if upper else phys] = _SYSTEM.get(logical, "const:")
                summary["system"].append(phys)
                if why:
                    summary["guesses"].append(f"{phys}: system ({why}) -> {_SYSTEM.get(logical, 'const:')}")
                continue                                           # written by the loader, not mapped
            if fs == "onboarding":
                col["context"] = key or logical
                summary["onboarding"].append(phys)
                if why and why != "given in schema file":
                    summary["guesses"].append(f"{phys}: onboarding field '{col['context']}' ({why})")
            elif fs == "source":
                sem = c.get("semantic")
                if not sem:
                    h = hints._by_name(logical.replace("_", " "))
                    if h and h.confidence >= 0.85:
                        sem = h.semantic_type
                        summary["guesses"].append(f"{phys}: matches retailer data of type '{sem}' ({h.rationale})")
                if sem:
                    col["semantic"] = sem
                    if sem == "currency_code":
                        col["context"] = "currency"
                    if sem in ("retailer_sku_code", "retailer_outlet_code") and dataset:
                        col["code_suffix"] = "product_code_suffix" if sem == "retailer_sku_code" else "outlet_code_suffix"
                        summary["guesses"].append(f"{phys}: retailer code gets a per-retailer suffix "
                                                  f"('{col['code_suffix']}') as in Veritas mappings")
                    if sem == "week_ending" and col["dtype"] != "date":
                        col["date_out"] = "yyyyMMdd"
                        summary["guesses"].append(f"{phys}: date written as text yyyyMMdd (as in POS_SELLOUT)")
                else:
                    summary["guesses"].append(f"{phys}: no retailer data type recognised; mapped by name/LLM or by hand")
                summary["source"].append(phys)
            else:
                summary[fs if fs in summary else "optional"].append(phys)
            spec_cols.append(col)
        spec: dict[str, Any] = {"kind": "fact" if any(c.get("semantic") in ("units_sold", "net_value") for c in spec_cols)
                                else "dimension",
                                "description": f"Imported target schema ({len(cols)} columns)",
                                "physical_name": tname, "columns": spec_cols}
        if dataset:
            spec["veritas_dataset"] = dataset
        if upper or rename or system:
            spec["physical"] = {"case": "upper" if upper else "asis", "rename": rename,
                                "system_columns": system, "order": [o.upper() if upper else o for o in order]}
        summary["columns"] = len(cols)
        tables[tname.lower()] = spec
        report.tables[tname.lower()] = summary
    return tables, report


def import_schema(content: bytes | str, filename: str, catalogue: Catalogue, out_dir: str) -> ImportReport:
    tables, report = build_tables(read_schema(content, filename), catalogue)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, spec in tables.items():
        p = out / f"{re.sub(r'[^a-z0-9_.]+', '_', name)}.yaml"
        header = (f"# Imported from {filename}. Check the guesses listed in the import report, edit freely;\n"
                  f"# re-importing the same table overwrites this file.\n")
        p.write_text(header + yaml.safe_dump({"tables": {name: spec}}, sort_keys=False, allow_unicode=True),
                     encoding="utf-8")
        report.files.append(str(p))
    return report

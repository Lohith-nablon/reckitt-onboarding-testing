"""Veritas mapping JSON <-> internal MappingSpec.

Format taken from Veritas mapping #4285 (Tesco_Toolkit_Daily_Store_LevelData_SMEx, dataset
POS_SELLOUT). Each dataset column has a tree of operators:

    DirectColumn   {ColumnName, ColumnType}                 SISO, leaf
    DirectText     {Text}                                   SISO, leaf
    DateFormat     {CurrentDateFormat, TargetDateFormat}    SISO, one input
    Concatenation  inputs in order                          MISO
    NULL           column left empty                        SISO

Other operators (maths, conditions, text functions) are mapped through
config/veritas_operators.yaml onto the expression functions in transforms.py, in both
directions. Unknown operator names stop the import of that mapping with a clear message.

Veritas stores source column names in lower case with spaces, while the landing zone writes
them in snake_case ('tpnb description' vs 'tpnb_description'); matching uses canon_header(). FileType.SkipFirstRows / SkipLastRows
carry the header offset and trailing total rows found by Discovery. OperatorPositions are
canvas coordinates for the Veritas UI; we lay generated operators out on a simple grid.

Not seen in the sample, so not emitted: lookups, filters, sheet selection for Excel files.
Excel sheet names go into Metadata.SheetName (ASSUMPTION: confirm Veritas accepts it).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from onboarding_poc.core.contracts import TargetTable
import ast
import json
from functools import lru_cache
from pathlib import Path

import yaml

from onboarding_poc.core.mapping.models import ColumnMapping, ConcatPart, MappingSpec, SourceSpec
from onboarding_poc.core.mapping import transforms
from onboarding_poc.core.text import canon_header

_OPERATORS_FILE = Path(__file__).resolve().parents[1] / "config" / "veritas_operators.yaml"


@lru_cache(maxsize=4)
def operator_catalogue(path: str = str(_OPERATORS_FILE)) -> dict[str, dict]:
    """Veritas operator name -> {function, type, details, kind}. See config/veritas_operators.yaml."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {o["name"]: o for o in raw.get("operators", [])}


def _by_function() -> dict[str, dict]:
    return {o["function"]: o for o in operator_catalogue().values() if o.get("function")}

def supported_operators() -> set[str]:
    return set(operator_catalogue())

# Java (Veritas) <-> strftime tokens, longest first
_JAVA_TOKENS = [("yyyy", "%Y"), ("MMMM", "%B"), ("MMM", "%b"), ("EEEE", "%A"), ("EEE", "%a"), ("yy", "%y"),
                ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"), ("d", "%d"), ("M", "%m")]


class UnsupportedVeritasMapping(ValueError):
    pass


def java_to_strftime(fmt: str) -> str:
    out, i = "", 0
    while i < len(fmt):
        for j, s in _JAVA_TOKENS:
            if fmt.startswith(j, i):
                out += s
                i += len(j)
                break
        else:
            out += fmt[i]
            i += 1
    return out


def strftime_to_java(fmt: str) -> str:
    rev = {"%Y": "yyyy", "%y": "yy", "%m": "MM", "%d": "dd", "%b": "MMM", "%B": "MMMM",
           "%H": "HH", "%M": "mm", "%S": "ss", "%a": "EEE", "%A": "EEEE"}
    out, i = "", 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            out += rev.get(fmt[i:i + 2], fmt[i:i + 2])
            i += 2
        else:
            out += fmt[i]
            i += 1
    return out


def _vcol(name: str) -> str:
    return " ".join(str(name).lower().split())


# ── operator builders ───────────────────────────────────────────────────────

def _id() -> str:
    return str(uuid.uuid4())


def _op_column(name: str) -> dict:
    return {"Details": {"ColumnName": _vcol(name), "ColumnType": {"Name": "String", "Details": None}},
            "Id": _id(), "Name": "DirectColumn", "Inputs": None, "Type": "SISO"}


def _op_text(text: str) -> dict:
    return {"Details": {"Text": text}, "Id": _id(), "Name": "DirectText", "Inputs": None, "Type": "SISO"}


def _op_null() -> dict:
    return {"Details": None, "Id": None, "Name": "NULL", "Inputs": None, "Type": "SISO"}


def _transformation(m: ColumnMapping, table: TargetTable, warnings: list[str]) -> dict:
    if m.action is None:
        return _op_null()
    if m.action == "DIRECT_COLUMN":
        return _op_column(m.source_columns[0])
    if m.action == "DIRECT_TEXT":
        return _op_text(m.value or "")
    if m.action == "DATE":
        target = m.target_date_format or (table.column(m.target_column).date_out if table.has(m.target_column) else None)
        return {"Details": {"CurrentDateFormat": strftime_to_java(m.date_format or ""),
                            "TargetDateFormat": target or "yyyyMMdd"},
                "Id": _id(), "Name": "DateFormat", "Inputs": [_op_column(m.source_columns[0])], "Type": "SISO"}
    if m.action == "CONCATENATION":
        inputs = []
        if m.parts:
            for p in m.parts:
                inputs.append(_op_column(p.column) if p.column is not None else _op_text(p.text or ""))
        else:
            for i, c in enumerate(m.source_columns):
                if i and m.separator:
                    inputs.append(_op_text(m.separator))
                inputs.append(_op_column(c))
        return {"Details": None, "Id": _id(), "Name": "Concatenation", "Inputs": inputs, "Type": "MISO"}
    if m.action == "EXPRESSION" and m.expression:
        return expression_to_tree(m.expression, warnings, m.target_column)
    warnings.append(f"{m.target_column}: {m.action} has no Veritas operator; exported as NULL")
    return _op_null()


def expression_to_tree(expr: str, warnings: list[str], target: str) -> dict:
    """s2b expression -> Veritas operator tree, using the operator catalogue."""
    tree = transforms.parse(expr)
    fns = _by_function()
    bin_fn = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div", ast.Mod: "mod", ast.Pow: "power"}

    def node(n) -> dict:
        if isinstance(n, ast.Expression):
            return node(n.body)
        if isinstance(n, ast.Constant):
            return _op_text("" if n.value is None else str(n.value))
        if isinstance(n, ast.Call) and n.func.id == "col":
            return _op_column(n.args[0].value)
        if isinstance(n, ast.Call) and n.func.id == "text":
            return _op_text(str(n.args[0].value))
        if isinstance(n, ast.BinOp) and type(n.op) in bin_fn:
            fname, args = bin_fn[type(n.op)], [n.left, n.right]
        elif isinstance(n, ast.Call):
            fname, args = n.func.id, list(n.args)
        else:
            warnings.append(f"{target}: '{ast.unparse(n)}' has no Veritas operator; exported as NULL")
            return _op_null()
        op = fns.get(fname)
        if op is None:
            warnings.append(f"{target}: function {fname}() has no Veritas operator in veritas_operators.yaml")
            return _op_null()
        detail_keys = op.get("details") or []
        n_inputs = len(args) - len(detail_keys)
        inputs, extra = args[:n_inputs], args[n_inputs:]
        details = None
        if detail_keys:
            details = {}
            for k, a in zip(detail_keys, extra):
                if not isinstance(a, ast.Constant):
                    warnings.append(f"{target}: {op['name']}.{k} must be a constant")
                details[k] = a.value if isinstance(a, ast.Constant) else ast.unparse(a)
        if not op.get("confirmed"):
            warnings.append(f"{target}: operator '{op['name']}' is a placeholder name; confirm with the Veritas team")
        return {"Details": details, "Id": _id(), "Name": op["name"], "Inputs": [node(a) for a in inputs],
                "Type": op.get("type", "MISO" if len(inputs) > 1 else "SISO")}

    return node(tree)


def tree_to_expression(op: dict, header_map: dict[str, str]) -> str:
    """Veritas operator tree -> s2b expression, using the operator catalogue."""
    cat = operator_catalogue()

    def ex(o: dict) -> str:
        name = o.get("Name")
        d = o.get("Details") or {}
        if name == "DirectColumn":
            c = d["ColumnName"]
            return f"[{header_map.get(canon_header(c), c)}]"
        if name == "DirectText":
            return json.dumps(d.get("Text", ""))
        if name in (None, "NULL"):
            return "null"
        spec = cat.get(name)
        if not spec or not spec.get("function"):
            raise UnsupportedVeritasMapping(f"operator {name} is not in veritas_operators.yaml")
        args = [ex(i) for i in (o.get("Inputs") or [])]
        args += [json.dumps(d.get(k)) for k in spec.get("details") or []]
        return f"{spec['function']}({', '.join(args)})"

    return ex(op)


def _positions(mappings: list[dict]) -> dict[str, list[int]]:
    pos: dict[str, list[int]] = {}
    k = 0
    for mp in mappings:
        t = mp["Transformations"]
        if not t.get("Id"):
            continue
        x = 140 * k

        def place(o: dict, depth: int, j: int) -> None:
            if o.get("Id"):
                pos[o["Id"]] = [x + 28 * j, 600 - 300 * depth + (90 * j if depth else 0)]
            for jj, child in enumerate(o.get("Inputs") or []):
                place(child, depth + 1, jj)
        place(t, 0, 0)
        k += 1
    return pos


def to_veritas(spec: MappingSpec, table: TargetTable, *, legc: str | None, retailer_display: str,
               developed_by: str, existing: dict | None = None) -> tuple[dict, list[str]]:
    """Build a Veritas mapping document. Returns (document, warnings)."""
    if not table.veritas_dataset:
        raise ValueError(f"{table.name} has no Veritas dataset")
    warnings: list[str] = []
    by_target = {c.target_column: c for c in spec.columns}
    mappings = []
    for tc in table.columns:
        m = by_target.get(tc.name) or ColumnMapping(target_column=tc.name)
        mappings.append({"DatasetColumn": tc.name, "Transformations": _transformation(m, table, warnings)})
    now = datetime.now(timezone.utc)
    approved_by = spec.approved_by or developed_by
    meta: dict[str, Any] = {"RetailerName": retailer_display}
    if spec.source.sheet:
        meta["SheetName"] = spec.source.sheet
    name = spec.veritas_name or f"{retailer_display}_{spec.source.file_pattern.rsplit('.', 1)[0].rstrip('_*')}_nablon"
    doc = {
        "id": spec.veritas_id,
        "LEGC": legc,
        "Name": name,
        "Dataset": table.veritas_dataset,
        "DevelopedBy": (existing or {}).get("DevelopedBy") or developed_by,
        "FileType": {
            "Format": spec.source.file_format, "Delimiter": spec.source.delimiter or ",",
            "QuoteCharacter": "\"", "SkipFirstRows": spec.source.header_row,
            "SkipLastRows": spec.source.footer_rows, "HasHeader": True,
        },
        "SourceColumnsUsed": [_vcol(c) for c in spec.source.source_columns],
        "Mappings": mappings,
        "Filters": [],
        "Metadata": meta,
        "Description": f"Generated by the Nablon retailer onboarding accelerator from {spec.source.file_pattern} "
                       f"(mapping {spec.mapping_id} v{spec.version}, {spec.origin}).",
        "ModificationStatus": {
            "CreationTS": (existing or {}).get("ModificationStatus", {}).get("CreationTS") or now.strftime("%Y-%m-%dT%H:%M:%S"),
            "ModificationTS": now.isoformat().replace("+00:00", "Z"),
            "CreationUser": (existing or {}).get("ModificationStatus", {}).get("CreationUser") or developed_by,
            "ModificationUser": approved_by,
        },
    }
    doc["OperatorPositions"] = _positions(mappings)
    return doc, warnings


# ── import ──────────────────────────────────────────────────────────────────

def _walk(op: dict | None) -> list[dict]:
    if not op:
        return []
    out = [op]
    for child in op.get("Inputs") or []:
        out += _walk(child)
    return out


def operators_used(doc: dict) -> set[str]:
    return {o.get("Name") for mp in doc.get("Mappings", []) for o in _walk(mp.get("Transformations"))}


def covers(doc: dict, headers: list[str]) -> bool:
    """True if every source column the mapping uses is present in the file headers."""
    hs = {canon_header(h) for h in headers}
    used = {canon_header(c) for c in doc.get("SourceColumnsUsed", [])}
    for mp in doc.get("Mappings", []):
        for o in _walk(mp.get("Transformations")):
            if o.get("Name") == "DirectColumn":
                used.add(canon_header(o["Details"]["ColumnName"]))
    return bool(used) and used <= hs


def _to_column(mp: dict, table: TargetTable, header_map: dict[str, str]) -> ColumnMapping:
    try:
        return _to_simple_column(mp, table, header_map)
    except UnsupportedVeritasMapping:
        # anything beyond the simple patterns (maths, nested operators) becomes an expression
        tcol = mp["DatasetColumn"]
        tc = table.column(tcol) if table.has(tcol) else None
        expr = tree_to_expression(mp.get("Transformations") or {}, header_map)
        return ColumnMapping(target_column=tcol, action="EXPRESSION", expression=expr,
                             source_columns=transforms.columns_used(expr), mandatory=bool(tc and tc.mandatory),
                             is_pk=bool(tc and tc.pk), confidence=1.0)


def _to_simple_column(mp: dict, table: TargetTable, header_map: dict[str, str]) -> ColumnMapping:
    tcol = mp["DatasetColumn"]
    tc = table.column(tcol) if table.has(tcol) else None
    m = ColumnMapping(target_column=tcol, mandatory=bool(tc and tc.mandatory), is_pk=bool(tc and tc.pk),
                      confidence=1.0)
    op = mp.get("Transformations") or {}
    name = op.get("Name")

    def col(o: dict) -> str:
        n = o["Details"]["ColumnName"]
        return header_map.get(canon_header(n), n)

    if name in (None, "NULL"):
        m.confidence = 0.0
        return m
    if name == "DirectColumn":
        m.action, m.source_columns = "DIRECT_COLUMN", [col(op)]
    elif name == "DirectText":
        m.action, m.value = "DIRECT_TEXT", (op.get("Details") or {}).get("Text")
    elif name == "DateFormat":
        inputs = op.get("Inputs") or []
        if len(inputs) != 1 or inputs[0].get("Name") != "DirectColumn":
            raise UnsupportedVeritasMapping(f"{tcol}: DateFormat over {[i.get('Name') for i in inputs]}")
        d = op.get("Details") or {}
        m.action, m.source_columns = "DATE", [col(inputs[0])]
        m.date_format = java_to_strftime(d.get("CurrentDateFormat", ""))
        m.target_date_format = d.get("TargetDateFormat")
    elif name == "Concatenation":
        parts = []
        for i in op.get("Inputs") or []:
            if i.get("Name") == "DirectColumn":
                parts.append(ConcatPart(column=col(i)))
            elif i.get("Name") == "DirectText":
                parts.append(ConcatPart(text=(i.get("Details") or {}).get("Text", "")))
            else:
                raise UnsupportedVeritasMapping(f"{tcol}: Concatenation input {i.get('Name')}")
        m.action, m.parts = "CONCATENATION", parts
        m.source_columns = [p.column for p in parts if p.column]
    else:
        raise UnsupportedVeritasMapping(f"{tcol}: operator {name}")
    return m


def from_veritas(doc: dict, table: TargetTable, *, market: str, retailer: str, fingerprint: str,
                 headers: list[str] | None = None, sheet: str | None = None,
                 file_pattern: str | None = None) -> MappingSpec:
    """Convert a Veritas mapping into an approved internal spec, resolving column names to the file's headers."""
    unknown = operators_used(doc) - supported_operators() - {None}
    if unknown:
        raise UnsupportedVeritasMapping(f"operators not in veritas_operators.yaml: {sorted(unknown)}")
    header_map = {canon_header(h): h for h in (headers or [])}
    cols = [_to_column(mp, table, header_map) for mp in doc.get("Mappings", []) if table.has(mp.get("DatasetColumn"))]
    seen = {c.target_column for c in cols}
    for tc in table.columns:
        if tc.name not in seen:
            cols.append(ColumnMapping(target_column=tc.name, mandatory=tc.mandatory, is_pk=tc.pk))
    order = {tc.name: i for i, tc in enumerate(table.columns)}
    cols.sort(key=lambda c: order.get(c.target_column, 999))
    for c in cols:
        if c.action:
            c.rationale = f"Veritas #{doc.get('id')} {doc.get('Name')}"
    ft = doc.get("FileType") or {}
    ms = doc.get("ModificationStatus") or {}
    return MappingSpec(
        mapping_id=str(uuid.uuid4()), status="approved", market=market, retailer=retailer,
        source=SourceSpec(file_pattern=file_pattern or doc.get("Name", ""), sheet=sheet,
                          header_row=int(ft.get("SkipFirstRows") or 0), footer_rows=int(ft.get("SkipLastRows") or 0),
                          file_format=ft.get("Format") or "csv", delimiter=ft.get("Delimiter"),
                          template_fingerprint=fingerprint,
                          source_columns=list(headers) if headers else list(doc.get("SourceColumnsUsed", []))),
        target_table=table.name, columns=cols, origin=f"veritas:{doc.get('id')}", veritas_id=doc.get("id"),
        veritas_name=doc.get("Name"), approved_by=ms.get("ModificationUser") or doc.get("DevelopedBy"),
        approved_at=ms.get("ModificationTS"),
    )


def conventions(doc: dict) -> dict[str, dict]:
    """What an existing mapping teaches us, per dataset column.

    {"legal_entity_code": {"text": "IGB0201"},
     "retailer_product_code": {"column": "tpnb", "suffix": "_Tesco"},
     "quantity": {"column": "sales volume ty"},
     "pos_date": {"column": "calendar date", "date_in": "yyyy-MM-dd", "date_out": "yyyyMMdd"}}
    """
    out: dict[str, dict] = {}
    for mp in doc.get("Mappings", []):
        op = mp.get("Transformations") or {}
        name, d = op.get("Name"), op.get("Details") or {}
        if name == "DirectText":
            out[mp["DatasetColumn"]] = {"text": d.get("Text")}
        elif name == "DirectColumn":
            out[mp["DatasetColumn"]] = {"column": canon_header(d["ColumnName"])}
        elif name == "DateFormat":
            inp = (op.get("Inputs") or [{}])[0]
            if inp.get("Name") == "DirectColumn":
                out[mp["DatasetColumn"]] = {"column": canon_header(inp["Details"]["ColumnName"]),
                                            "date_in": d.get("CurrentDateFormat"), "date_out": d.get("TargetDateFormat")}
        elif name == "Concatenation":
            ins = op.get("Inputs") or []
            if len(ins) == 2 and ins[0].get("Name") == "DirectColumn" and ins[1].get("Name") == "DirectText":
                out[mp["DatasetColumn"]] = {"column": canon_header(ins[0]["Details"]["ColumnName"]),
                                            "suffix": (ins[1].get("Details") or {}).get("Text", "")}
    return out

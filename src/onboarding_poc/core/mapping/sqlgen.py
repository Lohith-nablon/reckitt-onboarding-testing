"""Mapping spec -> Databricks SQL that loads one retailer file into its bronze target table.

The approved mapping (Veritas JSON) says what each target column is made of. Reckitt's
existing Databricks pipeline executes SQL, so the app turns the mapping into one idempotent
script per (file, target table):

    DELETE FROM <bronze table> WHERE <submission/source file> = ...;   -- safe to re-run
    INSERT INTO <bronze table> (<physical columns>)
    SELECT <physical layout>                                           -- load columns, order
    FROM (SELECT <one expression per target column> FROM <source>) AS mapped;

<source> is `read_files('abfss://...raw file...', format => 'csv', ...)` by default, or a
landing table if the pipeline lands raw files first (config `sql.source_mode`).

Semantics match the Python preview (mapping/apply.py): same trimming, number parsing,
date parsing, concatenation rules, so what the reviewer saw is what the pipeline loads.
SQL uses try_* functions so a bad value becomes NULL (and fails a check) instead of
failing the whole pipeline run.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from onboarding_poc.core.contracts import TargetTable
from onboarding_poc.core.mapping.models import ColumnMapping, MappingSpec
from onboarding_poc.core.mapping.transforms import parse
from onboarding_poc.core.mapping.veritas import strftime_to_java

_SQL_TYPE = {"string": "STRING", "decimal": "DECIMAL(38,6)", "integer": "BIGINT", "boolean": "BOOLEAN", "date": "DATE"}
_TRUE = ("y", "yes", "true", "t", "1")
_FALSE = ("n", "no", "false", "f", "0")


def q(name: str) -> str:
    return "`" + str(name).replace("`", "``") + "`"


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _num_text(x: str) -> str:
    """to_number in mapping/transforms: strip currency, spaces and thousands commas, (12) -> -12."""
    clean = f"regexp_replace(trim(CAST({x} AS STRING)), '[£$€₹\\\\s,]', '')"
    return f"try_cast(regexp_replace({clean}, '^\\\\((.*)\\\\)$', '-$1') AS DECIMAL(38,6))"


def _date(x: str, java_fmt: str | None) -> str:
    base = f"substring_index(trim(CAST({x} AS STRING)), 'T', 1)"
    if java_fmt:
        return f"to_date(try_to_timestamp({base}, {lit(java_fmt)}))"
    return f"try_cast({base} AS DATE)"


@dataclass
class Source:
    """Where the raw rows come from in the pipeline."""
    mode: str = "read_files"            # read_files | table
    path: str | None = None             # abfss://... raw file (read_files)
    table: str | None = None            # catalog.schema.table (table)
    file_format: str = "csv"
    delimiter: str | None = None
    header_row: int = 0                 # rows above the header (Veritas SkipFirstRows)
    sheet: str | None = None

    def sql(self) -> str:
        if self.mode == "table":
            return self.table or "${source_table}"
        opts = []
        if self.file_format in ("xlsx", "xls", "xlsm", "excel"):
            opts += ["format => 'excel'", "headerRows => 1"]
            if self.sheet or self.header_row:
                opts.append(f"dataAddress => {lit((repr(self.sheet) if self.sheet else '') + ('!A' + str(self.header_row + 1) if self.header_row else ''))}")
        else:
            opts += ["format => 'csv'", "header => true", "inferSchema => false", "mode => 'PERMISSIVE'"]
            if self.delimiter:
                opts.append(f"sep => {lit(self.delimiter)}")
            if self.header_row:
                opts.append(f"skipRows => {self.header_row}")
        return f"read_files({lit(self.path or '${source_path}')}, {', '.join(opts)})"


@dataclass
class SqlBundle:
    sql: str
    target_fqn: str
    select_sql: str                      # the INSERT's SELECT alone (used by the local pipeline runner)
    delete_sql: str | None
    columns: list[str]
    warnings: list[str] = field(default_factory=list)


class _Expr:
    """Our mapping expression language (mapping/transforms.py) -> Spark SQL."""

    def __init__(self, col):
        self.col = col

    def __call__(self, expr: str) -> str:
        return self.ev(parse(expr).body)

    def ev(self, n) -> str:
        if isinstance(n, ast.Constant):
            return lit(n.value)
        if isinstance(n, ast.Name):
            return {"null": "NULL", "none": "NULL", "true": "TRUE", "false": "FALSE"}[n.id.lower()]
        if isinstance(n, ast.UnaryOp):
            v = self.ev(n.operand)
            return f"(-{_num_text(v)})" if isinstance(n.op, ast.USub) else f"(NOT {v})" if isinstance(n.op, ast.Not) else v
        if isinstance(n, ast.BinOp):
            fn = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul", ast.Div: "div", ast.Mod: "mod", ast.Pow: "power"}[type(n.op)]
            return self.fn(fn, [n.left, n.right])
        if isinstance(n, ast.BoolOp):
            op = " AND " if isinstance(n.op, ast.And) else " OR "
            return "(" + op.join(self.ev(v) for v in n.values) + ")"
        if isinstance(n, ast.Compare):
            ops = {ast.Eq: "=", ast.NotEq: "<>", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
            parts, left = [], n.left
            for op, right in zip(n.ops, n.comparators):
                parts.append(f"({self.ev(left)} {ops[type(op)]} {self.ev(right)})")
                left = right
            return "(" + " AND ".join(parts) + ")"
        if isinstance(n, ast.Call):
            return self.fn(n.func.id, n.args)
        raise ValueError(f"cannot translate {type(n).__name__}")

    def fn(self, name: str, args) -> str:
        a = [self.ev(x) if not (name == "col" and isinstance(x, ast.Constant)) else x.value for x in args]
        num = [_num_text(x) for x in a] if name in ("add", "sub", "mul", "div", "mod", "power", "neg", "round", "abs",
                                                       "floor", "ceil", "sqrt", "min_of", "max_of", "sum_of") else a
        m = {
            "col": lambda: self.col(a[0]), "text": lambda: a[0],
            "add": lambda: "(" + " + ".join(num) + ")", "sub": lambda: f"({num[0]} - {num[1]})",
            "mul": lambda: "(" + " * ".join(num) + ")", "div": lambda: f"({num[0]} / nullif({num[1]}, 0))",
            "mod": lambda: f"({num[0]} % {num[1]})", "power": lambda: f"power({num[0]}, {num[1]})",
            "neg": lambda: f"(-{num[0]})", "round": lambda: f"round({num[0]}, {a[1] if len(a) > 1 else 0})",
            "abs": lambda: f"abs({num[0]})", "floor": lambda: f"floor({num[0]})", "ceil": lambda: f"ceil({num[0]})",
            "sqrt": lambda: f"sqrt({num[0]})", "min_of": lambda: f"least({', '.join(num)})",
            "max_of": lambda: f"greatest({', '.join(num)})",
            "sum_of": lambda: "(" + " + ".join(f"coalesce({x}, 0)" for x in num) + ")",
            "to_number": lambda: _num_text(a[0]), "to_text": lambda: f"CAST({a[0]} AS STRING)",
            "to_int": lambda: f"CAST({_num_text(a[0])} AS BIGINT)",
            "coalesce": lambda: f"coalesce({', '.join(a)})", "if_null": lambda: f"coalesce({a[0]}, {a[1]})",
            "nullif": lambda: f"nullif({a[0]}, {a[1]})", "is_null": lambda: f"({a[0]} IS NULL)",
            "iif": lambda: f"CASE WHEN {a[0]} THEN {a[1]} ELSE {a[2]} END",
            "case": lambda: "CASE " + " ".join(f"WHEN {a[i]} THEN {a[i + 1]}" for i in range(0, len(a) - 1, 2))
                            + f" ELSE {a[-1] if len(a) % 2 else 'NULL'} END",
            "in_list": lambda: f"({a[0]} IN ({', '.join(a[1:])}))",
            "concat": lambda: f"concat({', '.join(f'CAST({x} AS STRING)' for x in a)})",
            "upper": lambda: f"upper({a[0]})", "lower": lambda: f"lower({a[0]})", "trim": lambda: f"trim({a[0]})",
            "length": lambda: f"length({a[0]})",
            "substr": lambda: f"substr({a[0]}, {a[1]}" + (f", {a[2]})" if len(a) > 2 else ")"),
            "left": lambda: f"left({a[0]}, {a[1]})", "right": lambda: f"right({a[0]}, {a[1]})",
            "replace": lambda: f"replace({a[0]}, {a[1]}, {a[2]})",
            "regex_replace": lambda: f"regexp_replace({a[0]}, {a[1]}, {a[2]})",
            "regex_extract": lambda: f"regexp_extract({a[0]}, {a[1]}, {a[2] if len(a) > 2 else 1})",
            "lpad": lambda: f"lpad({a[0]}, {a[1]}, {a[2] if len(a) > 2 else lit('0')})",
            "rpad": lambda: f"rpad({a[0]}, {a[1]}, {a[2] if len(a) > 2 else lit(' ')})",
            "split_part": lambda: f"split_part({a[0]}, {a[1]}, {a[2]})",
            "to_date": lambda: _date(a[0], json.loads(a[1].replace("'", '"')) if len(a) > 1 and a[1].startswith("'") else None),
            "format_date": lambda: f"date_format({a[0]}, {a[1]})",
            "reformat_date": lambda: f"date_format({_date(a[0], json.loads(a[1].replace(chr(39), chr(34))))}, {a[2]})",
            "add_days": lambda: f"date_add({a[0]}, {a[1]})",
            "week_ending": lambda: f"next_day(date_sub({a[0]}, 1), {a[1] if len(a) > 1 else lit('Saturday')})",
            "year": lambda: f"year({a[0]})", "month": lambda: f"month({a[0]})", "day": lambda: f"day({a[0]})",
            "iso_week": lambda: f"weekofyear({a[0]})",
        }
        if name not in m:
            raise ValueError(f"function {name} has no SQL translation")
        return m[name]()


def _coerce(x: str, dtype: str, decimal: str = ".") -> str:
    if dtype == "string":
        return f"nullif(trim(CAST({x} AS STRING)), '')"
    if dtype in ("decimal", "integer"):
        t = "DECIMAL(38,6)" if dtype == "decimal" else "BIGINT"
        v = f"trim(CAST({x} AS STRING))"
        if decimal == ",":      # value rule: 1.234,56 -> 1234.56 (approved on the Conversions tab)
            return f"try_cast(replace(replace(regexp_replace({v}, '[£$€₹\\s]', ''), '.', ''), ',', '.') AS {t})"
        return f"try_cast(replace({v}, ',', '') AS {t})"
    if dtype == "boolean":
        v = f"lower(trim(CAST({x} AS STRING)))"
        return (f"CASE WHEN {v} IN ({', '.join(map(lit, _TRUE))}) THEN TRUE "
                f"WHEN {v} IN ({', '.join(map(lit, _FALSE))}) THEN FALSE END")
    if dtype == "date":
        return f"try_cast({x} AS DATE)"
    return x


def column_sql(m: ColumnMapping, dtype: str, date_out: str | None, warnings: list[str], decimal: str = ".") -> str:
    col = lambda name: q(name)
    if m.action is None or m.action == "LOOKUP":
        # LOOKUPs (RB codes, master attributes) are filled by harmonisation in Phase 2
        return f"CAST(NULL AS {_SQL_TYPE.get(dtype, 'STRING')})"
    if m.action == "DIRECT_TEXT":
        expr = lit(m.value)
    elif m.action == "DIRECT_COLUMN":
        expr = col(m.source_columns[0])
    elif m.action == "CONCATENATION":
        if m.parts:
            expr = "concat(" + ", ".join(f"CAST({col(p.column)} AS STRING)" if p.column is not None else lit(p.text or "")
                                         for p in m.parts) + ")"
        else:
            sep = m.separator if m.separator is not None else " "
            expr = f"nullif(concat_ws({lit(sep)}, {', '.join(col(c) for c in m.source_columns)}), '')"
    elif m.action == "DATE":
        parsed = _date(col(m.source_columns[0]), strftime_to_java(m.date_format) if m.date_format else None)
        out_fmt = m.target_date_format or date_out
        expr = f"date_format({parsed}, {lit(out_fmt)})" if out_fmt else parsed
    elif m.action == "EXPRESSION":
        try:
            expr = _Expr(col)(m.expression or "null")
        except Exception as exc:
            warnings.append(f"{m.target_column}: expression not translated ({exc}); loaded as NULL")
            expr = "NULL"
        if date_out:
            expr = f"CASE WHEN typeof({expr}) IN ('date', 'timestamp') THEN date_format({expr}, {lit(date_out)}) ELSE {expr} END"
    else:
        raise ValueError(m.action)
    return _coerce(expr, dtype, decimal)


def generate(spec: MappingSpec, table: TargetTable, *, target_fqn: str, source: Source, submission_id: str,
             batch_id: str, retailer: str, source_file: str, mapping_ref: str = "",
             decimals: dict[str, str] | None = None) -> SqlBundle:
    """decimals: target column -> ',' where the value rule says the file uses decimal commas."""
    warnings: list[str] = []
    decimals = decimals or {}
    by_target = {c.target_column: c for c in spec.columns}
    logical = []
    for tc in table.columns:
        m = by_target.get(tc.name) or ColumnMapping(target_column=tc.name)
        logical.append((tc.name, column_sql(m, tc.dtype, tc.date_out, warnings, decimals.get(tc.name, "."))))
    # drop trailing total / footer rows the way the preview does: a row without any primary-key
    # source value is not a data row
    key_srcs = [c.source_columns[0] for c in spec.columns
                if (c.is_pk or c.mandatory) and c.action in ("DIRECT_COLUMN", "DATE") and c.source_columns]
    where = f"\n  WHERE {q(key_srcs[0])} IS NOT NULL AND trim(CAST({q(key_srcs[0])} AS STRING)) <> ''" if key_srcs else ""
    inner = ("SELECT\n" + ",\n".join(f"    {expr} AS {q(name)}" for name, expr in logical)
             + f"\n  FROM {source.sql()}{where}")
    if table.kind == "dimension" and table.pk:
        # one row per key in a dimension, first occurrence wins (as in the preview)
        inner = (f"SELECT * FROM (\n  {inner}\n  ) AS d\n  QUALIFY row_number() OVER "
                 f"(PARTITION BY {', '.join(q(k) for k in table.pk)} ORDER BY {q(table.pk[0])}) = 1")
    now_ts = "date_format(current_timestamp(), \"yyyy-MM-dd'T'HH:mm:ss.SSS'Z'\")"
    hash_ = "sha2(concat_ws('|', " + ", ".join(f"coalesce(CAST(mapped.{q(n)} AS STRING), '')" for n, _ in logical) + "), 256)"
    gen = {"submission_id": lit(submission_id), "retailer": lit(retailer), "insert_ts": now_ts,
           "batch_id": lit(batch_id), "source_file": lit(source_file), "record_hash": hash_}
    outer: list[tuple[str, str]] = []
    delete = None
    if table.physical:
        lay = table.physical
        phys = {lay.physical(n): f"mapped.{q(n)}" for n, _ in logical}
        for c, rule in lay.system_columns.items():
            phys[c] = lit(rule[6:]) if rule.startswith("const:") else gen[rule]
        order = (lay.order or []) + [c for c in phys if c not in (lay.order or [])]
        outer = [(c, phys.get(c, "CAST(NULL AS STRING)")) for c in order]
        sub_col = next((c for c, r in lay.system_columns.items() if r == "submission_id"), None)
        file_col = next((c for c, r in lay.system_columns.items() if r == "source_file"), None)
        if sub_col:
            delete = f"DELETE FROM {target_fqn} WHERE {q(sub_col)} = {lit(submission_id)}"
        elif file_col:
            delete = f"DELETE FROM {target_fqn} WHERE {q(file_col)} = {lit(source_file)}"
    else:
        outer = [(n, f"mapped.{q(n)}") for n, _ in logical] + [
            ("load_batch_id", gen["batch_id"]), ("load_ts", now_ts), ("source_file", gen["source_file"]),
            ("record_hash", hash_)]
        delete = f"DELETE FROM {target_fqn} WHERE {q('source_file')} = {lit(source_file)} AND {q('load_batch_id')} = {lit(batch_id)}"
    select = ("SELECT\n" + ",\n".join(f"  {expr} AS {q(c)}" for c, expr in outer)
              + f"\nFROM (\n  {inner}\n) AS mapped")
    cols = [c for c, _ in outer]
    head = [
        f"-- Retailer onboarding accelerator (Nablon AI) · generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"-- Mapping {spec.mapping_id} v{spec.version} ({spec.status}) · {spec.market}/{spec.retailer} -> {table.name}",
        f"-- Source file: {source_file} · layout {spec.source.template_fingerprint}" + (f" · {mapping_ref}" if mapping_ref else ""),
        f"-- Approved by {spec.approved_by or '-'} at {spec.approved_at or '-'} · submission {submission_id}",
        "-- Re-runnable: the DELETE removes rows of this submission before the INSERT.",
    ] + [f"-- WARNING: {w}" for w in warnings]
    sql = "\n".join(head) + "\n\n" + (delete + ";\n\n" if delete else "") + \
        f"INSERT INTO {target_fqn} ({', '.join(q(c) for c in cols)})\n{select};\n"
    return SqlBundle(sql=sql, target_fqn=target_fqn, select_sql=select, delete_sql=delete, columns=cols, warnings=warnings)


def ddl(table: TargetTable, target_fqn: str) -> str:
    """CREATE TABLE IF NOT EXISTS for the target (all physical columns as STRING, as Veritas bronze
    stores them). Used by the local pipeline runner; in Reckitt's catalog the tables already exist."""
    if table.physical:
        lay = table.physical
        cols = list(lay.order or [lay.physical(c.name) for c in table.columns])
        cols += [c for c in [lay.physical(c.name) for c in table.columns] + list(lay.system_columns) if c not in cols]
    else:
        cols = [c.name for c in table.columns] + ["load_batch_id", "load_ts", "source_file", "record_hash"]
    return f"CREATE TABLE IF NOT EXISTS {target_fqn} ({', '.join(q(c) + ' STRING' for c in cols)})"


_SAFE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}$")


def check_fqn(name: str) -> str:
    if not _SAFE.match(name or ""):
        raise ValueError(f"not a valid table name: {name!r} (expected catalog.schema.table)")
    return name

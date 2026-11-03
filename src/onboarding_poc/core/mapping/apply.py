"""Execute a mapping spec against a source frame.

Semantics follow the Veritas operators: DirectColumn, DirectText, DateFormat (source format ->
target format, written as text), Concatenation (inputs joined in order, no separator) and NULL.
Source column names resolve through canon_header(): Veritas stores 'tpnb description', the
landing zone writes 'tpnb_description', the retailer sends 'TPNB Description'.

LOOKUP columns are left empty here; harmonisation and enrichment fill them afterwards.
The output keeps the source row index so lookups can be joined back row by row.
Type coercion failures are counted per column in `frame.attrs["coercion_errors"]`.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from onboarding_poc.core.contracts import TargetTable
from onboarding_poc.core.discovery.profiler import to_number
from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.mapping.transforms import evaluate
from onboarding_poc.core.mapping.veritas import java_to_strftime
from onboarding_poc.core.text import canon_header

_TRUE = {"y", "yes", "true", "t", "1"}
_FALSE = {"n", "no", "false", "f", "0"}


class MissingSourceColumn(KeyError):
    pass


def _parse_dates(s: pd.Series, fmt: str | None) -> pd.Series:
    if fmt:
        def one(v):
            if v is None or v != v:
                return None
            try:
                return datetime.strptime(str(v).split("T")[0].strip(), fmt).date()
            except ValueError:
                return None
        return s.map(one)
    return pd.to_datetime(s, errors="coerce", dayfirst=True).dt.date


def _coerce(s: pd.Series, dtype: str) -> tuple[pd.Series, int]:
    before = s.notna()
    if dtype == "string":
        out = s.map(lambda v: None if v is None or v != v else str(v).strip() or None)
    elif dtype in ("decimal", "integer"):
        out = to_number(s)
        if dtype == "integer":
            out = out.where(out.isna() | (out % 1 == 0)).astype("Int64")
    elif dtype == "boolean":
        def b(v):
            if v is None or v != v:
                return None
            x = str(v).strip().lower()
            return True if x in _TRUE else False if x in _FALSE else pd.NA
        out = s.map(b)
    elif dtype == "date":
        out = s if s.map(lambda v: v is None or hasattr(v, "year")).all() else _parse_dates(s, None)
    else:
        out = s
    failed = int((before & pd.Series(out, index=s.index).isna()).sum())
    return out, failed


def resolver(columns) -> dict[str, str]:
    return {canon_header(c): c for c in columns}


def apply_mapping(src: pd.DataFrame, spec: MappingSpec, table: TargetTable) -> pd.DataFrame:
    out = pd.DataFrame(index=src.index)
    errors: dict[str, int] = {}
    cols = resolver(src.columns)

    def col(name: str) -> pd.Series:
        key = canon_header(name)
        if key not in cols:
            raise MissingSourceColumn(f"{spec.target_table}: source column '{name}' not in file")
        return src[cols[key]]

    for m in spec.columns:
        if not table.has(m.target_column):
            continue
        tc = table.column(m.target_column)
        if m.action is None or m.action == "LOOKUP":
            out[m.target_column] = None
            continue
        if m.action == "DIRECT_TEXT":
            series = pd.Series([m.value] * len(src), index=src.index, dtype=object)
        elif m.action == "DIRECT_COLUMN":
            series = col(m.source_columns[0])
        elif m.action == "CONCATENATION":
            if m.parts:
                pieces = [col(p.column).astype("string") if p.column is not None
                          else pd.Series([p.text or ""] * len(src), index=src.index, dtype="string")
                          for p in m.parts]
                # a missing column value makes the whole code missing, not a bare suffix
                series = pieces[0].copy()
                for p in pieces[1:]:
                    series = series + p
                series = series.astype(object).where(series.notna(), None)
            else:
                sep = m.separator if m.separator is not None else " "
                frame = pd.concat([col(c) for c in m.source_columns], axis=1)
                series = frame.apply(lambda r: sep.join(str(v) for v in r if v is not None and v == v) or None, axis=1)
        elif m.action == "DATE":
            raw = col(m.source_columns[0])
            parsed = _parse_dates(raw, m.date_format)
            errors[m.target_column] = int((raw.notna() & parsed.isna()).sum())
            target_fmt = m.target_date_format or tc.date_out
            if target_fmt:
                f = java_to_strftime(target_fmt)
                series = parsed.map(lambda d: d.strftime(f) if d is not None and d == d else None)
            else:
                series = parsed
        elif m.action == "EXPRESSION":
            series = evaluate(m.expression or "null", src, lambda name: cols.get(canon_header(name), name))
            if tc.date_out and series.map(lambda v: hasattr(v, "strftime")).any():
                f = java_to_strftime(tc.date_out)
                series = series.map(lambda d: d.strftime(f) if hasattr(d, "strftime") else d)
        else:
            raise ValueError(m.action)
        coerced, failed = _coerce(series, tc.dtype)
        out[m.target_column] = coerced
        if failed:
            errors[m.target_column] = errors.get(m.target_column, 0) + failed
    for tc in table.columns:
        if tc.name not in out.columns:
            out[tc.name] = None
    out = out[[tc.name for tc in table.columns]]
    out.attrs["coercion_errors"] = {k: v for k, v in errors.items() if v}
    return out

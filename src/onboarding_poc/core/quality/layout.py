"""Physical bronze layout (ported from s2b writer): logical frame -> bronze-ready columns."""
from __future__ import annotations

import hashlib
from datetime import datetime

import pandas as pd

from onboarding_poc.core.contracts import TargetTable


def _hash_row(values: list) -> str:
    return hashlib.sha256("|".join("" if v is None or v != v else str(v) for v in values).encode()).hexdigest()


def to_physical(df: pd.DataFrame, table: TargetTable, *, submission_id: str, retailer: str, batch_id: str,
                source_file: str, now: datetime) -> tuple[pd.DataFrame, dict[str, str]]:
    """Logical frame -> physical frame + column types (string | date | decimal | integer | boolean)."""
    lay = table.physical
    cols = [c.name for c in table.columns]
    out = df[cols].copy()
    out.columns = [lay.physical(c) for c in cols]
    types = {lay.physical(c.name): c.dtype for c in table.columns}
    gen = {
        "submission_id": lambda: submission_id,
        "retailer": lambda: retailer,
        "insert_ts": lambda: now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "batch_id": lambda: batch_id,
        "source_file": lambda: source_file,
    }
    for col, rule in lay.system_columns.items():
        if rule.startswith("const:"):
            out[col] = rule[6:]
        elif rule == "record_hash":
            out[col] = [_hash_row(r) for r in df[cols].itertuples(index=False, name=None)]
        else:
            out[col] = gen[rule]()
        types[col] = "string"
    if lay.order:
        for c in [c for c in lay.order if c not in out.columns]:
            out[c] = None
            types[c] = "string"
        out = out[lay.order + [c for c in out.columns if c not in lay.order]]
    return out, types


def to_logical(df: pd.DataFrame, table: TargetTable) -> pd.DataFrame:
    """Physical extract (e.g. current bronze) -> logical column names, matched case-insensitively."""
    if table.physical is None:
        return df
    by_upper = {str(c).upper(): c for c in df.columns}
    ren = {}
    for c in table.columns:
        phys = table.physical.physical(c.name).upper()
        if phys in by_upper:
            ren[by_upper[phys]] = c.name
    out = df.rename(columns=ren)
    return out.replace({"null": None, "NULL": None})


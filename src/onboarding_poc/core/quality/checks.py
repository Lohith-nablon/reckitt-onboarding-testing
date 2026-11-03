"""Pre-load checks. Check names follow Meridian's DQ templates (src/dq/templates.py:
completeness, uniqueness, range_check, referential_integrity, pattern_match, freshness) so
results line up with the Meridian DQ dashboard; here they run in pandas before load.

Severity `error` blocks the load of that table; `warning` is reported only.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

import pandas as pd
from pydantic import BaseModel

from onboarding_poc.core.contracts import TargetTable

Severity = Literal["error", "warning"]


class CheckResult(BaseModel):
    check: str
    template: str
    column: str | None
    severity: Severity
    passed: bool
    failed_rows: int
    total_rows: int
    detail: str = ""
    sample: list[dict] = []


def _sample(df: pd.DataFrame, mask: pd.Series, cols: list[str], n: int = 5) -> list[dict]:
    return df.loc[mask, [c for c in cols if c in df.columns]].head(n).astype(str).to_dict("records")


def run_checks(df: pd.DataFrame, table: TargetTable, master_codes: dict[str, set[str]] | None = None,
               min_harmonised_share: float = 0.95) -> list[CheckResult]:
    n = len(df)
    out: list[CheckResult] = []
    pk = table.pk
    if n == 0:
        return [CheckResult(check="row_count", template="completeness", column=None, severity="error", passed=False,
                            failed_rows=0, total_rows=0, detail="no rows produced")]
    # completeness of mandatory columns
    for c in table.columns:
        if not (c.mandatory or c.pk) or c.name not in df.columns:
            continue
        mask = df[c.name].isna()
        out.append(CheckResult(check=f"not_null:{c.name}", template="completeness", column=c.name, severity="error",
                               passed=not mask.any(), failed_rows=int(mask.sum()), total_rows=n,
                               sample=_sample(df, mask, pk)))
    # type conformance (coercion failures from mapping)
    for col, failed in (df.attrs.get("coercion_errors") or {}).items():
        out.append(CheckResult(check=f"dtype:{col}", template="pattern_match", column=col, severity="error",
                               passed=False, failed_rows=failed, total_rows=n,
                               detail=f"{failed} values could not be converted to {table.column(col).dtype}"))
    # primary key uniqueness
    if pk:
        dup = df.duplicated(subset=pk, keep=False)
        out.append(CheckResult(check="primary_key_unique", template="uniqueness", column=",".join(pk),
                               severity="error", passed=not dup.any(), failed_rows=int(dup.sum()), total_rows=n,
                               sample=_sample(df, dup, pk)))
    # measures: negatives are allowed (returns) but flagged
    for c in table.columns:
        if c.dtype in ("decimal", "integer") and c.name in df.columns:
            mask = pd.to_numeric(df[c.name], errors="coerce") < 0
            out.append(CheckResult(check=f"non_negative:{c.name}", template="range_check", column=c.name,
                                   severity="warning", passed=not mask.any(), failed_rows=int(mask.sum()),
                                   total_rows=n, detail="negative values (returns / corrections?)",
                                   sample=_sample(df, mask, pk + [c.name])))
        if c.dtype == "date" and c.name in df.columns:
            d = pd.to_datetime(df[c.name], errors="coerce")
            today = pd.Timestamp(date.today())
            mask = (d > today + timedelta(days=7)) | (d < today - timedelta(days=3 * 365))
            out.append(CheckResult(check=f"date_window:{c.name}", template="freshness", column=c.name,
                                   severity="warning", passed=not mask.any(), failed_rows=int(mask.sum()), total_rows=n,
                                   detail="dates in the future or more than 3 years old"))
    # harmonisation coverage + referential integrity against masters
    for c in table.columns:
        if not c.harmonised or c.dtype != "string" or c.name not in df.columns:
            continue                     # the RB code column (match_confidence rides along)
        missing = df[c.name].isna()
        share = 1 - missing.mean()
        out.append(CheckResult(check=f"harmonised:{c.name}", template="completeness", column=c.name,
                               severity="warning", passed=share >= min_harmonised_share,
                               failed_rows=int(missing.sum()), total_rows=n,
                               detail=f"{share:.1%} of rows carry an accepted RB code; the rest wait for review"))
        codes = (master_codes or {}).get(c.harmonised)
        if codes:
            bad = df[c.name].notna() & ~df[c.name].astype(str).isin(codes)
            out.append(CheckResult(check=f"in_master:{c.name}", template="referential_integrity", column=c.name,
                                   severity="error", passed=not bad.any(), failed_rows=int(bad.sum()), total_rows=n,
                                   sample=_sample(df, bad, pk + [c.name])))
    return out


def blocking(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.severity == "error" and not r.passed]

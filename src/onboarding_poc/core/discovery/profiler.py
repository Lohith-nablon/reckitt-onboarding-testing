"""Column profiling on string frames. Output follows Meridian's DataProfile / ColumnProfile
contract (src/schemas/discovery.py), with value-pattern detection tuned for retailer files."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

from onboarding_poc.core.schemas import ColumnInfo, ColumnProfile, DataProfile
from onboarding_poc.core.text import normalise_gtin

DATE_FORMATS = ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%Y%m%d",
                "%d/%m/%y", "%m/%d/%y", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%Y/%m/%d"]
_INT = re.compile(r"^-?\d+$")
_DEC = re.compile(r"^-?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?$")
_UK_PC = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.IGNORECASE)
_IN_PIN = re.compile(r"^\d{6}$")
_FLAG = {"y", "n", "yes", "no", "true", "false", "0", "1", "t", "f"}
_SCI = re.compile(r"^\d(\.\d+)?e\+\d+$", re.IGNORECASE)


def detect_date_format(values: list[str], dayfirst: bool = True) -> tuple[str | None, float, bool]:
    """Return (format, parse_rate, ambiguous). Ambiguous = day/month order can't be told from data."""
    sample = [v for v in values if v][:500]
    if not sample:
        return None, 0.0, False
    rates = {}
    for fmt in DATE_FORMATS:
        ok = 0
        for v in sample:
            try:
                datetime.strptime(v.split("T")[0].strip(), fmt)
                ok += 1
            except ValueError:
                pass
        rates[fmt] = ok / len(sample)
    best = max(rates.values())
    if best < 0.8:
        return None, best, False
    winners = [f for f, r in rates.items() if r == best]
    if len(winners) == 1:
        return winners[0], best, False
    pref = [f for f in winners if f.startswith("%d")] if dayfirst else [f for f in winners if f.startswith("%m")]
    return (pref or winners)[0], best, True


def to_number(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype("string").str.replace(",", "", regex=False).str.strip(), errors="coerce")


def _infer(values: pd.Series, dayfirst: bool) -> tuple[str, str | None, float | None]:
    vals = values.dropna().astype(str)
    if vals.empty:
        return "string", None, None
    n = len(vals)
    sample = vals.head(2000)
    lower = sample.str.lower()
    if lower.isin(_FLAG).mean() > 0.98 and 2 <= sample.nunique() <= 4 and len(sample) >= 10:
        return "boolean", "flag", float(lower.isin(_FLAG).mean())
    gt = sample.map(lambda v: normalise_gtin(v) is not None)
    sci = sample.str.match(_SCI)
    if (gt | sci).mean() > 0.6 and gt.mean() > 0.3 and sample.str.len().median() >= 8:
        return "string", "gtin", float(gt.mean())
    fmt, rate, _ = detect_date_format(sample.tolist(), dayfirst)
    if fmt and rate >= 0.95:
        return "date", f"date:{fmt}", rate
    if sample.str.match(_UK_PC).mean() > 0.9:
        return "string", "uk_postcode", float(sample.str.match(_UK_PC).mean())
    if sample.str.match(_INT).mean() > 0.98:
        lens = sample.str.len()
        if sample.str.match(_IN_PIN).mean() > 0.95:
            return "string", "in_pincode", float(sample.str.match(_IN_PIN).mean())
        if (sample.str.startswith("0") & (lens > 1)).any() or lens.median() >= 7:
            return "string", "numeric_code", 1.0
        return "integer", None, None
    if sample.str.match(_DEC).mean() > 0.98:
        return "decimal", None, None
    if sample.str.match(r"^[A-Za-z]{0,4}\d+[A-Za-z0-9-]*$").mean() > 0.95 and sample.str.len().max() <= 20:
        return "string", "alnum_code", 1.0
    del n
    return "string", None, None


def profile_frame(df: pd.DataFrame, fqn: str, dayfirst: bool = True,
                  top_n: int = 10) -> tuple[DataProfile, list[ColumnInfo]]:
    profiles, infos = [], []
    rows = len(df)
    for col in df.columns:
        s = df[col]
        nulls = int(s.isna().sum())
        dtype, pattern, rate = _infer(s, dayfirst)
        nn = s.dropna()
        prof = ColumnProfile(
            column=col, data_type=dtype, row_count=rows, null_count=nulls,
            null_rate=round(nulls / rows, 4) if rows else 0.0, distinct_count=int(nn.nunique()),
            top_values=[(str(k), int(v)) for k, v in nn.value_counts().head(top_n).items()],
            pattern_detected=pattern, pattern_match_rate=round(rate, 4) if rate is not None else None,
        )
        if dtype in ("integer", "decimal"):
            num = to_number(nn).dropna()
            if len(num):
                prof.min_value, prof.max_value = float(num.min()), float(num.max())
                prof.mean = round(float(num.mean()), 4)
                prof.stddev = round(float(num.std()), 4) if len(num) > 1 else 0.0
                q = num.quantile([0.25, 0.5, 0.75])
                prof.p25, prof.p50, prof.p75 = (round(float(x), 4) for x in q)
        elif len(nn):
            prof.min_value, prof.max_value = str(nn.min()), str(nn.max())
        profiles.append(prof)
        infos.append(ColumnInfo(
            name=col, data_type=dtype, is_nullable=nulls > 0, null_pct=prof.null_rate,
            cardinality=prof.distinct_count,
            min_value=None if prof.min_value is None else str(prof.min_value),
            max_value=None if prof.max_value is None else str(prof.max_value),
        ))
    dp = DataProfile(source_type="file", fully_qualified_name=fqn, profiled_at=datetime.now(timezone.utc),
                     sample_fraction=1.0, column_profiles=profiles)
    return dp, infos

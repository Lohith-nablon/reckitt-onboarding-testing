"""Fill LOOKUP columns: RB codes from harmonisation, global attributes from the masters.

Only accepted matches (auto_accepted / approved / crosswalk) are written. Codes that are
still under review stay NULL, so nothing unconfirmed reaches bronze.
"""
from __future__ import annotations

import pandas as pd

from onboarding_poc.core.contracts import Contracts, TargetTable
from onboarding_poc.core.harmonisation.models import MatchResult
from onboarding_poc.core.mapping.apply import resolver
from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.text import canon_header

ACCEPTED = {"auto_accepted", "approved"}


def accepted_codes(matches: list[MatchResult]) -> dict[str, tuple[str, float]]:
    return {m.source_key: (m.rb_code, m.confidence) for m in matches if m.status in ACCEPTED and m.rb_code}


def fill_lookups(target: pd.DataFrame, src: pd.DataFrame, spec: MappingSpec, table: TargetTable,
                 product_codes: dict[str, tuple[str, float]], outlet_codes: dict[str, tuple[str, float]],
                 masters: dict[str, pd.DataFrame], contracts: Contracts) -> pd.DataFrame:
    out = target.copy()
    cols = resolver(src.columns)
    # 1. harmonised codes, keyed on the raw retailer code (first key column)
    for m in spec.columns:
        if m.action != "LOOKUP" or not m.lookup or not m.lookup.name.startswith("harmonisation."):
            continue
        codes = product_codes if m.lookup.name.endswith("product") else outlet_codes
        key_col = cols.get(canon_header(m.lookup.key_columns[0]))
        if key_col is None:
            continue
        idx = 1 if m.lookup.return_column == "confidence" else 0
        out[m.target_column] = src[key_col].map(
            lambda v: codes[str(v)][idx] if v is not None and v == v and str(v) in codes else None)
    # 2. master attributes, joined on the RB code
    for m in spec.columns:
        if m.action != "LOOKUP" or not m.lookup or not m.lookup.name.startswith("master."):
            continue
        master = m.lookup.name.split(".", 1)[1]
        mdf = masters.get(master)
        if mdf is None or m.lookup.key_columns[0] not in out.columns:
            continue
        key = contracts.masters[master].key
        lut = mdf.drop_duplicates(key).set_index(key)[m.lookup.return_column]
        out[m.target_column] = out[m.lookup.key_columns[0]].map(
            lambda v: lut.get(v) if v is not None and v == v else None)
    return out

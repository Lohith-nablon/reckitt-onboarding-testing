"""Fact / dimension classification, grain detection and target-table proposals."""
from __future__ import annotations

from itertools import combinations

import pandas as pd

from onboarding_poc.core.contracts import Catalogue, Contracts, TargetTable
from onboarding_poc.core.schemas import ColumnHint, TargetProposal

_OUTLET_DETAIL = {"outlet_name", "address_line", "city", "postcode"}
_PRODUCT_DETAIL = {"product_description", "ean", "brand_name", "pack_size_text"}


def types_present(hints: dict[str, ColumnHint], min_conf: float = 0.5) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for col, h in hints.items():
        if h.semantic_type and h.confidence >= min_conf:
            out.setdefault(h.semantic_type, []).append(col)
    return out


def classify(hints: dict[str, ColumnHint], catalogue: Catalogue) -> tuple[str, float, str | None]:
    """Return (classification, confidence, entity)."""
    present = types_present(hints)
    roles: dict[str, int] = {}
    for t in present:
        role = catalogue.types[t].role if t in catalogue.types else None
        if role:
            roles[role] = roles.get(role, 0) + 1
    has_measure = roles.get("measure", 0) > 0
    has_time = roles.get("time", 0) > 0
    product = roles.get("product_attr", 0)
    outlet = roles.get("outlet_attr", 0)
    if has_measure and has_time:
        entity = "mixed" if (set(present) & _OUTLET_DETAIL and set(present) & _PRODUCT_DETAIL) else None
        return "fact", 0.95, entity
    if has_measure:
        return "fact", 0.7, None
    if outlet > product and outlet >= 2:
        return "dimension", min(0.95, 0.6 + 0.1 * outlet), "outlet"
    if product > outlet and product >= 2:
        return "dimension", min(0.95, 0.6 + 0.1 * product), "product"
    return "unknown", 0.3, None


def find_grain(df: pd.DataFrame, hints: dict[str, ColumnHint], max_cols: int = 4) -> list[str]:
    """Smallest combination of key-like columns that is unique across the rows."""
    priority = ["week_ending", "retailer_sku_code", "ean", "retailer_outlet_code", "product_description",
                "outlet_name", "promotion_flag"]
    keyish = []
    for t in priority:
        keyish += [c for c, h in hints.items() if h.semantic_type == t and c not in keyish]
    if not keyish or df.empty:
        return []
    for k in range(1, min(max_cols, len(keyish)) + 1):
        for combo in combinations(keyish, k):
            sub = df[list(combo)]
            if sub.notna().all(axis=1).all() and not sub.duplicated().any():
                return list(combo)
    return []


def _derivable(table: TargetTable, present: set[str]) -> tuple[list[str], list[str]]:
    covered, missing = [], []
    product_key = bool(present & {"retailer_sku_code", "ean", "product_description"})
    outlet_key = "retailer_outlet_code" in present or bool(present & _OUTLET_DETAIL)
    for c in table.columns:
        ok = False
        if c.context:
            ok = True
        elif c.semantic and c.semantic in present:
            ok = True
        elif c.harmonised == "product" or (c.enrich and c.enrich.master == "erp_product"):
            ok = product_key
        elif c.harmonised == "outlet" or (c.enrich and c.enrich.master == "rb_customer"):
            ok = outlet_key
        (covered if ok else missing).append(c.name)
    return covered, missing


def propose_targets(hints: dict[str, ColumnHint], classification: str, entity: str | None,
                    contracts: Contracts) -> list[TargetProposal]:
    present = set(types_present(hints))
    proposals: list[TargetProposal] = []
    for t in contracts.tables.values():
        covered, missing = _derivable(t, present)
        weights = {c.name: (2.0 if (c.mandatory or c.pk) else 1.0) for c in t.columns}
        score = sum(weights[c] for c in covered) / sum(weights.values())
        missing_mand = [c for c in missing if t.column(c).mandatory or t.column(c).pk]
        proposals.append(TargetProposal(target_table=t.name, score=round(score, 3), primary=False,
                                        covered_columns=covered, missing_mandatory=missing_mand))
    kind_match = [p for p in proposals if contracts.tables[p.target_table].kind ==
                  ("fact" if classification == "fact" else "dimension")]
    if classification == "dimension" and entity:
        kind_match = [p for p in kind_match if contracts.tables[p.target_table].entity == entity] or kind_match
    primary = max(kind_match or proposals, key=lambda p: (not p.missing_mandatory, p.score))
    primary.primary = True
    result = [primary]
    # A fact file that carries product / outlet attributes can also feed the dimension tables
    for p in proposals:
        if p is primary or p.missing_mandatory:
            continue
        t = contracts.tables[p.target_table]
        detail = _OUTLET_DETAIL if t.entity == "outlet" else _PRODUCT_DETAIL if t.entity == "product" else set()
        # products: a description is enough to harmonise; outlets need more than a name
        need = 1 if t.entity == "product" else 2
        if t.kind == "dimension" and len(present & detail) >= need:
            result.append(p)
    return result

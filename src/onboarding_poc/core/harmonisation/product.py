"""Product harmonisation: retailer SKU -> RB product code.

Tier 0  crosswalk        approved earlier (HITL or previous runs)             confidence 1.00
Tier 1  universal code   valid GTIN found in the ERP product master           confidence 0.99
                         (lowered if the ERP description clearly disagrees: retailer may reuse barcodes)
Tier 2  description      brand blocking, then text similarity x size/count agreement
Tier 3  LLM              optional tie-breaker for the review band only
Every result carries the top-3 candidates so reviewers pick instead of searching.
"""
from __future__ import annotations

import pandas as pd
from pydantic import BaseModel
from rapidfuzz import fuzz

from onboarding_poc.core.harmonisation.models import Candidate, MatchResult, band
from onboarding_poc.core.harmonisation.normalise import ProductAttrs, normalise_product
from onboarding_poc.llm import LLM
from onboarding_poc.core.text import normalise_gtin


class _LLMChoice(BaseModel):
    code: str | None = None
    confidence: float = 0.0


def _size_agreement(a: ProductAttrs, b: ProductAttrs) -> float:
    if a.size is not None and b.size is not None:
        if a.size_unit != b.size_unit:
            return 0.3
        return 1.0 if abs(a.size - b.size) <= 0.02 * max(a.size, b.size) else 0.0
    if a.count is not None and b.count is not None:
        return 1.0 if a.count == b.count else 0.0
    if (a.size is None and a.count is None) or (b.size is None and b.count is None):
        return 0.6
    return 0.3  # one has a size, the other a count


class ProductMatcher:
    def __init__(self, erp: pd.DataFrame, cols: dict[str, str], abbreviations: dict[str, str],
                 auto_accept: float, review_floor: float, llm: LLM | None = None):
        self.abbrev = abbreviations
        self.auto_accept, self.review_floor = auto_accept, review_floor
        self.llm = llm
        c_code, c_gtin, c_desc, c_brand = cols["code"], cols["universal_code"], cols["description"], cols["brand"]
        self.rows = []
        self.by_gtin: dict[str, int] = {}
        for i, r in erp.reset_index(drop=True).iterrows():
            attrs = normalise_product(r[c_desc], abbreviations)
            brand = str(r.get(c_brand) or "").lower()
            self.rows.append({"code": str(r[c_code]), "desc": r[c_desc], "attrs": attrs, "brand": brand,
                              "brand_key": brand.replace(" ", "")})
            g = normalise_gtin(r.get(c_gtin))
            if g:
                self.by_gtin[g] = i
        self.brands = sorted({r["brand_key"] for r in self.rows if r["brand_key"]}, key=len, reverse=True)

    def _brand_of(self, a: ProductAttrs) -> str | None:
        squashed = a.text.replace(" ", "")
        for b in self.brands:
            if squashed.startswith(b) or f" {b} " in f" {a.text} ":
                return b
        return None

    def _score(self, a: ProductAttrs, row: dict) -> tuple[float, dict]:
        b = row["attrs"]
        text = (0.6 * fuzz.token_set_ratio(a.text, b.text) + 0.4 * fuzz.token_sort_ratio(a.text, b.text)) / 100
        size = _size_agreement(a, b)
        score = text * (0.55 + 0.45 * size)
        return round(score, 4), {"text": round(text, 3), "size_agreement": size}

    def _candidates(self, a: ProductAttrs, k: int = 5) -> list[Candidate]:
        brand = self._brand_of(a)
        pool = [r for r in self.rows if r["brand_key"] == brand] if brand else self.rows
        scored = []
        for r in pool or self.rows:
            s, detail = self._score(a, r)
            scored.append(Candidate(code=r["code"], label=r["desc"], score=s,
                                    detail={**detail, "brand_block": brand}))
        scored.sort(key=lambda c: -c.score)
        return scored[:k]

    def match_one(self, market: str, retailer: str, sku: str, ean: str | None, desc: str | None,
                  crosswalk: dict[str, str]) -> MatchResult:
        base = dict(entity="product", market=market, retailer=retailer, source_key=sku,
                    source_label=desc or ean or sku, source_attrs={"ean": ean, "description": desc})
        if sku in crosswalk:
            return MatchResult(**base, rb_code=crosswalk[sku], confidence=1.0, method="crosswalk",
                               status="auto_accepted", reason="approved crosswalk")
        a = normalise_product(desc, self.abbrev)
        g = normalise_gtin(ean)
        if g and g in self.by_gtin:
            row = self.rows[self.by_gtin[g]]
            conf, reason = 0.99, f"GTIN {g} found in ERP master"
            if a.text:
                s, _ = self._score(a, row)
                if s < 0.45:
                    conf, reason = 0.75, f"GTIN {g} matches ERP but description disagrees (score {s:.2f})"
            return MatchResult(**base, rb_code=row["code"], confidence=conf, method="universal_code",
                               status=band(conf, self.auto_accept, self.review_floor), reason=reason,
                               candidates=[Candidate(code=row["code"], label=row["desc"], score=conf)])
        if not a.text:
            return MatchResult(**base, rb_code=None, confidence=0.0, method="none", status="unmatched",
                               reason="no valid EAN and no description")
        cands = self._candidates(a)
        top = cands[0]
        conf = top.score
        reason = f"description match (text {top.detail['text']:.2f}, size agreement {top.detail['size_agreement']})"
        if len(cands) > 1 and top.score - cands[1].score < 0.03:
            conf *= 0.85
            reason += f"; close second {cands[1].code}"
        method = "description_attributes"
        if self.review_floor <= conf < self.auto_accept and self.llm and self.llm.available:
            pick = self.llm.structured(
                "A retailer product must be matched to the manufacturer's ERP product master.\n"
                f"Retailer description: {desc}\nCandidates:\n" +
                "\n".join(f"- {c.code}: {c.label}" for c in cands) +
                "\nReturn the matching code (or null if none is the same product: brand, variant, size and "
                "pack count must all agree) with confidence 0-1.", _LLMChoice, kind="product_adjudication")
            if pick and pick.code == top.code and pick.confidence >= 0.8:
                conf, method = max(conf, 0.9), "llm_adjudicated"
                reason += "; LLM confirmed top candidate"
            elif pick and pick.code and pick.code != top.code:
                reason += f"; LLM preferred {pick.code}"
        status = band(conf, self.auto_accept, self.review_floor)
        return MatchResult(**base, rb_code=top.code if status != "unmatched" else None, confidence=round(conf, 3),
                           method=method, status=status, candidates=cands, reason=reason)

    def match(self, products: pd.DataFrame, market: str, retailer: str,
              crosswalk: dict[str, str] | None = None) -> list[MatchResult]:
        """`products` columns: retailer_sku_code, ean (optional), product_description (optional)."""
        cw = crosswalk or {}
        out = []
        for _, r in products.iterrows():
            out.append(self.match_one(market, retailer, str(r["retailer_sku_code"]), r.get("ean"),
                                      r.get("product_description"), cw))
        return out

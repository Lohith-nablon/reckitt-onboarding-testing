"""Outlet harmonisation: retailer outlet code -> RB outlet code (customer master).

Tier 0  crosswalk  approved earlier                                         confidence 1.00
Tier 1  address    candidates restricted to the market + retailer banner, blocked by
                   exact postcode, then outward code / PIN prefix, then city, then whole banner.
                   Score = weighted name, address, city and postcode similarity over the fields
                   both sides have. Blocking level and margin over the runner-up adjust confidence.
"""
from __future__ import annotations

import pandas as pd
from rapidfuzz import fuzz

from onboarding_poc.core.harmonisation.models import Candidate, MatchResult, band
from onboarding_poc.core.harmonisation.normalise import normalise_address, normalise_postcode, outward_code

_WEIGHTS = {"name": 0.35, "address": 0.30, "city": 0.15, "postcode": 0.20}
_BLOCK_FACTOR = {"postcode": 1.0, "outward": 0.95, "city": 0.9, "banner": 0.8}


class OutletMatcher:
    def __init__(self, customers: pd.DataFrame, cols: dict[str, str], auto_accept: float, review_floor: float):
        self.auto_accept, self.review_floor = auto_accept, review_floor
        self.rows = []
        for _, r in customers.iterrows():
            pc = normalise_postcode(r.get(cols["postcode"]))
            self.rows.append({
                "code": str(r[cols["code"]]), "name_raw": r.get(cols["name"]),
                "name": normalise_address(r.get(cols["name"])), "banner": str(r.get(cols["banner"]) or ""),
                "market": str(r.get(cols["market"]) or "").upper(),
                "address": normalise_address(r.get(cols["address"])), "city": normalise_address(r.get(cols["city"])),
                "pc": pc, "outward": outward_code(pc),
            })

    def _pool(self, market: str, banner: str) -> list[dict]:
        b = banner.lower()
        pool = [r for r in self.rows if r["market"] == market.upper() and r["banner"].lower() == b]
        return pool or [r for r in self.rows if r["market"] == market.upper()]

    @staticmethod
    def _score(src: dict, row: dict, banner_word: str) -> tuple[float, dict]:
        parts: dict[str, float] = {}
        if src["name"]:
            a = src["name"].replace(banner_word, " ")
            b = row["name"].replace(banner_word, " ")
            parts["name"] = fuzz.token_set_ratio(a, b) / 100
        if src["address"] and row["address"]:
            parts["address"] = fuzz.token_set_ratio(src["address"], row["address"]) / 100
        if src["city"] and row["city"]:
            parts["city"] = fuzz.ratio(src["city"], row["city"]) / 100
        if src["pc"] and row["pc"]:
            parts["postcode"] = 1.0 if src["pc"] == row["pc"] else (
                0.6 if src["outward"] == row["outward"] else 0.8 * fuzz.ratio(src["pc"], row["pc"]) / 100)
        if not parts:
            return 0.0, {}
        w = sum(_WEIGHTS[k] for k in parts)
        score = sum(_WEIGHTS[k] * v for k, v in parts.items()) / w
        # name-only evidence is weak
        if set(parts) == {"name"}:
            score *= 0.8
        return score, {k: round(v, 3) for k, v in parts.items()}

    def match_one(self, market: str, retailer: str, banner: str, code: str, attrs: dict,
                  crosswalk: dict[str, str]) -> MatchResult:
        base = dict(entity="outlet", market=market, retailer=retailer, source_key=code,
                    source_label=attrs.get("outlet_name") or code, source_attrs=attrs)
        if code in crosswalk:
            return MatchResult(**base, rb_code=crosswalk[code], confidence=1.0, method="crosswalk",
                               status="auto_accepted", reason="approved crosswalk")
        src = {"name": normalise_address(attrs.get("outlet_name")), "address": normalise_address(attrs.get("address_line")),
               "city": normalise_address(attrs.get("city")), "pc": normalise_postcode(attrs.get("postcode"))}
        src["outward"] = outward_code(src["pc"])
        pool = self._pool(market, banner)
        if not pool:
            return MatchResult(**base, rb_code=None, confidence=0.0, method="none", status="unmatched",
                               reason=f"no customers for market {market}")
        level, block = "banner", pool
        for lvl, pred in (("postcode", lambda r: src["pc"] and r["pc"] == src["pc"]),
                          ("outward", lambda r: src["outward"] and r["outward"] == src["outward"]),
                          ("city", lambda r: src["city"] and fuzz.ratio(r["city"], src["city"]) >= 85)):
            hit = [r for r in pool if pred(r)]
            if hit:
                level, block = lvl, hit
                break
        banner_word = normalise_address(banner)
        cands = []
        for r in block:
            s, detail = self._score(src, r, banner_word)
            cands.append(Candidate(code=r["code"], label=f"{r['name_raw']} ({r['pc']})", score=round(s, 4),
                                   detail={**detail, "block": level}))
        cands.sort(key=lambda c: -c.score)
        cands = cands[:5]
        top = cands[0]
        conf = top.score * _BLOCK_FACTOR[level]
        reason = f"blocked on {level}; " + ", ".join(f"{k} {v:.2f}" for k, v in top.detail.items() if k != "block")
        if level == "postcode" and len(block) == 1:
            # the only outlet of this banner at that postcode: strong evidence even if the
            # customer master uses a legal entity name the retailer never prints
            conf = max(conf, 0.8 + 0.2 * top.detail.get("name", 0.0))
            reason += "; only banner outlet at this postcode"
        if len(cands) > 1:
            margin = top.score - cands[1].score
            if margin < 0.05:
                conf *= 0.85
                reason += f"; close second {cands[1].code}"
        status = band(conf, self.auto_accept, self.review_floor)
        return MatchResult(**base, rb_code=top.code if status != "unmatched" else None, confidence=round(conf, 3),
                           method="address", status=status, candidates=cands, reason=reason)

    def match(self, outlets: pd.DataFrame, market: str, retailer: str, banner: str,
              crosswalk: dict[str, str] | None = None) -> list[MatchResult]:
        """`outlets` columns: retailer_outlet_code + any of outlet_name, address_line, city, postcode."""
        cw = crosswalk or {}
        out = []
        for _, r in outlets.iterrows():
            attrs = {k: r.get(k) for k in ("outlet_name", "address_line", "city", "postcode") if k in r and r.get(k) == r.get(k)}
            out.append(self.match_one(market, retailer, banner, str(r["retailer_outlet_code"]), attrs, cw))
        return out

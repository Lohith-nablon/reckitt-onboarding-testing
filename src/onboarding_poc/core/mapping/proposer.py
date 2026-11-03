"""Propose a source-to-target mapping for one sheet and one target table.

Evidence, strongest first:
  1. Conventions from existing Veritas mappings (same retailer, then same market, then any):
     constants (legal / mgmt entity, transaction_id, uom, currency), code suffixes
     (tpnb + "_Tesco"), and which source header fed which dataset column.
  2. Semantic hints from Discovery (header synonyms, fuzzy match, value patterns, LLM).
  3. Market / retailer defaults in markets.yaml.
  4. Fuzzy name match or LLM for anything mandatory that is still empty.

Action per target column:
  context constant                    -> DIRECT_TEXT
  retailer code with a suffix         -> CONCATENATION [source column, suffix text]
  date column                         -> DATE (source format detected, output format from the contract)
  harmonised / enriched               -> LOOKUP (accelerator-owned tables only; not in Veritas datasets)
  several address-type columns        -> CONCATENATION
  otherwise                           -> DIRECT_COLUMN
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

import pandas as pd
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from onboarding_poc.core.contracts import Catalogue, MarketConfig, TargetColumn, TargetTable
from onboarding_poc.core.discovery.profiler import detect_date_format
from onboarding_poc.llm import LLM
from onboarding_poc.core.mapping.models import ColumnMapping, ConcatPart, LookupSpec, MappingSpec, SourceSpec
from onboarding_poc.core.mapping.veritas import conventions as veritas_conventions
from onboarding_poc.core.mapping.veritas import java_to_strftime
from onboarding_poc.core.schemas import SheetDiscovery
from onboarding_poc.core.text import canon_header, currency_in_header, normalise_header

_PRODUCT_KEYS = ["retailer_sku_code", "ean", "product_description"]
_OUTLET_KEYS = ["retailer_outlet_code", "outlet_name", "address_line", "city", "postcode"]
_FEEDS = ["daily", "weekly", "monthly"]


class _LLMPick(BaseModel):
    source_column: str | None = Field(default=None)
    confidence: float = 0.0


def _feed_of(text: str) -> str | None:
    t = text.lower()
    return next((f for f in _FEEDS if f in t), None)


@dataclass
class Conventions:
    """What existing Veritas mappings say, grouped by how close they are to this file."""
    tiers: list[tuple[str, float, list[dict]]] = field(default_factory=list)   # (label, confidence, docs)
    retailer_display: str = ""

    @classmethod
    def build(cls, docs: list[dict], market: str | None, retailer: str | None, market_of,
              retailer_display: str = "") -> "Conventions":
        same_r, same_m, other = [], [], []
        for d in docs:
            r = str((d.get("Metadata") or {}).get("RetailerName") or "").upper().replace(" ", "").replace("'", "")
            if retailer and r == retailer.upper():
                same_r.append(d)
            elif market and market_of(d.get("LEGC")) == market:
                same_m.append(d)
            else:
                other.append(d)
        return cls([("same retailer", 0.95, same_r), ("same market", 0.85, same_m), ("other market", 0.7, other)],
                   retailer_display or (retailer or "").title())

    def _first(self, column: str, key: str):
        for label, conf, docs in self.tiers:
            for d in docs:
                c = veritas_conventions(d).get(column)
                if c and key in c:
                    return c, conf, f"Veritas #{d.get('id')} {d.get('Name')} ({label})", d
        return None

    def constant(self, column: str, same_retailer_only: bool = False):
        hit = self._first(column, "text")
        if not hit or (same_retailer_only and hit[1] < 0.95):
            return None
        c, conf, why, _ = hit
        return c["text"], conf, why

    def suffix(self, column: str):
        """Same retailer: reuse its suffix. Otherwise derive one from another retailer's pattern
        (_Tesco -> _DMart), at low confidence so a person confirms it."""
        hit = self._first(column, "suffix")
        if not hit:
            return None
        c, conf, why, doc = hit
        if conf >= 0.95:
            return c["suffix"], conf, why, doc
        their = str((doc.get("Metadata") or {}).get("RetailerName") or "")
        if their and self.retailer_display and their.lower() in c["suffix"].lower():
            derived = re.sub(re.escape(their), self.retailer_display, c["suffix"], flags=re.IGNORECASE)
            return derived, 0.55, f"pattern of Veritas #{doc.get('id')} ({their} uses '{c['suffix']}')", doc
        return None

    def source_columns(self, column: str, headers: dict[str, str]):
        for label, conf, docs in self.tiers:
            for d in docs:
                c = veritas_conventions(d).get(column)
                if c and "column" in c and c["column"] in headers:
                    return headers[c["column"]], min(0.97, conf + 0.02), \
                        f"'{headers[c['column']]}' feeds {column} in Veritas #{d.get('id')} ({label})", c
        return None


def _parses(v: str, fmt: str) -> bool:
    from datetime import datetime
    try:
        datetime.strptime(v.split("T")[0].strip(), fmt)
        return True
    except ValueError:
        return False


def _cols_by_type(sd: SheetDiscovery, min_conf: float = 0.5) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    for col, h in sd.column_hints.items():
        if h.semantic_type and h.confidence >= min_conf:
            out.setdefault(h.semantic_type, []).append((col, h.confidence))
    for v in out.values():
        v.sort(key=lambda x: -x[1])
    return out


def _file_pattern(name: str) -> str:
    """tesco_weekly_epos_2026w40.xlsx -> tesco_weekly_epos_*.xlsx"""
    stem, dot, ext = name.rpartition(".")
    stem = re.sub(r"[\d]{2,}[\dA-Za-z-]*$", "*", stem)
    return f"{stem}{dot}{ext}" if dot else name


class MappingProposer:
    def __init__(self, catalogue: Catalogue, llm: LLM | None = None):
        self.catalogue = catalogue
        self.llm = llm

    def propose(self, sd: SheetDiscovery, frame: pd.DataFrame, target: TargetTable, mcfg: MarketConfig,
                conv: Conventions | None = None, footer_rows: int = 0, file_format: str = "csv",
                profile: dict[str, dict] | None = None,
                delimiter: str | None = None) -> MappingSpec:
        conv = conv or Conventions()
        self._profile = profile or {}
        by_type = _cols_by_type(sd)
        headers = {canon_header(c): c for c in frame.columns}
        used: set[str] = set()
        retailer = sd.source.retailer
        cols: list[ColumnMapping] = []
        for tc in target.columns:
            m = ColumnMapping(target_column=tc.name, mandatory=tc.mandatory, is_pk=tc.pk)
            if tc.harmonised:
                self._harmonised(m, tc, by_type)
            elif tc.enrich:
                key = "rb_product_code" if tc.enrich.master == "erp_product" else "rb_outlet_code"
                m.action = "LOOKUP"
                m.lookup = LookupSpec(name=f"master.{tc.enrich.master}", key_columns=[key],
                                      return_column=tc.enrich.column)
                m.confidence, m.rationale = 1.0, f"{tc.enrich.master}.{tc.enrich.column} joined on {key}"
            elif tc.code_suffix:
                self._code(m, tc, by_type, headers, conv, mcfg, retailer, sd.source.name, used)
            elif tc.semantic or tc.context:
                if tc.semantic:
                    self._source(m, tc, by_type, headers, conv, sd, frame, mcfg, used)
                if m.action is None and tc.context:
                    self._context(m, tc, sd, conv, mcfg, retailer)
            else:
                # free slots (supplier, measure_n, attribute_n): only filled when an existing mapping did
                hit = conv.source_columns(tc.name, headers)
                if hit:
                    m.action, m.source_columns, m.confidence, m.rationale = "DIRECT_COLUMN", [hit[0]], hit[1], hit[2]
            cols.append(m)
        for m in cols:
            if m.action is None and (m.mandatory or m.is_pk) and not target.column(m.target_column).context:
                self._fallback(m, target, sd, used)
        return MappingSpec(
            mapping_id=str(uuid.uuid4()), market=sd.source.market or "NA", retailer=retailer or "NA",
            source=SourceSpec(file_pattern=_file_pattern(sd.source.name), sheet=sd.sheet,
                              header_row=sd.header.header_row, footer_rows=footer_rows, file_format=file_format,
                              delimiter=delimiter, template_fingerprint=sd.template_fingerprint,
                              source_columns=list(frame.columns)),
            target_table=target.name, columns=cols,
        )

    # ── per-column strategies ──────────────────────────────────────────────

    @staticmethod
    def _harmonised(m: ColumnMapping, tc: TargetColumn, by_type) -> None:
        keys = _PRODUCT_KEYS if tc.harmonised == "product" else _OUTLET_KEYS
        key_cols = [c for t in keys for c, _ in by_type.get(t, [])[:1]]
        if key_cols:
            m.action = "LOOKUP"
            ret = "confidence" if tc.name == "match_confidence" else "rb_code"
            m.lookup = LookupSpec(name=f"harmonisation.{tc.harmonised}", key_columns=key_cols, return_column=ret)
            m.source_columns, m.confidence = key_cols, 1.0
            m.rationale = f"{tc.harmonised} harmonisation on {', '.join(key_cols)}"

    def _code(self, m, tc, by_type, headers, conv: Conventions, mcfg, retailer, file_name, used) -> None:
        hit = conv.source_columns(tc.name, headers)
        if hit:
            col, conf, why = hit[0], hit[1], hit[2]
        else:
            cands = by_type.get(tc.semantic, [])
            if not cands:
                return
            col, conf = cands[0]
            why = f"'{col}' tagged {tc.semantic}"
        used.add(col)
        prof = self._from_profile(tc.code_suffix)
        suffix = None if prof else conv.suffix(tc.name)
        if prof:
            text, sconf, swhy = prof
        elif suffix:
            text, sconf, swhy, doc = suffix
            src_feed, file_feed = _feed_of(text), _feed_of(file_name)
            if src_feed and file_feed and src_feed != file_feed:
                text = re.sub(src_feed, file_feed.title(), text, flags=re.IGNORECASE)
                sconf, swhy = 0.6, f"{swhy}; feed word changed to {file_feed.title()}: confirm"
            elif src_feed and not file_feed:
                sconf, swhy = min(sconf, 0.6), f"{swhy}; suffix names the {src_feed} feed: confirm for this file"
        else:
            text = mcfg.value(tc.code_suffix, retailer)
            sconf, swhy = (0.8, "suffix from markets.yaml") if text else (0.0, "")
        if text:
            m.action, m.parts = "CONCATENATION", [ConcatPart(column=col), ConcatPart(text=text)]
            m.source_columns = [col]
            m.confidence = round(min(conf, sconf), 3)
            m.rationale = f"{why}; suffix '{text}' from {swhy}"
        else:
            m.action, m.source_columns = "DIRECT_COLUMN", [col]
            m.confidence = round(conf * 0.7, 3)
            m.rationale = f"{why}; no code suffix convention known for {retailer}: confirm"

    def _source(self, m, tc, by_type, headers, conv: Conventions, sd, frame, mcfg, used) -> None:
        hit = conv.source_columns(tc.name, headers)
        cands = by_type.get(tc.semantic, [])
        if hit:
            col, conf, why, c = hit
        elif cands:
            col, conf = cands[0]
            why, c = sd.column_hints[col].rationale or f"'{col}' tagged {tc.semantic}", {}
        else:
            return
        used.add(col)
        if tc.date_out or tc.dtype == "date":
            fmt, rate, ambiguous = detect_date_format(frame[col].dropna().tolist(), mcfg.date_dayfirst)
            prof = self._from_profile("date_format")
            if prof:
                pf = java_to_strftime(prof[0])
                vals = frame[col].dropna().astype(str).head(500).tolist()
                ok = sum(1 for v in vals if _parses(v, pf)) / len(vals) if vals else 0
                if ok >= 0.95:
                    fmt, rate, ambiguous = pf, ok, False
                    why += f"; date format {prof[0]} from the onboarding profile"
            if c.get("date_in"):
                known = java_to_strftime(c["date_in"])
                k_fmt, k_rate, _ = detect_date_format(frame[col].dropna().head(200).tolist(), mcfg.date_dayfirst)
                if k_fmt == known or rate == 0:
                    fmt, ambiguous = known, False
            m.action, m.source_columns, m.date_format = "DATE", [col], fmt
            m.target_date_format = tc.date_out
            m.confidence = round(conf * (0.85 if ambiguous else 1.0) * (1.0 if fmt else 0.5), 3)
            m.rationale = f"{why}; parsed with {fmt} ({rate:.0%})" + (" - day/month ambiguous" if ambiguous else "")
        elif not hit and len(cands) > 1 and tc.semantic == "address_line":
            m.action, m.source_columns, m.separator = "CONCATENATION", [x for x, _ in cands], ", "
            m.confidence, m.rationale = round(min(x for _, x in cands), 3), f"address split over {len(cands)} columns"
        else:
            m.action, m.source_columns, m.confidence, m.rationale = "DIRECT_COLUMN", [col], round(conf, 3), why
            if not hit and len(cands) > 1:
                m.confidence = round(conf * 0.9, 3)
                m.rationale += f"; alternatives: {', '.join(x for x, _ in cands[1:])}"

    def _from_profile(self, key: str | None):
        v = (getattr(self, "_profile", None) or {}).get(key or "")
        if not v or not v.get("value"):
            return None
        confirmed = v.get("status") == "confirmed"
        return v["value"], (1.0 if confirmed else 0.9), \
            f"onboarding profile ({'confirmed' if confirmed else 'submitted'} by {v.get('provided_by')})"

    def _context(self, m, tc, sd: SheetDiscovery, conv: Conventions, mcfg: MarketConfig, retailer) -> None:
        key = tc.context
        prof = self._from_profile(key)
        if prof:
            m.action, m.value, m.confidence, m.rationale = "DIRECT_TEXT", prof[0], prof[1], prof[2]
            return
        if key == "market" and sd.source.market:
            m.action, m.value, m.confidence, m.rationale = "DIRECT_TEXT", sd.source.market, 1.0, "market from file path"
            return
        if key == "retailer" and sd.source.retailer:
            m.action, m.value, m.confidence, m.rationale = "DIRECT_TEXT", retailer, 1.0, "retailer from file path"
            return
        if key == "currency":
            for col, h in sd.column_hints.items():
                if h.semantic_type == "net_value" and currency_in_header(col):
                    m.action, m.value, m.confidence = "DIRECT_TEXT", currency_in_header(col), 0.95
                    m.rationale = f"currency in header '{col}'"
                    return
        # entity codes are retailer/market specific: only take them from the same retailer
        strict = key in ("legal_entity_code", "mgmt_entity_code", "transaction_id")
        hit = conv.constant(tc.name, same_retailer_only=strict)
        val = mcfg.value(key, retailer)
        if hit and val is not None and hit[1] < 0.8:
            hit = None                      # a market default beats another market's constant
        if hit and not (key == "currency" and mcfg.currency and hit[0] != mcfg.currency):
            m.action, m.value, m.confidence, m.rationale = "DIRECT_TEXT", hit[0], hit[1], hit[2]
            return
        if val is not None:
            m.action, m.value, m.confidence = "DIRECT_TEXT", str(val), 0.8
            m.rationale = f"{key} default from markets.yaml"

    def _fallback(self, m: ColumnMapping, target: TargetTable, sd: SheetDiscovery, used: set[str]) -> None:
        tc = target.column(m.target_column)
        free = [c for c in sd.column_hints if c not in used]
        if not free:
            return
        names = [normalise_header(tc.name.replace("_", " "))]
        if tc.semantic and tc.semantic in self.catalogue.types:
            names += self.catalogue.types[tc.semantic].synonyms
        best, score = None, 0.0
        for c in free:
            s = max(fuzz.token_set_ratio(normalise_header(c), n) for n in names)
            if s > score:
                best, score = c, s
        if best and score >= 80:
            m.action, m.source_columns, m.confidence = "DIRECT_COLUMN", [best], round(score / 100 * 0.6, 3)
            m.rationale = f"name similarity only ({score:.0f}); confirm"
            used.add(best)
            return
        if self.llm and self.llm.available:
            samples = {p.column: [v for v, _ in p.top_values[:4]] for p in sd.profile.column_profiles if p.column in free}
            prompt = (f"Target column '{tc.name}' ({tc.dtype}, semantic {tc.semantic}) in {target.name}: "
                      f"{target.description}.\nWhich source column fills it? Return null if none.\n"
                      f"Source columns with samples: {samples}")
            pick = self.llm.structured(prompt, _LLMPick, kind="mapping_fallback")
            if pick and pick.source_column in free:
                m.action, m.source_columns = "DIRECT_COLUMN", [pick.source_column]
                m.confidence, m.rationale = round(min(pick.confidence, 0.75), 3), "LLM proposal; confirm"
                used.add(pick.source_column)

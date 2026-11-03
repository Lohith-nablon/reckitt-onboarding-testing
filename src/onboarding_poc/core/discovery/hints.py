"""Column semantic hints. Same cascade as Meridian's HintsInferencer
(src/services/hints_inferencer.py): deterministic first, LLM only for the residue,
now with two extra deterministic passes for retailer files:

  1. static   exact match of the normalised header against catalogue synonyms
  2. fuzzy    rapidfuzz match against synonyms (handles 'Sales Val', 'Str No.')
  3. pattern  value-based detection from the profile (GTIN checksum, postcodes, dates, Y/N flags)
  4. llm      residue only, if Azure OpenAI is configured
Name and value evidence are combined: agreement raises confidence, conflict lowers it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from onboarding_poc.core.contracts import Catalogue
from onboarding_poc.llm import LLM
from onboarding_poc.core.schemas import ColumnHint, ColumnProfile
from onboarding_poc.core.text import normalise_header

# Synonyms that are too generic to trust fully on their own
_GENERIC = {"date", "value", "product", "store", "sales", "customer", "pin", "pack", "size", "week", "day",
            "description", "promo", "units", "qty", "material", "site", "location"}

_PATTERN_TO_TYPE = {
    "gtin": ("ean", 0.9),
    "uk_postcode": ("postcode", 0.85),
    "in_pincode": ("postcode", 0.6),
    "flag": ("promotion_flag", 0.55),
}
_MEASURE_TYPES = {"units_sold", "net_value"}


class _LLMColumn(BaseModel):
    column: str
    semantic_type: str | None = Field(default=None, description="catalogue value or null")
    confidence: float = Field(default=0.0, ge=0, le=1)


class _LLMColumns(BaseModel):
    columns: list[_LLMColumn]


class HintsInferencer:
    def __init__(self, catalogue: Catalogue, llm: LLM | None = None):
        self.catalogue = catalogue
        self.llm = llm
        self._syn_to_type: dict[str, str] = {}
        for t in catalogue.types.values():
            for s in t.synonyms:
                self._syn_to_type.setdefault(s, t.name)
        self._synonyms = list(self._syn_to_type)

    def _by_name(self, header: str) -> ColumnHint | None:
        n = normalise_header(header)
        if not n:
            return None
        if n in self._syn_to_type:
            conf = 0.85 if n in _GENERIC else 1.0
            return ColumnHint(semantic_type=self._syn_to_type[n], confidence=conf, source="static",
                              rationale=f"header '{header}' matches synonym '{n}'")
        m = process.extractOne(n, self._synonyms, scorer=fuzz.token_sort_ratio)
        if m and m[1] >= 88:
            return ColumnHint(semantic_type=self._syn_to_type[m[0]], confidence=round(m[1] / 100 * 0.9, 3),
                              source="fuzzy", rationale=f"header '{header}' ~ '{m[0]}' ({m[1]:.0f})")
        return None

    @staticmethod
    def _by_pattern(p: ColumnProfile) -> ColumnHint | None:
        pat = p.pattern_detected or ""
        if pat.startswith("date:"):
            return ColumnHint(semantic_type="week_ending", confidence=0.65, source="pattern",
                              rationale=f"values parse as dates ({pat[5:]})")
        if pat in _PATTERN_TO_TYPE:
            t, c = _PATTERN_TO_TYPE[pat]
            return ColumnHint(semantic_type=t, confidence=c, source="pattern",
                              rationale=f"values match {pat} ({(p.pattern_match_rate or 0):.0%})")
        return None

    def _combine(self, name: ColumnHint | None, pattern: ColumnHint | None, p: ColumnProfile) -> ColumnHint:
        if name and pattern:
            if name.semantic_type == pattern.semantic_type:
                return name.model_copy(update={"confidence": max(name.confidence, 0.97),
                                               "rationale": f"{name.rationale}; {pattern.rationale}"})
            # value evidence for GTIN beats a generic header
            if pattern.semantic_type == "ean" and name.confidence < 1.0:
                return pattern
            return name.model_copy(update={"confidence": round(name.confidence * 0.85, 3),
                                           "rationale": f"{name.rationale}; values suggest {pattern.semantic_type}"})
        hint = name or pattern
        if hint is None:
            return ColumnHint(semantic_type=None, confidence=0.0, source="none")
        if hint.semantic_type in _MEASURE_TYPES and p.data_type not in ("integer", "decimal"):
            hint = hint.model_copy(update={"confidence": round(hint.confidence * 0.6, 3),
                                           "rationale": f"{hint.rationale}; values are not numeric"})
        return hint

    def infer(self, profiles: list[ColumnProfile], table_context: str = "") -> dict[str, ColumnHint]:
        hints = {p.column: self._combine(self._by_name(p.column), self._by_pattern(p), p) for p in profiles}
        residue = [p for p in profiles if hints[p.column].confidence < 0.6]
        if residue and self.llm and self.llm.available:
            hints.update(self._llm_pass(residue, table_context))
        return hints

    def _llm_pass(self, residue: list[ColumnProfile], table_context: str) -> dict[str, ColumnHint]:
        types = ", ".join(sorted(self.catalogue.types))
        lines = [f"- {p.column}: type={p.data_type}, pattern={p.pattern_detected}, "
                 f"samples={[v for v, _ in p.top_values[:5]]}" for p in residue]
        prompt = (
            "You classify columns from retailer sell-out files sent to a CPG manufacturer (Reckitt).\n"
            f"File context: {table_context}\n"
            f"Allowed semantic types: {types}\n"
            "For each column return the best semantic type or null if none fits, with a confidence 0-1.\n"
            "Columns:\n" + "\n".join(lines)
        )
        res = self.llm.structured(prompt, _LLMColumns, kind="column_hints")
        out: dict[str, ColumnHint] = {}
        if not res:
            return out
        for c in res.columns:
            if c.column in {p.column for p in residue} and (c.semantic_type in self.catalogue.types or
                                                            c.semantic_type is None):
                out[c.column] = ColumnHint(semantic_type=c.semantic_type, confidence=round(c.confidence * 0.9, 3),
                                           source="llm", rationale="LLM classification of residue column")
        return out

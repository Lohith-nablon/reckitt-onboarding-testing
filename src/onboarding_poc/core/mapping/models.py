"""Internal mapping spec. Converted to / from the Veritas mapping JSON in mapping/veritas.py."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Action = Literal["DIRECT_COLUMN", "DIRECT_TEXT", "CONCATENATION", "DATE", "LOOKUP", "EXPRESSION"]


class LookupSpec(BaseModel):
    name: str                      # harmonisation.product | harmonisation.outlet | master.<name>
    key_columns: list[str]         # source columns (harmonisation) or target columns (master)
    return_column: str


class ConcatPart(BaseModel):
    column: str | None = None      # source column, or
    text: str | None = None        # constant text


class ColumnMapping(BaseModel):
    target_column: str
    action: Action | None = None   # None = unmapped (Veritas "NULL" operator)
    source_columns: list[str] = Field(default_factory=list)
    value: str | None = None                   # DIRECT_TEXT constant
    parts: list[ConcatPart] = Field(default_factory=list)   # CONCATENATION inputs, in order
    separator: str | None = None               # CONCATENATION without parts: join source_columns
    date_format: str | None = None             # DATE: source format (strftime)
    target_date_format: str | None = None      # DATE: output format (Java style, e.g. yyyyMMdd)
    lookup: LookupSpec | None = None
    expression: str | None = None              # EXPRESSION: see mapping/transforms.py
    mandatory: bool = False
    is_pk: bool = False
    confidence: float = 0.0
    rationale: str = ""
    reviewed_by: str | None = None             # set when a person changed this column; kept on re-proposal

    def signature(self) -> tuple:
        """What the column does (not why or how sure): used to tell reviewer changes from proposals."""
        return (self.action, tuple(self.source_columns), self.value,
                tuple((p.column, p.text) for p in self.parts), self.separator, self.date_format,
                self.target_date_format, self.expression,
                (self.lookup.name, tuple(self.lookup.key_columns), self.lookup.return_column) if self.lookup else None)


class SourceSpec(BaseModel):
    file_pattern: str
    sheet: str | None = None
    header_row: int = 0                        # rows before the header (Veritas SkipFirstRows)
    footer_rows: int = 0                       # trailing rows dropped (Veritas SkipLastRows)
    file_format: str = "csv"
    delimiter: str | None = None
    template_fingerprint: str
    source_columns: list[str] = Field(default_factory=list)   # every header in the file


class MappingSpec(BaseModel):
    mapping_id: str
    version: int = 1
    status: Literal["proposed", "approved", "superseded"] = "proposed"
    market: str
    retailer: str
    source: SourceSpec
    target_table: str
    columns: list[ColumnMapping]
    origin: str = "proposed"                   # proposed | veritas:<id>
    veritas_id: int | None = None
    veritas_name: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None

    @property
    def key(self) -> str:
        return mapping_key(self.market, self.retailer, self.source.template_fingerprint, self.target_table)

    def issues(self, review_threshold: float = 0.8) -> dict[str, list[str]]:
        blocking = [c.target_column for c in self.columns if c.action is None and (c.mandatory or c.is_pk)]
        review = [c.target_column for c in self.columns if c.action is not None and c.confidence < review_threshold]
        unmapped_optional = [c.target_column for c in self.columns if c.action is None and not (c.mandatory or c.is_pk)]
        return {"blocking": blocking, "review": review, "unmapped_optional": unmapped_optional}

    @property
    def min_confidence(self) -> float:
        mapped = [c.confidence for c in self.columns if c.action is not None]
        return min(mapped) if mapped else 0.0

    def column(self, name: str) -> ColumnMapping:
        return next(c for c in self.columns if c.target_column == name)


def mapping_key(market: str, retailer: str, fingerprint: str, target: str) -> str:
    return f"{market}|{retailer}|{fingerprint}|{target}"

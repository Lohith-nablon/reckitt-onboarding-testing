"""Shared data contracts.

ColumnInfo / ColumnProfile / DataProfile / ColumnHint / TableHint are carried over from
meridian-connection-manager (src/schemas/discovery.py, src/schemas/schema_intel.py) so the
outputs stay compatible with Meridian's scan + KPI tooling. Everything else is new for the
retailer-file flow.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Carried over from Meridian ──────────────────────────────────────────────

class ColumnInfo(BaseModel):
    name: str
    data_type: str
    is_nullable: bool
    default: str | None = None
    null_pct: float | None = None
    cardinality: int | None = None
    min_value: str | None = None
    max_value: str | None = None


class ColumnProfile(BaseModel):
    column: str
    data_type: str
    row_count: int
    null_count: int
    null_rate: float
    distinct_count: int
    min_value: Any | None = None
    max_value: Any | None = None
    mean: float | None = None
    stddev: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    top_values: list[tuple[Any, int]] = []
    pattern_detected: str | None = None
    pattern_match_rate: float | None = None     # new: share of non-null values matching the pattern


class DataProfile(BaseModel):
    source_type: str
    fully_qualified_name: str
    profiled_at: datetime
    sample_fraction: float
    column_profiles: list[ColumnProfile]


class ColumnHint(BaseModel):
    """Per-column semantic-type suggestion. `pattern` source is new (value-based detection)."""
    semantic_type: str | None
    confidence: float
    source: Literal["static", "fuzzy", "pattern", "llm", "human", "none"]
    rationale: str | None = None


class TableHint(BaseModel):
    table_semantic_type: str | None
    confidence: float
    source: Literal["static", "rules", "llm"]


# ── Discovery ───────────────────────────────────────────────────────────────

class SourceFile(BaseModel):
    uri: str                        # local path or blob path
    name: str
    market: str | None = None
    retailer: str | None = None
    size_bytes: int | None = None
    modified_at: datetime | None = None


class HeaderDetection(BaseModel):
    header_row: int                 # 0-based row index in the raw sheet
    confidence: float
    title_rows: list[str] = []      # text found above the header (report titles, supplier names)
    footer_rows_dropped: int = 0    # trailing total/summary rows removed
    reasons: list[str] = []


class SheetDiscovery(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: SourceFile
    sheet: str | None
    table_key: str                  # "<market>.<retailer>.<file>[.<sheet>]"
    template_fingerprint: str
    header: HeaderDetection
    row_count: int
    columns: list[ColumnInfo]
    profile: DataProfile
    column_hints: dict[str, ColumnHint]
    classification: Literal["fact", "dimension", "unknown"]
    classification_confidence: float
    entity: Literal["product", "outlet", "mixed", None] = None
    grain_candidate: list[str] = []         # smallest unique column combination found
    date_range: tuple[str, str] | None = None
    proposals: list["TargetProposal"] = []


class TargetProposal(BaseModel):
    target_table: str
    score: float                    # 0..1 coverage of target columns by this sheet
    primary: bool                   # the main target; others are "also feeds"
    covered_columns: list[str] = []
    missing_mandatory: list[str] = []


class OpenQuestion(BaseModel):
    topic: str
    column: str | None = None
    question: str
    proposed_answer: str | None = None
    confidence: float | None = None


class DiscoveryBrief(BaseModel):
    file: str
    market: str | None
    retailer: str | None
    generated_at: datetime
    sheets: list[dict[str, Any]]
    prefilled: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[OpenQuestion] = []


SheetDiscovery.model_rebuild()

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["auto_accepted", "needs_review", "unmatched", "approved"]
Method = Literal["crosswalk", "universal_code", "description_attributes", "address", "llm_adjudicated", "none"]


class Candidate(BaseModel):
    code: str
    label: str
    score: float
    detail: dict = Field(default_factory=dict)


class MatchResult(BaseModel):
    entity: Literal["product", "outlet"]
    market: str
    retailer: str
    source_key: str                 # retailer SKU or outlet code
    source_label: str               # description / outlet name for reviewers
    source_attrs: dict = Field(default_factory=dict)
    rb_code: str | None
    confidence: float
    method: Method
    status: Status
    candidates: list[Candidate] = Field(default_factory=list)
    reason: str = ""


def band(conf: float, auto_accept: float, review_floor: float) -> Status:
    if conf >= auto_accept:
        return "auto_accepted"
    if conf >= review_floor:
        return "needs_review"
    return "unmatched"

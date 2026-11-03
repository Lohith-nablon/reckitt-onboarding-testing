"""Retailer onboarding profile.

Step 0 of the workflow: before the first file is mapped, the business and audit details that
never appear in retailer data are captured once per retailer:
  * by Reckitt in the workbench (legal entity, management entity, code suffixes, ...)
  * by the retailer through an intake link (platform, delivery, date format, contacts, ...)

The form is generated from the target schema: every target column that needs a per-retailer
value (`context:` / `code_suffix:` in the contracts) becomes a field, merged with the extra
fields in config/onboarding_fields.yaml. Adding a target table with a new business column adds
a field to the form automatically.

Profile values feed mapping first (confidence 1.0 when confirmed by Reckitt, 0.9 when only
submitted by the retailer), before Veritas conventions or market defaults.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from onboarding_poc.core.contracts import Contracts, Markets

_BUILTIN_CONTEXT = {"market", "retailer"}      # come from the file path / name, not the form


class OnboardingField(BaseModel):
    key: str
    label: str
    group: str = "business"
    audience: str = "internal"          # client | internal
    type: str = "text"
    options: list[str] = Field(default_factory=list)
    required: bool = False
    help: str = ""
    used_by: list[str] = Field(default_factory=list)        # table.column


class FieldValue(BaseModel):
    key: str
    value: str | None = None
    status: str | None = None           # submitted (by retailer) | confirmed (by Reckitt)
    provided_by: str | None = None
    provided_at: str | None = None
    suggestion: str | None = None
    suggestion_source: str | None = None


@lru_cache(maxsize=4)
def _load_yaml(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


class OnboardingRegistry:
    def __init__(self, config_dir: str, contracts: Contracts):
        raw = _load_yaml(str(Path(config_dir) / "onboarding_fields.yaml"))
        self.groups: list[dict] = raw.get("groups", [])
        self.fields: dict[str, OnboardingField] = {}
        for f in raw.get("fields", []):
            self.fields[f["key"]] = OnboardingField(**f)
        # fields demanded by the target schema
        for t in contracts.tables.values():
            for c in t.columns:
                for key in (c.context, c.code_suffix):
                    if not key or key in _BUILTIN_CONTEXT:
                        continue
                    if key not in self.fields:
                        self.fields[key] = OnboardingField(key=key, label=key.replace("_", " ").capitalize(),
                                                           group="business", help="added from the target schema")
                    f = self.fields[key]
                    f.used_by.append(f"{t.name}.{c.name}")
                    if c.mandatory and key == c.context and not c.semantic:
                        f.required = True
        if not any(g["id"] == "business" for g in self.groups):
            self.groups.insert(0, {"id": "business", "label": "Business codes"})

    def for_audience(self, audience: str | None) -> list[OnboardingField]:
        return [f for f in self.fields.values() if audience is None or f.audience == audience]


def suggestions(market: str, retailer: str, contracts: Contracts, markets: Markets, conventions) -> dict[str, tuple[str, str]]:
    """Proposed values for empty fields: existing Veritas mappings first, then markets.yaml."""
    mcfg = markets.get(market)
    out: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    for t in contracts.tables.values():
        for c in t.columns:
            if c.context and c.context not in seen and c.context not in _BUILTIN_CONTEXT:
                strict = c.context in ("legal_entity_code", "mgmt_entity_code", "transaction_id")
                hit = conventions.constant(c.name, same_retailer_only=strict)
                default = mcfg.value(c.context, retailer)
                if hit and hit[1] < 0.8 and default is not None:
                    hit = None              # another market's constant never beats this market's default
                if hit:
                    out[c.context] = (str(hit[0]), hit[2])
                    seen.add(c.context)
            if c.code_suffix and c.code_suffix not in seen:
                hit = conventions.suffix(c.name)
                if hit:
                    out[c.code_suffix] = (hit[0], hit[2])
                    seen.add(c.code_suffix)
    for key, label in (("currency", "currency"), ("legc", "legc"), ("week_end_day", "week_end_day"), ("uom", "uom")):
        if key not in out:
            v = mcfg.value(key, retailer) if key not in ("week_end_day",) else mcfg.week_end_day
            if v:
                out[key] = (str(v), f"{market} default in markets.yaml")
    if "date_format" not in out and mcfg.date_dayfirst:
        out["date_format"] = ("dd/MM/yyyy", f"{market} writes dates day-first (confirm separator)")
    return out


def completeness(fields: list[OnboardingField], values: dict[str, FieldValue]) -> dict[str, Any]:
    required = [f.key for f in fields if f.required]
    missing = [k for k in required if not (values.get(k) and values[k].value)]
    filled = sum(1 for f in fields if values.get(f.key) and values[f.key].value)
    return {"required": len(required), "missing_required": missing, "filled": filled, "total": len(fields),
            "complete": not missing}

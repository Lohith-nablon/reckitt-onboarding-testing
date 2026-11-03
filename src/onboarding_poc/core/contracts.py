"""Loads the YAML configuration: target contracts, semantic catalogue, market defaults."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

DType = Literal["string", "date", "decimal", "integer", "boolean"]


class EnrichSpec(BaseModel):
    master: str
    column: str


class TargetColumn(BaseModel):
    name: str
    dtype: DType = "string"
    mandatory: bool = False
    pk: bool = False
    semantic: str | None = None
    context: str | None = None          # market | retailer | currency | uom | legal_entity_code | ...
    code_suffix: str | None = None      # convention key, e.g. product_code_suffix
    date_out: str | None = None         # Java-style output format, e.g. yyyyMMdd
    harmonised: Literal["product", "outlet"] | None = None
    enrich: EnrichSpec | None = None
    description: str = ""
    default: str | None = None          # constant used when nothing else fills the column

    @property
    def field_source(self) -> str:
        """Where the value comes from: source (retailer file), onboarding (retailer profile),
        derived (harmonisation / master data) or unmapped."""
        if self.harmonised or self.enrich:
            return "derived"
        if self.context and not self.semantic:
            return "onboarding"
        if self.semantic:
            return "source"
        return "optional"


class PhysicalLayout(BaseModel):
    """How the logical (Veritas dataset) columns land in the physical bronze table."""
    case: Literal["upper", "lower", "asis"] = "asis"
    rename: dict[str, str] = Field(default_factory=dict)          # logical -> physical (before case)
    # physical column -> generator: submission_id | retailer | insert_ts | batch_id | source_file |
    #                               record_hash | const:<value>
    system_columns: dict[str, str] = Field(default_factory=dict)
    order: list[str] | None = None                                 # physical column order

    def physical(self, logical: str) -> str:
        n = self.rename.get(logical, logical)
        return n.upper() if self.case == "upper" else n.lower() if self.case == "lower" else n


class TargetTable(BaseModel):
    name: str
    kind: Literal["fact", "dimension"]
    entity: Literal["product", "outlet"] | None = None
    veritas_dataset: str | None = None
    physical_name: str | None = None
    physical: PhysicalLayout | None = None
    description: str = ""
    columns: list[TargetColumn]

    @property
    def pk(self) -> list[str]:
        return [c.name for c in self.columns if c.pk]

    @property
    def table_name(self) -> str:
        return self.physical_name or self.name

    def column(self, name: str) -> TargetColumn:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    def has(self, name: str) -> bool:
        return any(c.name == name for c in self.columns)


class MasterSpec(BaseModel):
    file: str
    key: str
    columns: dict[str, str]


class Contracts(BaseModel):
    tables: dict[str, TargetTable]
    masters: dict[str, MasterSpec]

    def by_dataset(self, dataset: str) -> TargetTable | None:
        return next((t for t in self.tables.values() if t.veritas_dataset == dataset), None)


class SemanticType(BaseModel):
    name: str
    kind: str
    role: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class Catalogue(BaseModel):
    version: str
    types: dict[str, SemanticType]
    abbreviations: dict[str, str] = Field(default_factory=dict)


class MarketConfig(BaseModel):
    code: str | None = None
    legc: str | None = None
    country_code: str | None = None
    currency: str | None = None
    date_dayfirst: bool = False
    week_end_day: str | None = None
    postcode_regex: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    retailers: dict[str, dict] = Field(default_factory=dict)

    def banner(self, retailer: str) -> str:
        return self.retailers.get((retailer or "").upper(), {}).get("banner", retailer)

    def display_name(self, retailer: str) -> str:
        return self.retailers.get((retailer or "").upper(), {}).get("display_name", (retailer or "").title())

    def value(self, key: str, retailer: str | None = None) -> Any:
        """Retailer override -> market value -> None. Currency also resolves to the market currency."""
        r = self.retailers.get((retailer or "").upper(), {})
        if key in r:
            return r[key]
        if key in self.values:
            return self.values[key]
        if key == "currency":
            return self.currency
        return getattr(self, key, None) if key in ("legc", "country_code") else None


class Markets(BaseModel):
    markets: dict[str, MarketConfig]
    default: MarketConfig

    def get(self, market: str | None) -> MarketConfig:
        if market and market.upper() in self.markets:
            return self.markets[market.upper()]
        return self.default

    def by_legc(self, legc: str | None) -> str | None:
        return next((k for k, m in self.markets.items() if legc and m.legc == legc), None)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _expand(columns: list[dict]) -> list[dict]:
    out = []
    for c in columns:
        n = c.pop("repeat", None)
        if n:
            out += [{**c, "name": c["name"].format(i=i)} for i in range(1, int(n) + 1)]
        else:
            out.append(c)
    return out


def _table_files(config_dir: str, extra_dir: str | None) -> list[Path]:
    files = [Path(config_dir) / "bronze_contracts.yaml"]
    for d in (Path(config_dir) / "targets", Path(extra_dir) if extra_dir else None):
        if d and d.is_dir():
            files += sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml"))
    return files


@lru_cache(maxsize=8)
def load_contracts(config_dir: str, extra_dir: str | None = None) -> Contracts:
    """Target tables from bronze_contracts.yaml, config/targets/*.yaml and `extra_dir`/*.yaml.

    A later file redefines a table of the same name, so an imported schema overrides the
    built-in stand-ins without editing them. Masters come from bronze_contracts.yaml."""
    raw = _load(Path(config_dir) / "bronze_contracts.yaml")
    table_specs: dict[str, dict] = {}
    for f in _table_files(config_dir, extra_dir):
        data = _load(f) or {}
        table_specs.update(data.get("tables") or {})
    return build_contracts(table_specs, raw.get("masters", {}))


def build_contracts(table_specs: dict[str, dict], masters_raw: dict[str, dict]) -> Contracts:
    """Contracts from table specs as written in YAML (or produced by schema_import.build_tables)."""
    tables = {}
    for name, spec in table_specs.items():
        spec = dict(spec)
        spec["columns"] = _expand([dict(c) for c in spec["columns"]])
        if spec.get("physical") and spec["physical"].get("order"):
            order = []
            for o in spec["physical"]["order"]:
                if isinstance(o, dict):          # {"MEASURE_{i}": [1, 5]}
                    (pattern, (lo, hi)), = o.items()
                    order += [pattern.format(i=i) for i in range(lo, hi + 1)]
                else:
                    order.append(o)
            spec["physical"] = {**spec["physical"], "order": order}
        tables[name] = TargetTable(name=name, **spec)
    masters = {name: MasterSpec(**spec) for name, spec in (masters_raw or {}).items()}
    return Contracts(tables=tables, masters=masters)


@lru_cache(maxsize=8)
def load_catalogue(config_dir: str) -> Catalogue:
    raw = _load(Path(config_dir) / "semantic_catalogue.yaml")
    types = {name: SemanticType(name=name, **spec) for name, spec in raw["types"].items()}
    return Catalogue(version=raw.get("version", "v1"), types=types, abbreviations=raw.get("abbreviations", {}))


@lru_cache(maxsize=8)
def load_markets(config_dir: str) -> Markets:
    raw = _load(Path(config_dir) / "markets.yaml")
    default = raw.get("default") or {}
    markets = {}
    for k, v in (raw.get("markets") or {}).items():
        merged_values = {**(default.get("values") or {}), **(v.get("values") or {})}
        markets[k.upper()] = MarketConfig(code=k.upper(), **{**v, "values": merged_values})
    return Markets(markets=markets, default=MarketConfig(**default))

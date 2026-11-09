"""Master config: one YAML file plus environment overrides.

Holds everything the architecture puts in "master config": the S3 connector, folder paths,
the LLM endpoint and its access, review thresholds and the discovery sample size.

Load order (later wins):
  1. defaults in this file
  2. config/master_config.yaml (or the file named by POC_CONFIG)
  3. environment variables POC_<SECTION>__<KEY>, e.g. POC_STORAGE__BUCKET=rb-onboarding-poc

Secrets are never written in the YAML: name the environment variable (api_key_env,
access_key_env, ...) and set it in the Databricks App secret scope.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
CORE_CONFIG_DIR = str(Path(__file__).resolve().parent / "core" / "config")


class S3Settings(BaseModel):
    bucket: str = ""
    prefix: str = "retailer-onboarding-poc"
    region: str = "eu-north-1"
    endpoint_url: str | None = None            # MinIO / moto / VPC endpoint
    profile: str | None = None                 # local runs only
    access_key_env: str = "AWS_ACCESS_KEY_ID"   # Databricks App: map from the secret scope
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    session_token_env: str = "AWS_SESSION_TOKEN"


class AdlsSettings(BaseModel):
    """Azure Data Lake Storage Gen2 (Reckitt's lake). Auth, first match wins: account key env,
    SAS token env, else DefaultAzureCredential (managed identity / service principal env vars)."""
    account: str = ""                      # storage account name, e.g. rbedlsdev
    container: str = "bronze"              # filesystem holding the landing zone and our folders
    prefix: str = "retailer-onboarding-poc"
    account_key_env: str = "AZURE_STORAGE_KEY"
    sas_token_env: str = "AZURE_STORAGE_SAS"

    def abfss(self, key: str) -> str:
        path = f"{self.prefix.strip('/')}/{key}" if self.prefix else key
        return f"abfss://{self.container}@{self.account}.dfs.core.windows.net/{path}"


class VolumeSettings(BaseModel):
    """Unity Catalog volume, used when simulating on Nablon's Databricks workspace in place of
    Reckitt's ADLS. Accessed through the Files API with the app's own identity."""
    path: str = ""                          # /Volumes/<catalog>/<schema>/<volume>/retailer-onboarding-poc


class StorageSettings(BaseModel):
    backend: Literal["local", "adls", "volume", "s3"] = "local"
    local_root: str = str(ROOT / "data" / "store")
    adls: AdlsSettings = AdlsSettings()
    volume: VolumeSettings = VolumeSettings()
    s3: S3Settings = S3Settings()             # kept for Nablon's AWS dev workspace


class Paths(BaseModel):
    """Folder layout inside the bucket / local root. Retailer files can also come from any
    other S3 path given at intake time with the client's credentials."""
    landing: str = "landing"                     # bronze landing zone: raw retailer files as they arrive
    inbox: str = "inbox"                         # copies of the files a session works on
    outputs: str = "outputs"                     # bronze-ready files, reports, trace per session
    sessions: str = "state/sessions"             # session state JSON
    mapping_library: str = "library/mappings"    # approved mappings (Veritas JSON + internal spec)
    crosswalk: str = "library/crosswalk"         # learned retailer code -> RB code
    mdm: str = "mdm"                             # ERP product master, RB customer master
    veritas_reference: str = "library/veritas_reference"   # existing Veritas mappings for compare


class WarehouseSettings(BaseModel):
    """Unity Catalog is read and written through a Databricks SQL warehouse."""
    backend: Literal["local", "databricks"] = "local"
    host: str = ""                          # adb-123.azuredatabricks.net
    http_path: str = ""                     # /sql/1.0/warehouses/<id>
    token_env: str = "DATABRICKS_TOKEN"     # personal token; inside a Databricks App the app's own identity is used
    warehouse_id_env: str = "DATABRICKS_WAREHOUSE_ID"    # set from the app's sql-warehouse resource
    local_root: str = str(ROOT / "data" / "warehouse")   # DuckDB stand-in for development
    # The POC rule: the app never executes Reckitt's pipeline SQL. Only the local stand-in may,
    # for demos. Setting this true on databricks lets the app run the generated SQL itself.
    allow_pipeline_run: bool = False


class UnityCatalogSettings(BaseModel):
    """Where the pipeline loads bronze and where harmonisation writes silver."""
    bronze_catalog: str = "bronze_dev"
    bronze_schema: str = "pos"
    silver_catalog: str = "silver_dev"
    silver_schema: str = "pos"
    bronze_tables: dict[str, str] = Field(default_factory=dict)   # target table -> catalog.schema.table
    silver_suffix: str = "_harmonised"
    crosswalk_table: str = ""               # default <silver_catalog>.<silver_schema>.retailer_code_crosswalk
    mapping_table: str = ""                 # Delta table for the mapping store when mapping_store.mode = uc_table

    def bronze(self, target: str) -> str:
        if target in self.bronze_tables:
            return self.bronze_tables[target]
        from onboarding_poc.core.mapping.sqlgen import fqn
        return fqn(self.bronze_catalog, self.bronze_schema, target.split('.')[-1])

    def silver(self, target: str) -> str:
        from onboarding_poc.core.mapping.sqlgen import fqn
        return fqn(self.silver_catalog, self.silver_schema, f"{target.split('.')[-1]}{self.silver_suffix}")

    def crosswalk(self) -> str:
        from onboarding_poc.core.mapping.sqlgen import fqn
        return self.crosswalk_table or fqn(self.silver_catalog, self.silver_schema, "retailer_code_crosswalk")


class MappingStoreSettings(BaseModel):
    """Where the approved mapping JSON is kept, in Delta format (decided 5 Nov).
    delta_path: a Delta table written straight to storage (ADLS / local folder) with delta-rs.
    uc_table:   a Delta table in Unity Catalog, written through the SQL warehouse.
    auto:       uc_table when the warehouse is Databricks, delta_path otherwise."""
    mode: Literal["auto", "delta_path", "uc_table"] = "auto"
    folder: str = "mapping_store"           # under the storage root, for delta_path


class SqlSettings(BaseModel):
    """How the generated SQL reads the raw file. read_files: straight from ADLS (default).
    table: from a landing table the pipeline fills first (name template below)."""
    source_mode: Literal["read_files", "table"] = "read_files"
    landing_table_template: str = "{bronze_catalog}.landing.{retailer}_{file_stem}"


class LLMSettings(BaseModel):
    """Model serving / AI Gateway. Runtime in Reckitt stays on OpenAI models unless the
    Reckitt platform team approves another one."""
    provider: Literal["none", "openai", "azure_openai", "databricks"] = "none"
    base_url: str = ""                 # databricks: https://<host>/serving-endpoints ; openai: gateway URL
    model: str = ""                    # serving endpoint name / deployment name
    api_key_env: str = "POC_LLM_API_KEY"
    api_version: str = "2024-10-21"    # azure_openai only
    temperature: float = 0.0
    timeout_s: int = 60
    max_retries: int = 2


class DiscoverySettings(BaseModel):
    sample_rows: int = Field(20, ge=10, le=20)   # rows per file sent to the LLM (10-20 agreed)
    max_columns_per_call: int = 60


class Thresholds(BaseModel):
    mapping_review: float = 0.80        # below: flagged for review on the mapping canvas
    match_auto_accept: float = 0.90     # RB code accepted without review
    match_review_floor: float = 0.70    # below: unmatched, reviewer picks from top 5
    discovery_review: float = 0.70      # column type below this is flagged in AI Discovery
    veritas_match_target: float = 0.95  # KPI: share of target columns equal to the Veritas mapping


class MDMSettings(BaseModel):
    product_file: str = "erp_product_master.csv"
    customer_file: str = "rb_customer_master.csv"
    cache_ttl_s: int = 3600
    size_warn_mb: int = 1024            # revisit (DB or RAG) near 1 GB


class AuthSettings(BaseModel):
    # databricks_app: user from X-Forwarded-Email (set by Databricks Apps); none: local dev
    mode: Literal["none", "databricks_app"] = "none"
    dev_user: str = "local.user@nablon.ai"


class OutputSettings(BaseModel):
    format: Literal["csv", "parquet"] = "csv"
    preview_rows: int = 200


class AppConfig(BaseModel):
    env: str = "local"
    storage: StorageSettings = StorageSettings()
    paths: Paths = Paths()
    llm: LLMSettings = LLMSettings()
    discovery: DiscoverySettings = DiscoverySettings()
    thresholds: Thresholds = Thresholds()
    mdm: MDMSettings = MDMSettings()
    auth: AuthSettings = AuthSettings()
    output: OutputSettings = OutputSettings()
    warehouse: WarehouseSettings = WarehouseSettings()
    uc: UnityCatalogSettings = UnityCatalogSettings()
    sql: SqlSettings = SqlSettings()
    mapping_store: MappingStoreSettings = MappingStoreSettings()
    core_config_dir: str = CORE_CONFIG_DIR
    frontend_dist: str = str(ROOT / "frontend" / "dist")

    def public(self) -> dict:
        """Config as shown on the screen: no secrets, only which env vars hold them."""
        d = self.model_dump()
        d["llm"]["api_key_set"] = bool(os.environ.get(self.llm.api_key_env))
        d["warehouse"]["token_set"] = bool(os.environ.get(self.warehouse.token_env))
        return d


def _deep_set(d: dict, keys: list[str], value: str) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _env_overrides(prefix: str = "POC_") -> dict:
    out: dict = {}
    for k, v in os.environ.items():
        if k.startswith(prefix) and "__" in k:
            _deep_set(out, [p.lower() for p in k[len(prefix):].split("__")], v)
    return out


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        a[k] = _merge(a.get(k) or {}, v) if isinstance(v, dict) else v
    return a


def load_config(path: str | None = None, **overrides) -> AppConfig:
    path = path or os.environ.get("POC_CONFIG") or str(ROOT / "config" / "master_config.yaml")
    data: dict = {}
    if Path(path).is_file():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    data = _merge(data, _env_overrides())
    data = _merge(data, overrides)
    cfg = AppConfig.model_validate(data)
    # relative folders are relative to the project root, not the working directory
    if not Path(cfg.storage.local_root).is_absolute():
        cfg.storage.local_root = str(ROOT / cfg.storage.local_root)
    if not Path(cfg.warehouse.local_root).is_absolute():
        cfg.warehouse.local_root = str(ROOT / cfg.warehouse.local_root)
    if not Path(cfg.frontend_dist).is_absolute():
        cfg.frontend_dist = str(ROOT / cfg.frontend_dist)
    return cfg


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()

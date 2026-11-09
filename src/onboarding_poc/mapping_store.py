"""Mapping store: the approved mapping JSON, kept in Delta format (decided 5 Nov).

Once a mapping is approved at Gate A, its JSON (Veritas format) is converted to a Delta row and
stored. A plain .json copy is also written next to the SQL file, as a fallback. One row per (session, mapping); re-publishing a session
replaces its rows. The SQL generated from the mapping is stored with it.

  delta_path  delta-rs writes the Delta table straight to storage: ADLS (abfss://) in Reckitt,
              a local folder in development. No Spark or SQL warehouse needed.
  uc_table    the Delta table lives in Unity Catalog, written through the SQL warehouse. Used when
              simulating on a Databricks workspace (storage is a UC volume there).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

COLUMNS = ["session_id", "mapping_id", "version", "market", "retailer", "file_name", "sheet", "target_table",
           "target_fqn", "submission_id", "approved_by", "approved_at", "published_by", "published_at",
           "mapping_json", "spec_json", "sql"]


class MappingStore:
    def __init__(self, ctx):
        self.ctx = ctx

    @property
    def mode(self) -> str:
        m = self.ctx.cfg.mapping_store.mode
        if m != "auto":
            return m
        return "uc_table" if self.ctx.cfg.warehouse.backend == "databricks" else "delta_path"

    # ── locations ──────────────────────────────────────────────────────────
    def table(self) -> str:
        uc = self.ctx.cfg.uc
        return uc.mapping_table or f"{uc.bronze_catalog}.{uc.bronze_schema}.onboarding_mapping_store"

    def path(self) -> tuple[str, dict]:
        cfg, folder = self.ctx.cfg, self.ctx.cfg.mapping_store.folder
        if cfg.storage.backend == "local":
            p = Path(cfg.storage.local_root) / folder
            p.mkdir(parents=True, exist_ok=True)
            return str(p), {}
        if cfg.storage.backend == "adls":
            a = cfg.storage.adls
            opts = {"azure_storage_account_name": a.account}
            if os.environ.get(a.account_key_env):
                opts["azure_storage_account_key"] = os.environ[a.account_key_env]
            elif os.environ.get(a.sas_token_env):
                opts["azure_storage_sas_key"] = os.environ[a.sas_token_env]
            else:
                opts["azure_use_azure_cli"] = "false"   # fall back to managed identity / env credentials
            return a.abfss(folder), opts
        raise ValueError(f"mapping_store.mode delta_path needs local or adls storage, not {cfg.storage.backend}; "
                         "use mapping_store.mode uc_table")

    def location(self) -> str:
        return self.table() if self.mode == "uc_table" else self.path()[0]

    # ── write / read ───────────────────────────────────────────────────────
    def write(self, session_id: str, rows: list[dict]) -> str:
        df = pd.DataFrame([{c: (None if r.get(c) is None else str(r.get(c))) for c in COLUMNS} for r in rows],
                          columns=COLUMNS)
        if self.mode == "uc_table":
            self.ctx.warehouse.write_frame(df, self.table(), delete_where=f"`session_id` = '{session_id}'")
            return self.table()
        from deltalake import DeltaTable, write_deltalake
        import pyarrow as pa
        path, opts = self.path()
        data = pa.Table.from_pandas(df, schema=pa.schema([(c, pa.string()) for c in COLUMNS]), preserve_index=False)
        try:
            dt = DeltaTable(path, storage_options=opts or None)
            dt.delete(f"session_id = '{session_id}'")
        except Exception:      # first write: no table yet
            pass
        write_deltalake(path, data, mode="append", storage_options=opts or None)
        return path

    def read(self, session_id: str | None = None) -> pd.DataFrame:
        if self.mode == "uc_table":
            where = f" WHERE `session_id` = '{session_id}'" if session_id else ""
            return self.ctx.warehouse.query(f"SELECT * FROM {self.table()}{where}")
        from deltalake import DeltaTable
        path, opts = self.path()
        try:
            df = DeltaTable(path, storage_options=opts or None).to_pandas()
        except Exception:
            return pd.DataFrame(columns=COLUMNS)
        return df[df["session_id"] == session_id] if session_id else df

    def mapping_json(self, session_id: str, mapping_id: str) -> dict | None:
        df = self.read(session_id)
        hit = df[df["mapping_id"] == mapping_id]
        if not len(hit) or hit.iloc[0]["mapping_json"] in (None, ""):
            return None
        return json.loads(hit.iloc[0]["mapping_json"])

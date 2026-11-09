"""Unity Catalog through a SQL warehouse, plus a local stand-in for development.

  databricks  databricks-sql-connector against a Databricks SQL warehouse. Tables are named
              catalog.schema.table exactly as in Unity Catalog. Used in Reckitt's environment.
  local       DuckDB files under data/warehouse/, one file per catalog, so the same
              catalog.schema.table names and the same generated SQL (transpiled with sqlglot)
              work on a laptop. Used for development, tests and demos.

The app only reads and writes tables here. Running Reckitt's pipeline is not the app's job;
for local demos `run_pipeline_locally` plays that role on the generated SQL.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Protocol

import pandas as pd
from loguru import logger

from onboarding_poc.config import AppConfig
from onboarding_poc.core.mapping.sqlgen import check_fqn, q

_BATCH = 500


def _lit(v) -> str:
    if v is None or (isinstance(v, float) and v != v) or v is pd.NA:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


class Warehouse(Protocol):
    dialect: str
    def query(self, sql: str) -> pd.DataFrame: ...
    def execute(self, sql: str) -> None: ...
    def table_exists(self, fqn: str) -> bool: ...
    def write_frame(self, df: pd.DataFrame, fqn: str, mode: str = "append", delete_where: str | None = None) -> int: ...
    def describe(self) -> dict: ...


def split_script(sql: str) -> list[str]:
    """Split a generated script on statement-ending semicolons (none appear inside our literals' lines)."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in re.split(r";\s*\n", body) if s.strip().strip(";")]


class DatabricksWarehouse:
    dialect = "databricks"

    def __init__(self, host: str, http_path: str, token: str | None):
        from databricks import sql as dbsql
        server = host.replace("https://", "").rstrip("/")
        if token:
            self._connect = lambda: dbsql.connect(server_hostname=server, http_path=http_path, access_token=token)
        else:
            # inside a Databricks App: OAuth as the app's service principal (DATABRICKS_CLIENT_ID/SECRET)
            from databricks.sdk.core import Config, oauth_service_principal
            sdk_cfg = Config()
            self._connect = lambda: dbsql.connect(server_hostname=server, http_path=http_path,
                                                  credentials_provider=lambda: oauth_service_principal(sdk_cfg))
        self.host, self.http_path = host, http_path

    def query(self, sql: str) -> pd.DataFrame:
        with self._connect() as c, c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall_arrow().to_pandas()

    def execute(self, sql: str) -> None:
        with self._connect() as c, c.cursor() as cur:
            cur.execute(sql)

    def table_exists(self, fqn: str) -> bool:
        cat, sch, tbl = check_fqn(fqn).split(".")
        df = self.query(f"SELECT 1 FROM {cat}.information_schema.tables WHERE table_schema = {_lit(sch)} "
                        f"AND table_name = {_lit(tbl)}")
        return len(df) > 0

    def write_frame(self, df: pd.DataFrame, fqn: str, mode: str = "append", delete_where: str | None = None) -> int:
        check_fqn(fqn)
        cols = list(df.columns)
        stmts = [f"CREATE TABLE IF NOT EXISTS {fqn} ({', '.join(q(c) + ' STRING' for c in cols)})"]
        if mode == "overwrite":
            stmts.append(f"DELETE FROM {fqn}")
        elif delete_where:
            stmts.append(f"DELETE FROM {fqn} WHERE {delete_where}")
        rows = df.astype(object).where(df.notna(), None).values.tolist()
        for i in range(0, len(rows), _BATCH):
            vals = ",\n".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in rows[i:i + _BATCH])
            stmts.append(f"INSERT INTO {fqn} ({', '.join(q(c) for c in cols)}) VALUES\n{vals}")
        with self._connect() as c, c.cursor() as cur:
            for s in stmts:
                cur.execute(s)
        return len(rows)

    def describe(self) -> dict:
        return {"kind": "databricks", "host": self.host, "http_path": self.http_path}


_LOCAL_MACROS = [
    # Spark functions the generated SQL uses that DuckDB lacks or names differently
    "CREATE OR REPLACE MACRO substring_index(s, d, n) AS split_part(s, d, n)",
]


class LocalWarehouse:
    """DuckDB stand-in: catalog -> one .duckdb file, so `catalog.schema.table` resolves as in UC."""
    dialect = "duckdb"

    def __init__(self, root: str):
        import duckdb
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.root / "_main.duckdb"))
        self._lock = threading.RLock()
        self._attached: set[str] = set()
        for m in _LOCAL_MACROS:
            self.con.execute(m)

    def _attach_for(self, sql: str) -> None:
        for cat, sch in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\b", sql)):
            if cat in ("mapped",) or cat.startswith("_"):
                continue
            if cat not in self._attached:
                self.con.execute(f"ATTACH IF NOT EXISTS '{self.root / (cat + '.duckdb')}' AS {cat}")
                self._attached.add(cat)
            self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {cat}.{sch}")

    def to_local(self, sql: str) -> str:
        import sqlglot
        from onboarding_poc.core.mapping.veritas import java_to_strftime
        out = sqlglot.transpile(sql, read="databricks", write="duckdb")[0]
        # Spark try_to_timestamp(x, 'dd-MM-yyyy') -> DuckDB try_strptime(x, '%d-%m-%Y'); format must be a constant
        out = re.sub(r"TRY_TO_TIMESTAMP\((.*?), '([^']*)'\)",
                     lambda m: f"TRY_STRPTIME({m.group(1)}, '{java_to_strftime(m.group(2))}')", out)
        return out.replace("'-$1'", "'-\\1'")

    def query(self, sql: str) -> pd.DataFrame:
        with self._lock:
            local = self.to_local(sql)
            self._attach_for(local)
            return self.con.execute(local).df()

    def execute(self, sql: str) -> None:
        with self._lock:
            local = self.to_local(sql)
            self._attach_for(local)
            self.con.execute(local)

    def register(self, name: str, df: pd.DataFrame) -> None:
        with self._lock:
            self.con.register(name, df)

    def table_exists(self, fqn: str) -> bool:
        cat, sch, tbl = check_fqn(fqn).split(".")
        with self._lock:
            self._attach_for(fqn)
            r = self.con.execute("SELECT 1 FROM information_schema.tables WHERE table_catalog = ? AND table_schema = ? "
                                 "AND table_name = ?", [cat, sch, tbl]).fetchall()
            return bool(r)

    def write_frame(self, df: pd.DataFrame, fqn: str, mode: str = "append", delete_where: str | None = None) -> int:
        check_fqn(fqn)
        out = df.astype(object).where(df.notna(), None).astype("string")
        with self._lock:
            self._attach_for(fqn)
            self.con.register("__w", out)
            self.con.execute(f"CREATE TABLE IF NOT EXISTS {fqn} AS SELECT * FROM __w LIMIT 0")
            if mode == "overwrite":
                self.con.execute(f"DELETE FROM {fqn}")
            elif delete_where:
                self.con.execute(f"DELETE FROM {fqn} WHERE {self.to_local('SELECT 1 WHERE ' + delete_where).split('WHERE', 1)[1]}")
            cols = ", ".join('"' + c.replace('"', '""') + '"' for c in out.columns)
            self.con.execute(f"INSERT INTO {fqn} ({cols}) SELECT {cols} FROM __w")
            self.con.unregister("__w")
        return len(out)

    def describe(self) -> dict:
        return {"kind": "local", "path": str(self.root)}


def build_warehouse(cfg: AppConfig) -> Warehouse:
    w = cfg.warehouse
    if w.backend == "databricks":
        token = os.environ.get(w.token_env, "")
        host = w.host or os.environ.get("DATABRICKS_HOST", "")
        http_path = w.http_path
        if not http_path and os.environ.get(w.warehouse_id_env):
            http_path = f"/sql/1.0/warehouses/{os.environ[w.warehouse_id_env]}"
        in_app = bool(os.environ.get("DATABRICKS_CLIENT_ID"))
        if not (host and http_path and (token or in_app)):
            raise ValueError("warehouse.backend is databricks but the host, the warehouse (http_path or "
                             f"${w.warehouse_id_env}) or credentials (${w.token_env}, or the app identity) are missing")
        return DatabricksWarehouse(host, http_path, token or None)
    logger.info("using the local DuckDB warehouse at {}", w.local_root)
    return LocalWarehouse(w.local_root)

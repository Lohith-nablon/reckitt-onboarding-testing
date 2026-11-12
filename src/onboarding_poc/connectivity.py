"""Unity Catalog connectivity check: who the app runs as, which warehouse, and whether each
configured table can be read. Used by `python -m onboarding_poc check-uc` and the Environment card.

Inside a Databricks App the app's service principal is used (no token, no config file).
On a laptop: DATABRICKS_HOST + DATABRICKS_TOKEN, or a ~/.databrickscfg profile via
DATABRICKS_CONFIG_PROFILE for the Jobs API.
"""
from __future__ import annotations

import os
import time

from onboarding_poc.config import mdm_table, ops_table


def auth_mode() -> str:
    if os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_APP_PORT"):
        return "app service principal (OAuth)"
    if os.environ.get("DATABRICKS_TOKEN"):
        return "personal access token"
    if os.environ.get("DATABRICKS_CONFIG_PROFILE"):
        return f"config profile {os.environ['DATABRICKS_CONFIG_PROFILE']}"
    return "local (no Databricks)"


def tables(ctx) -> dict[str, str]:
    cfg = ctx.cfg
    from onboarding_poc.mapping_store import MappingStore
    out = {"rb_product_master": mdm_table(cfg, cfg.mdm.product_table),
           "rb_customer_master": mdm_table(cfg, cfg.mdm.customer_table),
           "landing_events": ops_table(cfg, cfg.ops.landing_events_table),
           "audit": ops_table(cfg, cfg.ops.audit_table),
           "crosswalk": cfg.uc.crosswalk()}
    ms = MappingStore(ctx)
    if ms.mode == "uc_table":
        out["mapping_store"] = ms.table()
    return out


def check(ctx) -> dict:
    wh_cfg = ctx.cfg.warehouse
    res: dict = {"auth": auth_mode(), "warehouse_backend": wh_cfg.backend, "mdm_source": ctx.cfg.mdm.source,
                 "pipeline_mode": ctx.cfg.pipeline.mode, "ok": True, "tables": []}
    try:
        wh = ctx.warehouse
        res["warehouse"] = wh.describe()
    except Exception as exc:
        res.update(ok=False, error=f"warehouse: {exc}")
        return res
    try:
        who = wh.query("SELECT current_user() AS u") if wh.dialect == "databricks" else None
        res["identity"] = str(who.iloc[0, 0]) if who is not None else "local"
    except Exception as exc:
        res.update(ok=False, identity=None, error=f"identity: {str(exc)[:300]}")
    for name, fqn in tables(ctx).items():
        t0 = time.time()
        row = {"name": name, "fqn": fqn}
        try:
            if wh.table_exists(fqn):
                row["rows"] = int(wh.query(f"SELECT count(*) AS n FROM {fqn}").iloc[0, 0])
                row["status"] = "ok"
            else:
                row["status"] = "missing"
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)[:300]
            if name.startswith("rb_"):
                res["ok"] = False
        row["ms"] = int((time.time() - t0) * 1000)
        res["tables"].append(row)
    if ctx.cfg.mdm.source == "uc" and not any(t["name"].startswith("rb_") and t.get("rows") for t in res["tables"]):
        res["ok"] = False
        res.setdefault("error", "RB master tables are empty or missing: run Load demo data, or deploy/databricks/setup.sql")
    return res

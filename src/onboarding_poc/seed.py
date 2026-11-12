"""Dummy dataset for demos and tests: MDM extracts, a Veritas reference mapping (#4285) and
sample retailer files in the landing zone, written to the configured storage (local folder or ADLS).

    python -m onboarding_poc.seed            # into the configured storage
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from onboarding_poc.config import AppConfig, get_config
from onboarding_poc.core.sample_data import build
from onboarding_poc.storage import build_storage


def seed_rb_master(cfg: AppConfig, warehouse, mdm_dir: Path) -> dict:
    """Dummy RB codes as Unity Catalog tables (<catalog>.rb_master.*), built from the same demo
    MDM extracts so the demo files still match. Replaces the tables' rows on every call."""
    import pandas as pd
    from onboarding_poc.config import mdm_table
    out = {}
    for csv, table in (("erp_product_master.csv", cfg.mdm.product_table),
                       ("rb_customer_master.csv", cfg.mdm.customer_table)):
        df = pd.read_csv(mdm_dir / csv, dtype=str, keep_default_na=False)
        fqn = mdm_table(cfg, table)
        out[fqn] = warehouse.write_frame(df, fqn, mode="overwrite")
    return out


def seed(cfg: AppConfig | None = None, storage=None, warehouse=None) -> dict:
    cfg = cfg or get_config()
    st = storage or build_storage(cfg)
    with tempfile.TemporaryDirectory() as d:
        paths = build(d)
        root = Path(d)
        st.write_bytes(f"{cfg.paths.mdm}/{cfg.mdm.product_file}", (root / "masters/erp_product_master.csv").read_bytes())
        st.write_bytes(f"{cfg.paths.mdm}/{cfg.mdm.customer_file}", (root / "masters/rb_customer_master.csv").read_bytes())
        for p in (root / "veritas").glob("*.json"):
            st.write_bytes(f"{cfg.paths.veritas_reference}/{p.name}", p.read_bytes())
        samples = {}
        for name in ("tesco", "tesco_daily", "dmart"):
            p = paths[name]
            key = f"{cfg.paths.landing}/{p.parent.parent.name}/{p.parent.name}/{p.name}"
            st.write_bytes(key, p.read_bytes())
            samples[name] = key
        st.write_bytes("samples/ground_truth/product_xref.csv", (root / "ground_truth/product_xref.csv").read_bytes())
        st.write_bytes("samples/ground_truth/outlet_xref.csv", (root / "ground_truth/outlet_xref.csv").read_bytes())
        if cfg.mdm.source == "uc" and warehouse is not None:
            try:
                samples["rb_master"] = seed_rb_master(cfg, warehouse, root / "masters")
            except Exception as exc:            # files are in; report the table problem to the caller
                samples["rb_master_error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    return samples


if __name__ == "__main__":
    print(seed())

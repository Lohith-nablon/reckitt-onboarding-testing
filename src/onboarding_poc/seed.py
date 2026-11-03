"""Dummy dataset for demos and tests: MDM extracts, a Veritas reference mapping (#4285) and
sample retailer files, written to the configured storage (local folder or S3).

    python -m onboarding_poc.seed            # into the configured storage
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from onboarding_poc.config import AppConfig, get_config
from onboarding_poc.core.sample_data import build
from onboarding_poc.storage import build_storage


def seed(cfg: AppConfig | None = None, storage=None) -> dict:
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
            key = f"samples/{p.parent.parent.name}/{p.parent.name}/{p.name}"
            st.write_bytes(key, p.read_bytes())
            samples[name] = key
        st.write_bytes("samples/ground_truth/product_xref.csv", (root / "ground_truth/product_xref.csv").read_bytes())
        st.write_bytes("samples/ground_truth/outlet_xref.csv", (root / "ground_truth/outlet_xref.csv").read_bytes())
    return samples


if __name__ == "__main__":
    print(seed())

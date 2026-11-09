"""End-to-end on the Tesco daily sample: Phase 1 (mapping -> JSON + SQL), the pipeline load (local
stand-in), Phase 2 (harmonisation on the loaded tables -> silver), then the learning loop."""
from conftest import USER, run_phase1, run_phase2, run_pipeline, run_to_signoff


def test_first_file_full_journey(svc, tesco):
    s, f = tesco
    st = run_phase1(svc, s)
    assert s.stage == "pipeline" and s.gates["A"].status == "approved"
    item = next(i for i in st["items"] if i["target_table"] == "bronze.pos_sellout")
    sql = svc.ctx.storage.read_text(item["sql_key"])
    assert "INSERT INTO bronze_dev.pos.pos_sellout" in sql and "read_files(" in sql and "DELETE FROM" in sql
    # the mapping JSON is kept in Delta format (decided 5 Nov), with a plain .json copy as a fallback
    assert svc.ctx.storage.exists(item["sql_key"].replace(".sql", ".veritas.json"))
    from deltalake import DeltaTable
    from onboarding_poc.mapping_store import MappingStore
    ms = MappingStore(svc.ctx)
    assert ms.mode == "delta_path"
    DeltaTable(ms.location())                                     # a real Delta table on disk
    doc = ms.mapping_json(s.session_id, item["mapping_id"])
    assert doc["Dataset"] == "POS_SELLOUT" and len(doc["Mappings"]) == 38
    run_phase1(svc, s)                                            # re-publishing replaces, never duplicates
    assert len(ms.read(s.session_id)) == len(st["items"])
    # nothing loaded until the pipeline runs, and Phase 2 refuses to start
    assert not svc.pipeline.status(s)["all_loaded"]
    import pytest
    with pytest.raises(ValueError):
        svc.pipeline.confirm(s, USER)
    loaded = run_pipeline(svc, s)
    pos = next(i for i in loaded["items"] if i["target_table"] == "bronze.pos_sellout")
    assert pos["loaded_rows"] == f.sheets[0].rows
    reps, manifest = run_phase2(svc, s)
    r = next(x for x in reps if x["target_table"] == "bronze.pos_sellout")
    assert r["rows"] == f.sheets[0].rows and not r["blocking"]
    # KPI: generated mapping equals the Veritas mapping #4285
    assert r["veritas_compare"]["match_rate"] >= svc.ctx.cfg.thresholds.veritas_match_target
    kinds = {o["kind"] for o in manifest["outputs"]}
    assert {"silver", "crosswalk", "discovery_report", "validation"} <= kinds
    prod = svc.ctx.warehouse.query("SELECT * FROM silver_dev.pos.retailer_product_harmonised")
    assert prod["rb_product_code"].notna().mean() > 0.9 and prod["global_brand"].notna().any()
    s = svc.session(s.session_id)
    assert s.status == "signed_off" and all(g.status == "approved" for g in s.gates.values())
    assert len(svc.library.entries()) == len(manifest["library"]) >= 1


def test_repeat_file_skips_review(svc, tesco):
    s, _ = tesco
    run_to_signoff(svc, s)
    s2 = svc.intake.create("Tesco daily week 2", "UK", "TESCO", USER)
    data = svc.ctx.storage.read_bytes(svc.samples["tesco_daily"]).replace(b"2026-10-1", b"2026-10-2")
    f2 = svc.intake.add_upload(s2, "tesco_toolkit_daily_2026-10-18.csv", data, USER)
    assert f2.known_layout
    out = svc.analyse(s2, USER)
    assert s2.gates["A"].decided_by == "library" and out["published"] and s2.stage == "pipeline"
    run_pipeline(svc, s2)
    h = svc.run_harmonisation(s2, USER)
    assert h["gate_b"]["status"] == "approved" and h["summary"]["product"]["crosswalk"] >= 1
    steps = {x["module"]: x["status"] for x in svc.journey.build(s2)["files"][0]["steps"]}
    assert steps["M2"] == "skipped" and steps["M3"] == "skipped"
    svc.validate.run(s2, USER)
    svc.output.signoff(s2, USER)
    assert any(e["version"] == 2 for e in svc.library.entries())
    # silver keeps both sessions' rows side by side
    n = svc.ctx.warehouse.query("SELECT count(DISTINCT onboarding_session_id) AS n FROM silver_dev.pos.pos_sellout_harmonised")
    assert int(n.iloc[0, 0]) == 2


def test_rerunning_the_sql_does_not_duplicate(svc, tesco):
    s, f = tesco
    run_phase1(svc, s)
    svc.pipeline.run_locally(s, USER)
    svc.pipeline.run_locally(s, USER)
    pos = next(i for i in svc.pipeline.status(s)["items"] if i["target_table"] == "bronze.pos_sellout")
    assert pos["loaded_rows"] == f.sheets[0].rows


def test_other_market_file(svc):
    s = svc.intake.create("DMart", "IN", "DMART", USER)
    svc.intake.add_upload(s, "dmart_sellout_2026-10-04.csv", svc.ctx.storage.read_bytes(svc.samples["dmart"]), USER)
    out = svc.analyse(s, USER)
    assert out["mappings"] and out["discovery"][0]["open_questions"] >= 1

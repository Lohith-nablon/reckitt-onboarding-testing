"""End-to-end: M1-M8 on the Tesco daily sample, then the learning loop on a repeat file."""
from conftest import USER, run_to_signoff


def test_first_file_full_journey(svc, tesco):
    s, f = tesco
    assert not f.known_layout and f.sheets[0].rows > 100
    out = svc.analyse(s, USER)
    assert out["gate_a"]["status"] == "open"
    targets = {m["target_table"] for m in out["mappings"]}
    assert "bronze.pos_sellout" in targets
    reps, manifest = run_to_signoff(svc, s)
    pos = next(r for r in reps if r["target_table"] == "bronze.pos_sellout")
    assert pos["rows"] == f.sheets[0].rows and not pos["blocking"]
    # KPI: generated mapping equals the Veritas mapping #4285
    assert pos["veritas_compare"]["match_rate"] >= svc.ctx.cfg.thresholds.veritas_match_target
    kinds = {o["kind"] for o in manifest["outputs"]}
    assert {"bronze", "veritas_mapping", "mapping_spec", "discovery_report", "validation"} <= kinds
    st = svc.ctx.storage
    bronze = next(o for o in manifest["outputs"] if o["kind"] == "bronze" and o["table"] == "bronze.pos_sellout")
    header = st.read_text(bronze["key"]).splitlines()[0]
    assert header.startswith("DATE,LEGAL_ENTITY_CODE") and "MEASURE_20" in header   # physical POS_SELLOUT layout
    assert st.exists(f"outputs/{s.session_id}/trace.json") and st.exists(f"outputs/{s.session_id}/manifest.json")
    s = svc.session(s.session_id)
    assert s.status == "signed_off" and all(g.status == "approved" for g in s.gates.values())
    assert len(svc.library.entries()) == len(manifest["library"]) >= 1


def test_repeat_file_skips_discovery_and_mapping(svc, tesco):
    s, _ = tesco
    run_to_signoff(svc, s)
    s2 = svc.intake.create("Tesco daily week 2", "UK", "TESCO", USER)
    data = svc.ctx.storage.read_bytes(svc.samples["tesco_daily"]).replace(b"2026-10-1", b"2026-10-2")
    f2 = svc.intake.add_upload(s2, "tesco_toolkit_daily_2026-10-18.csv", data, USER)
    assert f2.known_layout
    svc.analyse(s2, USER)
    assert s2.gates["A"].status == "approved" and s2.gates["A"].decided_by == "library"
    h = svc.run_harmonisation(s2, USER)
    assert h["gate_b"]["status"] == "approved"                 # codes come from the crosswalk / auto-accept
    assert h["summary"]["product"]["crosswalk"] >= 1           # reviewer decisions from session 1 reused
    steps = {x["module"]: x["status"] for x in svc.journey.build(s2)["files"][0]["steps"]}
    assert steps["M2"] == "skipped" and steps["M3"] == "skipped"
    svc.validate.run(s2, USER)
    svc.output.signoff(s2, USER)
    assert svc.library.entries()[0]["version"] == 2 or any(e["version"] == 2 for e in svc.library.entries())


def test_other_market_file(svc):
    s = svc.intake.create("DMart", "IN", "DMART", USER)
    data = svc.ctx.storage.read_bytes(svc.samples["dmart"])
    svc.intake.add_upload(s, "dmart_sellout_2026-10-04.csv", data, USER)
    out = svc.analyse(s, USER)
    assert out["mappings"] and out["discovery"][0]["open_questions"] >= 1

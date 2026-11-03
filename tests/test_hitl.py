"""AI suggests, people decide: overrides, gates, and the trace behind every decision."""
import pytest

from conftest import USER, FakeLLM


def _pos(svc, s):
    return next(d for d in svc.mapping.all(s) if d["spec"]["target_table"] == "bronze.pos_sellout")


def test_override_is_kept_and_traced(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    mid = _pos(svc, s)["spec"]["mapping_id"]
    link = svc.mapping.update_link(s, mid, "attribute_1", {"action": "DIRECT_TEXT", "value": "POC"}, USER, "test")
    assert link["state"] == "human_modified" and link["decided_by"] == USER and link["confidence"] == 1.0
    svc.mapping.propose(s, USER)                 # re-proposal keeps the person's change
    c = next(x for x in svc.mapping.canvas(s, mid)["links"] if x["target"] == "attribute_1")
    assert c["state"] == "human_modified" and c["value"] == "POC"
    ev = [e for e in svc.ctx.store.events(s.session_id) if e["step"] == "mapping.link_changed"][-1]
    assert ev["decided_by"] == USER and ev["before"] != ev["after"] and ev["why"] == "test"


def test_gate_a_refuses_unmapped_mandatory(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    d = _pos(svc, s)
    mand = next(c["target_column"] for c in d["spec"]["columns"] if c["mandatory"] and c["action"])
    svc.mapping.update_link(s, d["spec"]["mapping_id"], mand, {"action": None}, USER)
    with pytest.raises(ValueError, match="mandatory"):
        svc.mapping.approve(s, USER)


def test_change_after_approval_reopens_gate(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    svc.mapping.approve(s, USER)
    svc.mapping.update_link(s, _pos(svc, s)["spec"]["mapping_id"], "attribute_2", {"action": "DIRECT_TEXT", "value": "x"}, USER)
    assert svc.session(s.session_id).gates["A"].status == "reopened"


def test_bad_links_are_rejected(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    mid = _pos(svc, s)["spec"]["mapping_id"]
    with pytest.raises(ValueError, match="not in file"):
        svc.mapping.update_link(s, mid, "attribute_1", {"action": "DIRECT_COLUMN", "source_columns": ["nope"]}, USER)
    with pytest.raises(ValueError, match="expression"):
        svc.mapping.update_link(s, mid, "attribute_1", {"action": "EXPRESSION", "expression": "__import__('os')"}, USER)


def test_gate_b_needs_review_decisions(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    svc.mapping.approve(s, USER)
    svc.run_harmonisation(s, USER)
    pending = [r for r in svc.codes.queue(s, only_open=True) if r["status"] == "needs_review"]
    if pending:
        with pytest.raises(ValueError, match="undecided"):
            svc.approve_gate_b(s, USER)
        r = pending[0]
        alt = r["candidates"][-1]["code"]
        svc.codes.decide(s, r["match_id"], "choose", USER, rb_code=alt, note="picked from top 5")
        assert len(r["candidates"]) <= 5
    with pytest.raises(ValueError):
        svc.output.signoff(s, USER)          # Gate B / validation still open


def test_value_rule_change_edits_mapping(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    svc.mapping.approve(s, USER)
    rule = next(r for r in svc.values.rules(s) if r["kind"] == "date_format")
    assert rule["parse_rate"] > 0.98 and rule["examples"][0]["after"].isdigit()
    upd = svc.values.decide(s, rule["rule_id"], USER, value="dd-MM-yyyy", note="test")
    assert upd["from"] == "dd-MM-yyyy" and upd["issue"]           # wrong format now flagged
    assert svc.session(s.session_id).gates["A"].status == "reopened"


def test_discovery_with_ai_and_human_type(cfg):
    from onboarding_poc.context import Context
    from onboarding_poc.seed import seed
    from onboarding_poc.service import OnboardingService
    llm = FakeLLM()
    svc = OnboardingService(cfg, Context(cfg, llm=llm))
    samples = seed(cfg, svc.ctx.storage)
    s = svc.intake.create("t", "UK", "TESCO", USER)
    f = svc.intake.add_upload(s, "t.csv", svc.ctx.storage.read_bytes(samples["tesco_daily"]), USER)
    rep = svc.discovery.run(s, USER)[0]
    assert "discovery" in llm.calls and rep["decided_by"].startswith("ai:")
    assert rep["sheets"][0]["table"]["description"] == "Daily store sales per product"
    assert any(q["topic"] == "ai" for q in rep["open_questions"])
    col = rep["sheets"][0]["columns"][0]["column"]
    out = svc.discovery.set_column_type(s, f.file_id, None, col, "supplier_code", USER, "per retailer")
    assert out["source"] == "human" and out["confidence"] == 1.0
    sd = svc.discovery.sheet_discoveries(s, f.file_id)[0]
    assert sd.column_hints[col].source == "human"


def test_trace_answers_provenance(svc, tesco):
    s, _ = tesco
    svc.analyse(s, USER)
    for e in svc.ctx.store.events(s.session_id):
        assert e["module"].startswith("M") and e["decided_by"] and e["at"] and e["step"]

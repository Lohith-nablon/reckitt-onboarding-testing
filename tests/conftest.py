from __future__ import annotations

import pytest

from onboarding_poc.config import load_config
from onboarding_poc.seed import seed
from onboarding_poc.service import OnboardingService

USER = "reviewer@nablon.ai"


class FakeLLM:
    """Stands in for model serving: answers each prompt kind with a fixed, schema-valid object."""
    available = True
    label = "fake:gpt-4o"
    provider = "fake"

    def __init__(self):
        self.calls = []

    def structured(self, prompt, schema, kind):
        self.calls.append(kind)
        if kind == "discovery":
            return schema.model_validate({
                "table": {"description": "Daily store sales per product", "entity": "sales", "grain": [],
                          "period": "daily", "data_quality_notes": ["sample note"]},
                "columns": [], "questions": ["Is the file net of returns?"]})
        return None


@pytest.fixture()
def cfg(tmp_path):
    return load_config(path="/nonexistent", storage={"backend": "local", "local_root": str(tmp_path / "store")},
                       warehouse={"backend": "local", "local_root": str(tmp_path / "warehouse")},
                       frontend_dist=str(tmp_path / "no_dist"))


@pytest.fixture()
def svc(cfg):
    s = OnboardingService(cfg)
    s.samples = seed(cfg, s.ctx.storage)
    return s


@pytest.fixture()
def tesco(svc):
    """Session with the Tesco daily file added."""
    s = svc.intake.create("Tesco daily", "UK", "TESCO", USER)
    data = svc.ctx.storage.read_bytes(svc.samples["tesco_daily"])
    f = svc.intake.add_upload(s, "tesco_toolkit_daily_2026-10-11.csv", data, USER)
    return s, f


def run_phase1(svc, s, user=USER):
    """Discovery, mapping, conversions, Gate A -> mapping JSON + SQL published."""
    svc.analyse(s, user)
    if s.gates["A"].status != "approved":
        for x in svc.values.open_issues(s):
            svc.values.decide(s, x["rule_id"], user)
        svc.approve_gate_a(s, user)
    return svc.pipeline.status(s, check=False)


def run_pipeline(svc, s, user=USER):
    """What Reckitt's pipeline does, played by the local DuckDB stand-in."""
    svc.pipeline.run_locally(s, user)
    return svc.pipeline.confirm(s, user, "loaded")


def run_phase2(svc, s, user=USER):
    svc.run_harmonisation(s, user)
    if s.gates["B"].status != "approved":
        for r in svc.codes.queue(s, only_open=True):
            if r["status"] == "needs_review":
                svc.codes.decide(s, r["match_id"], "accept" if r["rb_code"] else "leave_empty", user)
        for x in svc.standards.open_issues(s, svc.validate.frames(s)):
            svc.standards.decide(s, x["rule_id"], user)
        svc.approve_gate_b(s, user)
    reps = svc.validate.run(s, user)
    return reps, svc.output.signoff(s, user, "test sign-off")


def run_to_signoff(svc, s, user=USER):
    run_phase1(svc, s, user)
    run_pipeline(svc, s, user)
    return run_phase2(svc, s, user)

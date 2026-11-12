"""Automated hand-off (pipeline job / watch_table), RB codes from Unity Catalog, landing inbox,
audit table and metrics."""
from __future__ import annotations

import time

import pytest

from onboarding_poc.config import load_config
from onboarding_poc.connectivity import check
from onboarding_poc.jobs import DatabricksJobRunner, _state
from onboarding_poc.seed import seed
from onboarding_poc.service import OnboardingService

from conftest import USER, run_phase1, run_phase2


def _svc(tmp_path, **over):
    cfg = load_config(path="/nonexistent", storage={"backend": "local", "local_root": str(tmp_path / "store")},
                      warehouse={"backend": "local", "local_root": str(tmp_path / "warehouse")},
                      frontend_dist=str(tmp_path / "no_dist"), **over)
    svc = OnboardingService(cfg)
    seed(cfg, svc.ctx.storage, svc.ctx.warehouse)
    return svc


def _tesco_key(svc):
    return next(i["key"] for i in svc.inbox() if "toolkit_daily" in i["key"])


def test_job_mode_runs_pipeline_and_starts_phase2(tmp_path):
    svc = _svc(tmp_path, pipeline={"mode": "job"}, mdm={"source": "uc"})
    r = svc.start_from_landing(_tesco_key(svc), USER)
    s = svc.session(r["session_id"])
    assert (s.market, s.retailer) == ("UK", "TESCO")
    run_phase1(svc, s)
    s = svc.session(s.session_id)
    assert s.stage == "pipeline" and s.outputs["pipeline_run"]["state"] == "success"
    assert s.outputs["pipeline_run"]["sql_key"].endswith("/pipeline/run-1.sql")
    st = svc.pipeline_status(s.session_id)
    s = svc.session(s.session_id)
    assert s.stage == "harmonisation" and s.outputs["pipeline"]["confirmed_by"] == "pipeline-job"
    assert s.outputs["phase2_auto"]["ok"] and s.gates["B"].status == "open"
    assert {i["loaded_rows"] for i in st["items"]} == {745, 20}
    # Phase 2 matched against the RB master tables in UC
    assert svc.ctx.mdm.info()["source"] == "uc" and svc.ctx.mdm.info()["erp_product"]["rows"] == 30
    run_phase2(svc, s)
    # a repeat of the same layout goes all the way to validate on its own
    r2 = svc.start_from_landing(_tesco_key(svc), USER)
    svc.pipeline_status(r2["session_id"])
    s2 = svc.session(r2["session_id"])
    assert s2.gates["A"].decided_by == "library" and s2.gates["B"].status == "approved" and s2.stage == "validate"


def test_job_start_failure_is_shown_not_raised(tmp_path):
    svc = _svc(tmp_path, pipeline={"mode": "job"})
    s = svc.session(svc.start_from_landing(_tesco_key(svc), USER, analyse=False)["session_id"])

    class Broken:
        kind = "databricks"

        def start(self, params, work=None):
            raise RuntimeError("PERMISSION_DENIED: no CAN_MANAGE_RUN")

    svc.ctx._jobs = Broken()
    run_phase1(svc, s)
    s = svc.session(s.session_id)
    assert s.gates["A"].status == "approved" and s.stage == "pipeline"
    assert s.outputs["pipeline_run"]["state"] == "failed" and "CAN_MANAGE_RUN" in s.outputs["pipeline_run"]["message"]


def test_watch_table_mode(tmp_path):
    svc = _svc(tmp_path, pipeline={"mode": "watch_table", "auto_phase2": False})
    s = svc.session(svc.start_from_landing(_tesco_key(svc), USER, analyse=False)["session_id"])
    run_phase1(svc, s)
    svc.pipeline_status(s.session_id)
    assert svc.session(s.session_id).stage == "pipeline"           # nothing loaded yet
    svc.pipeline.run_locally(svc.session(s.session_id), USER)       # "Reckitt" runs its pipeline
    svc.pipeline_status(s.session_id)
    s = svc.session(s.session_id)
    assert s.stage == "harmonisation" and s.outputs["pipeline"]["confirmed_by"] == "pipeline-watch"
    assert "phase2_auto" not in s.outputs


def test_databricks_runner_states():
    assert _state("RUNNING", None) == "running" and _state("QUEUED", None) == "pending"
    assert _state("TERMINATED", "SUCCESS") == "success" and _state("TERMINATED", "FAILED") == "failed"

    class St:
        def __init__(self, v):
            self.value = v

    class Run:
        run_page_url = "https://x/run/7"

        class state:
            life_cycle_state = St("TERMINATED")
            result_state = St("SUCCESS")
            state_message = ""

    class Jobs:
        def run_now(self, job_id, job_parameters):
            assert job_id == 42 and job_parameters["sql_path"].startswith("/Volumes/")
            return type("R", (), {"run_id": 7})()

        def get_run(self, run_id):
            return Run()

    r = DatabricksJobRunner("42", client=type("W", (), {"jobs": Jobs()})())
    info = r.start({"session_id": "s", "sql_path": "/Volumes/a/b/c/x.sql"})
    assert info["run_id"] == "7" and info["state"] == "success" and info["url"].endswith("/7")
    with pytest.raises(ValueError):
        DatabricksJobRunner("")


def test_metrics_audit_and_check(tmp_path):
    svc = _svc(tmp_path, mdm={"source": "uc"}, ops={"audit": "on"})
    s = svc.session(svc.start_from_landing(_tesco_key(svc), USER)["session_id"])
    files = svc.discovery.load(s, s.files[0].file_id)["report"]["sheets"][0]["columns"]
    col = next(c for c in files if c["needs_review"])
    svc.discovery.set_column_type(s, s.files[0].file_id, None, col["column"], col["semantic_type"], USER)
    m = svc.metrics.session(svc.session(s.session_id))
    assert m["mapping"]["rag"] == "green" and m["mapping"]["pct_ge_90"] == 1.0
    assert m["manual"]["by_step"] == {"Discovery": 1}
    sysm = svc.metrics.system()
    assert sysm["totals"]["sessions"] == 1 and sysm["monthly"][0]["files"] == 1
    time.sleep(0.1)
    svc.ctx.audit.flush()
    for _ in range(50):
        if svc.ctx.audit.written:
            break
        time.sleep(0.2)
    res = check(svc.ctx)
    by = {t["name"]: t for t in res["tables"]}
    assert res["ok"] and by["rb_product_master"]["rows"] == 30 and by["audit"]["status"] == "ok"


def test_concatenation_link_change_saves(tmp_path):
    svc = _svc(tmp_path)
    key = next(i["key"] for i in svc.inbox() if "dmart" in i["key"])
    s = svc.session(svc.start_from_landing(key, USER)["session_id"])
    link = svc.mapping.update_link(s, s.mapping_ids[0], "retailer_outlet_code",
                                   {"action": "CONCATENATION", "source_columns": ["Site Code"],
                                    "parts": [{"column": "Site Code", "text": None}, {"column": None, "text": "_DMart_Daily"},
                                              {"column": None, "text": ""}]}, USER)
    assert link["action"] == "CONCATENATION" and len(link["parts"]) == 2 and link["state"] == "human_modified"

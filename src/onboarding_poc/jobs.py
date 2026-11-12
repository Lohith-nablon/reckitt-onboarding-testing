"""Starting and watching the pipeline job (pipeline.mode = job).

DatabricksJobRunner  Jobs API through databricks-sdk. Inside a Databricks App it runs as the app's
                     service principal, which needs CAN_MANAGE_RUN on the job (add the job to the
                     app as resource "pipeline-job").
LocalJobRunner       development stand-in: runs the work in-process and reports it as a finished run.

A run is reported as {run_id, state, message, url, started_at, finished_at} with state one of
pending | running | success | failed.
"""
from __future__ import annotations

import uuid
from typing import Callable

from onboarding_poc.session import now

FINAL = {"success", "failed"}


def _state(life: str | None, result: str | None) -> str:
    if life in (None, "PENDING", "QUEUED", "BLOCKED", "WAITING_FOR_RETRY"):
        return "pending"
    if life in ("RUNNING", "TERMINATING"):
        return "running"
    if life == "TERMINATED" and result == "SUCCESS":
        return "success"
    return "failed"        # TERMINATED with FAILED / TIMEDOUT / CANCELED, SKIPPED, INTERNAL_ERROR


class DatabricksJobRunner:
    kind = "databricks"

    def __init__(self, job_id: str, client=None):
        if not job_id:
            raise ValueError("pipeline.mode is job but pipeline.job_id is empty: add the job to the app as "
                             "resource 'pipeline-job' (POC_PIPELINE__JOB_ID)")
        self.job_id = int(job_id)
        if client is None:
            from databricks.sdk import WorkspaceClient
            client = WorkspaceClient()
        self.w = client

    def start(self, params: dict[str, str], work: Callable | None = None) -> dict:
        r = self.w.jobs.run_now(job_id=self.job_id, job_parameters=params)
        run_id = r.run_id if hasattr(r, "run_id") else r.response.run_id
        info = self.status(run_id)
        info["started_at"] = now()
        return info

    def status(self, run_id) -> dict:
        run = self.w.jobs.get_run(int(run_id))
        st = run.state
        life = st.life_cycle_state.value if st and st.life_cycle_state else None
        result = st.result_state.value if st and st.result_state else None
        return {"run_id": str(run_id), "job_id": str(self.job_id), "state": _state(life, result),
                "life_cycle": life, "result": result, "message": (st.state_message if st else None) or None,
                "url": run.run_page_url, "finished_at": now() if _state(life, result) in FINAL else None}


class LocalJobRunner:
    """Runs the pipeline work immediately in this process (DuckDB stand-in)."""
    kind = "local"

    def __init__(self):
        self.runs: dict[str, dict] = {}

    def start(self, params: dict[str, str], work: Callable | None = None) -> dict:
        run_id = "local-" + uuid.uuid4().hex[:8]
        info = {"run_id": run_id, "job_id": "local", "state": "running", "life_cycle": "RUNNING", "result": None,
                "message": None, "url": None, "started_at": now(), "finished_at": None}
        try:
            if work:
                work()
            info.update(state="success", life_cycle="TERMINATED", result="SUCCESS", finished_at=now())
        except Exception as exc:
            info.update(state="failed", life_cycle="TERMINATED", result="FAILED", message=str(exc)[:500],
                        finished_at=now())
        self.runs[run_id] = info
        return dict(info)

    def status(self, run_id) -> dict:
        return dict(self.runs.get(str(run_id)) or {"run_id": str(run_id), "state": "failed",
                                                     "message": "local run not found (app restarted)"})


def build_runner(cfg):
    if cfg.warehouse.backend == "databricks":
        return DatabricksJobRunner(cfg.pipeline.job_id)
    return LocalJobRunner()

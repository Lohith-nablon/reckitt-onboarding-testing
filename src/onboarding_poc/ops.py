"""Operational Delta tables in Unity Catalog (catalog = uc.bronze_catalog, schema = ops.schema_name):

  onboarding_audit           every trace event (who, what, when, why), written in small batches
  onboarding_landing_events  file arrivals, written by the intake job (file-arrival trigger)

The session JSON in storage stays the source of truth for the app; these tables make the same
history queryable in the SQL editor and dashboards.
"""
from __future__ import annotations

import json
import queue
import threading

import pandas as pd
from loguru import logger

from onboarding_poc.config import ops_table

AUDIT_COLUMNS = ["event_id", "at", "session_id", "retailer", "market", "module", "step", "decided_by",
                 "subject", "source", "why", "confidence", "before_json", "after_json", "version"]


def _j(v) -> str | None:
    if v is None:
        return None
    try:
        return json.dumps(v, default=str)[:20000]
    except Exception:
        return str(v)[:20000]


class AuditSink:
    """Collects trace events and appends them to the audit table from a background thread, so a
    slow SQL warehouse never holds up a screen. Failures are logged and the batch is dropped."""

    def __init__(self, ctx, flush_every_s: float = 3.0):
        self.ctx = ctx
        self.q: queue.Queue = queue.Queue()
        self.flush_every_s = flush_every_s
        self.last_error: str | None = None
        self.written = 0
        self._started = False
        self._lock = threading.Lock()

    @property
    def table(self) -> str:
        return ops_table(self.ctx.cfg, self.ctx.cfg.ops.audit_table)

    def put(self, s, ev: dict) -> None:
        row = {"event_id": ev["event_id"], "at": ev["at"], "session_id": ev["session_id"], "retailer": s.retailer,
               "market": s.market, "module": ev["module"], "step": ev["step"], "decided_by": ev["decided_by"],
               "subject": ev.get("subject"), "source": ev.get("source"), "why": ev.get("why"),
               "confidence": None if ev.get("confidence") is None else str(ev["confidence"]),
               "before_json": _j(ev.get("before")), "after_json": _j(ev.get("after")), "version": str(ev.get("version"))}
        self.q.put(row)
        self._start()

    def _start(self) -> None:
        with self._lock:
            if not self._started:
                threading.Thread(target=self._loop, name="audit-sink", daemon=True).start()
                self._started = True

    def _loop(self) -> None:
        while True:
            rows = [self.q.get()]
            try:
                while True:
                    rows.append(self.q.get(timeout=self.flush_every_s))
                    if len(rows) >= 200:
                        break
            except queue.Empty:
                pass
            self._write(rows)

    def _write(self, rows: list[dict]) -> None:
        try:
            self.ctx.warehouse.write_frame(pd.DataFrame(rows, columns=AUDIT_COLUMNS), self.table)
            self.written += len(rows)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)[:300]
            logger.warning("audit write to {} failed ({} events dropped): {}", self.table, len(rows), exc)

    def flush(self) -> None:
        """Write whatever is queued, now (tests, shutdown)."""
        rows = []
        while not self.q.empty():
            rows.append(self.q.get_nowait())
        if rows:
            self._write(rows)


def landing_events(ctx) -> dict[str, dict]:
    """Arrivals recorded by the intake job, keyed by storage key. Empty when the table is not there."""
    fqn = ops_table(ctx.cfg, ctx.cfg.ops.landing_events_table)
    try:
        if not ctx.warehouse.table_exists(fqn):
            return {}
        df = ctx.warehouse.query(f"SELECT * FROM {fqn}")
    except Exception as exc:
        logger.debug("landing events not readable: {}", exc)
        return {}
    root = ctx.storage.uri("").rstrip("/")
    out = {}
    for r in df.astype(object).where(df.notna(), None).to_dict("records"):
        path = str(r.get("file_path") or "")
        key = path[len(root) + 1:] if root and path.startswith(root) else path
        out[key] = r
    return out

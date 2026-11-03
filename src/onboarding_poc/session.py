"""Onboarding session state and the trace (decision log).

One session = one retailer onboarding: its files, its target schema, every decision.
Stored as JSON in S3 (or the local folder):

  state/sessions/<id>/session.json         this model
  state/sessions/<id>/trace.json           every step and decision, append-only
  state/sessions/<id>/discovery/<file>.json
  state/sessions/<id>/mappings/<mapping_id>.json
  state/sessions/<id>/harmonisation/*.json
  state/sessions/<id>/validation/*.json

Every trace event answers the five provenance questions: what the input was (source), who
decided (AI, rules, library or a named person), why (rationale + confidence), when and on
which version, and what changed (before / after).
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal["start", "files", "discovery", "mapping", "harmonisation", "validate", "signoff", "done"]
STAGES: list[str] = ["start", "files", "discovery", "mapping", "harmonisation", "validate", "signoff", "done"]
GateStatus = Literal["not_started", "open", "approved", "reopened"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    return prefix + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]


class Gate(BaseModel):
    status: GateStatus = "not_started"
    decided_by: str | None = None
    decided_at: str | None = None
    note: str | None = None


class SheetState(BaseModel):
    sheet: str | None = None
    fingerprint: str
    rows: int
    columns: list[str]
    header_row: int = 0
    footer_rows: int = 0
    file_format: str = "csv"
    delimiter: str | None = None
    library_hit: str | None = None          # mapping library entry that covers this layout (M8)


class FileState(BaseModel):
    file_id: str
    name: str
    key: str                                 # storage key of the raw file
    source: str                              # upload | s3://...
    size_bytes: int
    sha256: str
    added_by: str
    added_at: str
    sheets: list[SheetState] = Field(default_factory=list)
    known_layout: bool = False               # every sheet covered by the mapping library
    status: str = "added"                    # added | discovered | mapped | harmonised | validated | signed_off
    error: str | None = None


class TargetState(BaseModel):
    source: Literal["default", "uploaded"] = "default"
    file_name: str | None = None
    key: str | None = None
    tables: dict[str, dict] = Field(default_factory=dict)     # table specs (YAML form)
    import_report: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    session_id: str
    name: str
    market: str
    retailer: str
    created_by: str
    created_at: str
    updated_at: str
    stage: Stage = "start"
    status: Literal["active", "signed_off", "archived"] = "active"
    profile: dict[str, dict] = Field(default_factory=dict)   # business fields: {key: {value, status, provided_by}}
    target: TargetState = TargetState()
    files: list[FileState] = Field(default_factory=list)
    gates: dict[str, Gate] = Field(default_factory=lambda: {"A": Gate(), "B": Gate(), "C": Gate()})
    mapping_ids: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    version: int = 0

    def file(self, file_id: str) -> FileState:
        for f in self.files:
            if f.file_id == file_id:
                return f
        raise KeyError(f"file {file_id} not in session {self.session_id}")


class TraceEvent(BaseModel):
    event_id: str
    at: str
    session_id: str
    module: str                     # M1..M8
    step: str                       # e.g. discovery.column_type, mapping.link_changed
    decided_by: str                 # ai:<model> | rules | library | system | <user email>
    subject: str | None = None      # file / column / code the decision is about
    source: str | None = None       # input it was based on (file + sheet, sample rows, MDM version)
    why: str | None = None
    confidence: float | None = None
    before: Any = None
    after: Any = None
    version: int | None = None      # session version when recorded


class SessionStore:
    """Reads and writes session JSON. One lock per process keeps read-modify-write safe inside
    the single Databricks App process; the version counter detects concurrent edits elsewhere."""

    def __init__(self, storage, sessions_prefix: str):
        self.st = storage
        self.prefix = sessions_prefix.strip("/")
        self._lock = threading.RLock()

    def key(self, sid: str, *parts: str) -> str:
        return "/".join([self.prefix, sid, *parts])

    def save(self, s: Session) -> Session:
        with self._lock:
            s.version += 1
            s.updated_at = now()
            self.st.write_json(self.key(s.session_id, "session.json"), s.model_dump())
            return s

    def load(self, sid: str) -> Session:
        raw = self.st.read_json(self.key(sid, "session.json"))
        if raw is None:
            raise KeyError(f"session {sid} not found")
        return Session.model_validate(raw)

    def list(self) -> list[Session]:
        out = []
        for k in self.st.list(self.prefix):
            if k.endswith("/session.json"):
                try:
                    out.append(Session.model_validate(self.st.read_json(k)))
                except Exception:
                    continue
        return sorted(out, key=lambda s: s.updated_at, reverse=True)

    # documents next to the session -------------------------------------------------
    def put(self, sid: str, rel: str, obj: Any) -> None:
        self.st.write_json(self.key(sid, rel), obj)

    def get(self, sid: str, rel: str, default: Any = None) -> Any:
        return self.st.read_json(self.key(sid, rel), default)

    def docs(self, sid: str, folder: str) -> list[Any]:
        return [self.st.read_json(k) for k in self.st.list(self.key(sid, folder)) if k.endswith(".json")]

    # trace -------------------------------------------------------------------------
    def trace(self, s: Session, module: str, step: str, decided_by: str, **kw) -> TraceEvent:
        ev = TraceEvent(event_id=uuid.uuid4().hex[:12], at=now(), session_id=s.session_id, module=module,
                        step=step, decided_by=decided_by, version=s.version, **kw)
        with self._lock:
            k = self.key(s.session_id, "trace.json")
            events = self.st.read_json(k, [])
            events.append(ev.model_dump())
            self.st.write_json(k, events)
        return ev

    def events(self, sid: str) -> list[dict]:
        return self.st.read_json(self.key(sid, "trace.json"), [])


def rows_jsonable(df) -> list[list]:
    """DataFrame -> list of rows with only str / None (no NaN, NA or dates) for the API."""
    import pandas as pd

    def cell(v):
        try:
            if v is None or pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        return v if isinstance(v, str) else str(v)
    return [[cell(v) for v in row] for row in df.itertuples(index=False, name=None)]

"""M8 File journey and learning.

Learning (what makes the second file of a type fast):
  * Mapping library: every signed-off mapping is stored in S3 as Veritas JSON plus the internal
    spec, keyed by market / retailer / layout fingerprint / target. A new file with a known
    layout reuses it: AI Discovery and mapping review are skipped (Gate A passes on its own).
  * Code crosswalk: every RB code a person accepted or corrected at Gate B is stored per
    retailer. Next time the same retailer code resolves instantly (method "crosswalk").

File journey: one timeline per file built from the trace, so anyone can see where a file is,
which steps were skipped because the layout was known, and who decided what.
"""
from __future__ import annotations

from typing import Any

from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.session import STAGES, Session, now

JOURNEY_STEPS = [
    ("M1", "Intake", "files"),
    ("M2", "AI Discovery", "discovery"),
    ("M3", "AI-assisted mapping", "mapping"),
    ("PIPELINE", "Reckitt pipeline load (manual)", "pipeline"),
    ("M4", "Code harmonisation", "harmonisation"),
    ("M5", "Value harmonisation", "harmonisation"),
    ("M6", "Enrich and validate", "validate"),
    ("M7", "Output and sign-off", "signoff"),
]


def _safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s)


class MappingLibrary:
    def __init__(self, ctx):
        self.ctx = ctx
        self.prefix = ctx.cfg.paths.mapping_library

    def _key(self, market: str, retailer: str, fingerprint: str, target: str) -> str:
        return f"{self.prefix}/{_safe(market)}/{_safe(retailer)}/{fingerprint}__{_safe(target)}.json"

    def get(self, market: str, retailer: str, fingerprint: str, target: str) -> dict | None:
        return self.ctx.storage.read_json(self._key(market, retailer, fingerprint, target))

    def for_layout(self, market: str, retailer: str, fingerprint: str) -> list[dict]:
        base = f"{self.prefix}/{_safe(market)}/{_safe(retailer)}/{fingerprint}__"
        return [self.ctx.storage.read_json(k) for k in self.ctx.storage.list(f"{self.prefix}/{_safe(market)}/{_safe(retailer)}")
                if k.startswith(base) and k.endswith(".json")]

    def entries(self) -> list[dict]:
        out = []
        for k in self.ctx.storage.list(self.prefix):
            if k.endswith(".json"):
                e = self.ctx.storage.read_json(k)
                out.append({"key": k, "market": e["spec"]["market"], "retailer": e["spec"]["retailer"],
                            "target_table": e["spec"]["target_table"], "fingerprint": e["spec"]["source"]["template_fingerprint"],
                            "version": e["version"], "approved_by": e.get("approved_by"), "approved_at": e.get("approved_at"),
                            "file_name": e.get("file_name"), "session_id": e.get("session_id"),
                            "times_reused": e.get("times_reused", 0)})
        return out

    def save(self, spec: MappingSpec, veritas_doc: dict | None, session: Session, file_name: str, user: str) -> dict:
        key = self._key(spec.market, spec.retailer, spec.source.template_fingerprint, spec.target_table)
        prev = self.ctx.storage.read_json(key)
        version = (prev or {}).get("version", 0) + 1
        history = (prev or {}).get("history", [])
        if prev:
            history.append({"version": prev["version"], "approved_by": prev.get("approved_by"),
                            "approved_at": prev.get("approved_at"), "session_id": prev.get("session_id")})
        entry = {"version": version, "spec": spec.model_dump(), "veritas": veritas_doc, "approved_by": user,
                 "approved_at": now(), "session_id": session.session_id, "file_name": file_name,
                 "times_reused": (prev or {}).get("times_reused", 0), "history": history}
        self.ctx.storage.write_json(key, entry)
        return {"key": key, "version": version}

    def mark_reused(self, entry: dict) -> None:
        spec = entry["spec"]
        key = self._key(spec["market"], spec["retailer"], spec["source"]["template_fingerprint"], spec["target_table"])
        entry["times_reused"] = entry.get("times_reused", 0) + 1
        self.ctx.storage.write_json(key, entry)

    def veritas_docs(self) -> list[dict]:
        """Veritas mappings known to the tool: signed-off library entries plus the reference
        exports from Veritas (used for conventions and for the Veritas compare KPI)."""
        docs = [e["veritas"] for e in (self.ctx.storage.read_json(k) for k in self.ctx.storage.list(self.prefix)
                                       if k.endswith(".json")) if e and e.get("veritas")]
        return docs + self.reference_docs()

    def reference_docs(self) -> list[dict]:
        p = self.ctx.cfg.paths.veritas_reference
        return [self.ctx.storage.read_json(k) for k in self.ctx.storage.list(p) if k.endswith(".json")]


class Crosswalk:
    def __init__(self, ctx):
        self.ctx = ctx
        self.prefix = ctx.cfg.paths.crosswalk

    def _key(self, entity: str, market: str, retailer: str) -> str:
        return f"{self.prefix}/{entity}/{_safe(market)}/{_safe(retailer)}.json"

    def load(self, entity: str, market: str, retailer: str) -> dict:
        return self.ctx.storage.read_json(self._key(entity, market, retailer), {"accepted": {}, "rejected": {}})

    def codes(self, entity: str, market: str, retailer: str) -> tuple[dict[str, str], set[str]]:
        d = self.load(entity, market, retailer)
        return {k: v["rb_code"] for k, v in d["accepted"].items()}, set(d["rejected"])

    def learn(self, entity: str, market: str, retailer: str, decisions: list[dict], user: str, session_id: str) -> int:
        d = self.load(entity, market, retailer)
        n = 0
        for x in decisions:
            k = x["source_key"]
            rec = {"by": user, "at": now(), "session_id": session_id, "method": x.get("method"),
                   "confidence": x.get("confidence"), "label": x.get("source_label")}
            if x["decision"] == "rejected":
                d["rejected"][k] = rec
                d["accepted"].pop(k, None)
            else:
                d["accepted"][k] = {**rec, "rb_code": x["rb_code"]}
                d["rejected"].pop(k, None)
            n += 1
        self.ctx.storage.write_json(self._key(entity, market, retailer), d)
        return n

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in self.ctx.storage.list(self.prefix):
            d = self.ctx.storage.read_json(k)
            out[k[len(self.prefix) + 1:]] = {"accepted": len(d["accepted"]), "rejected": len(d["rejected"])}
        return out


class Journey:
    def __init__(self, ctx):
        self.ctx = ctx

    def build(self, s: Session) -> dict:
        events = self.ctx.store.events(s.session_id)
        stage_idx = STAGES.index(s.stage)
        files = []
        for f in s.files:
            fe = [e for e in events if e.get("subject") and (e["subject"] == f.file_id or e["subject"].startswith(f.file_id + ":")
                                                              or e["subject"] == f.name)]
            steps = []
            for mod, label, stage in JOURNEY_STEPS:
                if mod == "PIPELINE":
                    mine = [e for e in events if e["step"].startswith("pipeline.") or e["step"] == "mapping.published"]
                else:
                    mine = [e for e in fe if e["module"] == mod and not e["step"].startswith("pipeline.")] or \
                        [e for e in events if e["module"] == mod and not e.get("subject") and not e["step"].startswith("pipeline.")]
                skipped = f.known_layout and mod in ("M2", "M3")
                if skipped:
                    status = "skipped"
                elif STAGES.index(stage) < stage_idx or s.status == "signed_off":
                    status = "done"
                elif STAGES.index(stage) == stage_idx:
                    status = "in_progress"
                else:
                    status = "not_started"
                last = mine[-1] if mine else None
                if mod == "PIPELINE":
                    label = {"job": "Pipeline job (started by the app)",
                             "watch_table": "Reckitt pipeline load (app watches bronze)"}.get(self.ctx.cfg.pipeline.mode, label)
                steps.append({"module": mod, "label": label, "status": status,
                              "note": "layout known: reused approved mapping from the library" if skipped else None,
                              "last_event": last and {k: last[k] for k in ("at", "step", "decided_by", "why")},
                              "events": len(mine)})
            files.append({"file_id": f.file_id, "name": f.name, "known_layout": f.known_layout, "status": f.status,
                          "steps": steps})
        people = sorted({e["decided_by"] for e in events if "@" in e["decided_by"]})
        by_actor: dict[str, int] = {}
        for e in events:
            a = "person" if "@" in e["decided_by"] else e["decided_by"].split(":")[0]
            by_actor[a] = by_actor.get(a, 0) + 1
        return {"session_id": s.session_id, "stage": s.stage, "status": s.status, "files": files,
                "gates": {k: g.model_dump() for k, g in s.gates.items()},
                "decisions": {"total": len(events), "by_actor": by_actor, "people": people}}

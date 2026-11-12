"""Mapping metrics and system KPIs.

Per session (Discovery and Mapping screens):
  * how sure the AI is: target columns that need a mapping (all but optional ones left empty) bucketed green / amber / red by mapping confidence
    (metrics.green, metrics.amber), share mapped at >= 90 / 80 / 70 %, columns set by a person;
  * effort: columns a person has to check x metrics.minutes_per_review;
  * manual inputs: every decision a person typed or picked, from the trace, by step;
  * time between the gates.

System (Metrics screen): totals, a monthly roll-up and accuracy over time across all sessions.
Everything is computed from the session JSON and trace, so it needs no extra tables.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from onboarding_poc.session import Session

SYSTEM_ACTORS = ("ai:", "rules", "library", "system", "pipeline-job", "pipeline-watch", "pipeline")

# trace steps that are a person's input, grouped by where it happened
MANUAL_STEPS = [("discovery.column_type_set", "Discovery"), ("discovery.question_answered", "Discovery"),
                ("mapping.link_changed", "Mapping"), ("mapping.link_accepted", "Mapping"),
                ("value_rule.", "Conversions"), ("codes.", "Code matching"), ("standard_rule.", "Value standards")]
NOT_MANUAL = {"codes.matched", "codes.skipped"}


def is_person(actor: str | None) -> bool:
    return bool(actor) and not any(actor == a or actor.startswith(a) for a in SYSTEM_ACTORS)


def rag(value: float | None, green: float, amber: float) -> str | None:
    if value is None:
        return None
    return "green" if value >= green else "amber" if value >= amber else "red"


def _pct(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _hours(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600, 2)
    except ValueError:
        return None


class Metrics:
    def __init__(self, ctx, discovery, mapping):
        self.ctx, self.discovery, self.mapping = ctx, discovery, mapping

    @property
    def th(self):
        return self.ctx.cfg.metrics

    # ── one session ─────────────────────────────────────────────────────────
    def mapping_quality(self, s: Session) -> dict:
        g, a = self.th.green, self.th.amber
        per, tot = [], defaultdict(int)
        for d in self.mapping.all(s):
            spec = self.mapping.spec(d)
            b = defaultdict(int)
            for c in spec.columns:
                b["total"] += 1
                state = (d.get("links", {}).get(c.target_column) or {}).get("state")
                checked = bool(c.reviewed_by) or state in ("accepted", "human_modified", "library")
                needed = bool(c.action) or c.mandatory or c.is_pk
                if not needed:
                    b["unmapped_optional"] += 1
                    continue
                # AI quality: what the AI proposed, scored on its own confidence (kept after a person accepts it)
                if c.action and not c.reviewed_by:
                    b["mapped_ai"] += 1
                    b[f"ai_{rag(c.confidence, g, a)}"] += 1
                    for t in (0.9, 0.8, 0.7):
                        if c.confidence >= t:
                            b[f"ge_{int(t * 100)}"] += 1
                # work left: a column a person accepted or set is done, whatever its confidence
                if checked and c.action:
                    b["person"] += 1
                elif not c.action:
                    b["unmapped_mandatory"] += 1
                else:
                    b[rag(c.confidence, g, a)] += 1
            review = b["amber"] + b["red"] + b["unmapped_mandatory"]
            row = {"mapping_id": spec.mapping_id, "file_name": d["file_name"], "sheet": d["sheet"],
                   "target_table": spec.target_table, **{k: b[k] for k in (
                       "total", "mapped_ai", "person", "green", "amber", "red", "unmapped_mandatory",
                       "unmapped_optional", "ge_90", "ge_80", "ge_70")},
                   "auto_ready": _pct(b["ge_90"], b["total"] - b["unmapped_optional"]), "to_review": review,
                   "veritas_match": (d.get("veritas_compare") or {}).get("match_rate")}
            row["rag"] = rag(row["auto_ready"], g, a)
            per.append(row)
            for k in ("total", "mapped_ai", "person", "green", "amber", "red", "unmapped_mandatory",
                      "unmapped_optional", "ge_90", "ge_80", "ge_70", "ai_green", "ai_amber", "ai_red"):
                tot[k] += b[k]
            tot["to_review"] += review
        total = tot["total"] - tot["unmapped_optional"]     # columns that need a mapping; optional NULLs are valid
        out = {k: tot[k] for k in ("total", "mapped_ai", "person", "green", "amber", "red", "unmapped_mandatory",
                                   "unmapped_optional", "to_review")}
        out.update(pct_ge_90=_pct(tot["ge_90"], total), pct_ge_80=_pct(tot["ge_80"], total),
                   pct_ge_70=_pct(tot["ge_70"], total), auto_ready=_pct(tot["ge_90"], total),
                   ai_green=tot["ai_green"], ai_amber=tot["ai_amber"], ai_red=tot["ai_red"],
                   effort_minutes=round(tot["to_review"] * self.th.minutes_per_review, 1))
        out["rag"] = rag(out["auto_ready"], g, a)
        vm = [r["veritas_match"] for r in per if r["veritas_match"] is not None]
        out["veritas_match"] = round(sum(vm) / len(vm), 4) if vm else None
        return {"summary": out, "mappings": per}

    def discovery_quality(self, s: Session) -> dict:
        g, a = self.th.green, self.th.amber
        b = defaultdict(int)
        for f in s.files:
            try:
                rep = self.discovery.load(s, f.file_id)["report"]
            except Exception:
                continue
            for sh in rep.get("sheets", []):
                for c in sh.get("columns", []):
                    b["columns"] += 1
                    if c.get("source") == "human":
                        b["person"] += 1
                    elif not c.get("semantic_type"):
                        b["unknown"] += 1
                    else:
                        b[rag(c.get("confidence") or 0, g, a)] += 1
                    b["needs_review"] += bool(c.get("needs_review"))
        out = dict(b)
        out["recognised"] = _pct(b["green"] + b["amber"] + b["red"] + b["person"], b["columns"])
        out["rag"] = rag(_pct(b["green"], b["green"] + b["amber"] + b["red"] + b["person"]), g, a)
        return out

    def manual_inputs(self, s: Session, events: list[dict] | None = None) -> dict:
        events = self.ctx.store.events(s.session_id) if events is None else events
        by_step: dict[str, int] = defaultdict(int)
        people: set[str] = set()
        for ev in events:
            step, actor = ev.get("step", ""), ev.get("decided_by")
            if step in NOT_MANUAL or not is_person(actor):
                continue
            group = next((g for prefix, g in MANUAL_STEPS if step == prefix or (prefix.endswith(".") and step.startswith(prefix))), None)
            if group:
                by_step[group] += 1
                people.add(actor)
        total = sum(by_step.values())
        return {"total": total, "by_step": dict(by_step), "people": sorted(people),
                "per_file": round(total / len(s.files), 1) if s.files else None}

    def timings(self, s: Session) -> dict:
        ga, gb, gc = (s.gates[k].decided_at for k in "ABC")
        pipe = (s.outputs.get("pipeline") or {}).get("confirmed_at")
        return {"to_gate_a_h": _hours(s.created_at, ga), "pipeline_h": _hours(ga, pipe),
                "to_gate_b_h": _hours(pipe, gb), "to_signoff_h": _hours(s.created_at, gc)}

    def session(self, s: Session) -> dict:
        events = self.ctx.store.events(s.session_id)
        mq = self.mapping_quality(s)
        rows = sum((s.outputs.get("pipeline") or {}).get("tables", {}).values()) if s.outputs.get("pipeline") else 0
        return {"session_id": s.session_id, "name": s.name, "retailer": s.retailer, "market": s.market,
                "stage": s.stage, "status": s.status, "created_at": s.created_at,
                "files": len(s.files), "known_layout_files": sum(1 for f in s.files if f.known_layout),
                "rows_loaded": rows, "mapping": mq["summary"], "mappings": mq["mappings"],
                "discovery": self.discovery_quality(s), "manual": self.manual_inputs(s, events),
                "timings": self.timings(s), "thresholds": self.th.model_dump()}

    # ── whole system ────────────────────────────────────────────────────────
    def system(self) -> dict:
        sessions = [self.session(s) for s in self.ctx.store.list()]
        sessions.sort(key=lambda x: x["created_at"])
        files = sum(x["files"] for x in sessions)
        manual = sum(x["manual"]["total"] for x in sessions)

        def avg(vals):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        totals = {"sessions": len(sessions), "signed_off": sum(x["status"] == "signed_off" for x in sessions),
                  "files": files, "known_layout_files": sum(x["known_layout_files"] for x in sessions),
                  "rows_loaded": sum(x["rows_loaded"] for x in sessions),
                  "manual_inputs": manual, "manual_per_file": round(manual / files, 1) if files else None,
                  "auto_ready": avg(x["mapping"]["auto_ready"] for x in sessions),
                  "veritas_match": avg(x["mapping"]["veritas_match"] for x in sessions),
                  "effort_minutes": round(sum(x["mapping"]["effort_minutes"] for x in sessions), 1),
                  "hours_to_signoff": avg(x["timings"]["to_signoff_h"] for x in sessions)}
        totals["rag"] = rag(totals["auto_ready"], self.th.green, self.th.amber)
        months: dict[str, list] = defaultdict(list)
        for x in sessions:
            months[x["created_at"][:7]].append(x)
        monthly = [{"month": m, "sessions": len(xs), "files": sum(x["files"] for x in xs),
                    "signed_off": sum(x["status"] == "signed_off" for x in xs),
                    "rows_loaded": sum(x["rows_loaded"] for x in xs),
                    "manual_inputs": sum(x["manual"]["total"] for x in xs),
                    "auto_ready": avg(x["mapping"]["auto_ready"] for x in xs),
                    "veritas_match": avg(x["mapping"]["veritas_match"] for x in xs)}
                   for m, xs in sorted(months.items())]
        trend = [{"at": x["created_at"], "session_id": x["session_id"], "name": x["name"], "retailer": x["retailer"],
                  "auto_ready": x["mapping"]["auto_ready"], "veritas_match": x["mapping"]["veritas_match"],
                  "manual_inputs": x["manual"]["total"], "rag": x["mapping"]["rag"], "stage": x["stage"],
                  "known_layout": x["files"] > 0 and x["known_layout_files"] == x["files"]} for x in sessions]
        return {"totals": totals, "monthly": monthly, "trend": trend, "thresholds": self.th.model_dump(),
                "sessions": [{k: x[k] for k in ("session_id", "name", "retailer", "market", "stage", "status",
                                                 "files", "rows_loaded", "created_at")}
                             | {"mapping": x["mapping"], "manual": x["manual"]["total"], "timings": x["timings"]}
                             for x in reversed(sessions)]}

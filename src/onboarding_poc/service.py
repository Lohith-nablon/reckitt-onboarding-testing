"""OnboardingService: the eight modules wired together for one process (the Databricks App).

Screen -> module(s)
  Start onboarding        M1 (session, business fields, mapping file)
  Add files               M1 (+ M8 known-layout check)
  AI Discovery            M2
  Mapping                 M3, Gate A
  Harmonisation           M4 + M5, Gate B
  Validate & preview      M6
  Sign off                M7, Gate C, then M8 learning
  File journey            M8 (every screen)
"""
from __future__ import annotations

from onboarding_poc.config import AppConfig, get_config
from onboarding_poc.context import Context
from onboarding_poc.modules.m1_intake import Intake
from onboarding_poc.modules.m2_discovery import Discovery
from onboarding_poc.modules.m3_mapping import Mapping
from onboarding_poc.modules.m4_code_harmonisation import CodeHarmonisation
from onboarding_poc.modules.m5_value_harmonisation import ValueHarmonisation
from onboarding_poc.modules.m6_enrich_validate import EnrichValidate
from onboarding_poc.modules.m7_output_signoff import OutputSignoff
from onboarding_poc.modules.m8_journey_learning import Crosswalk, Journey, MappingLibrary
from onboarding_poc.session import Session, now


class OnboardingService:
    def __init__(self, cfg: AppConfig | None = None, ctx: Context | None = None):
        self.ctx = ctx or Context(cfg or get_config())
        self.library = MappingLibrary(self.ctx)
        self.crosswalk = Crosswalk(self.ctx)
        self.journey = Journey(self.ctx)
        self.intake = Intake(self.ctx, self.library)
        self.discovery = Discovery(self.ctx)
        self.mapping = Mapping(self.ctx, self.discovery, self.intake, self.library)
        self.codes = CodeHarmonisation(self.ctx, self.discovery, self.mapping, self.crosswalk)
        self.values = ValueHarmonisation(self.ctx, self.mapping, self.discovery)
        self.validate = EnrichValidate(self.ctx, self.mapping, self.codes, self.values, self.library)
        self.output = OutputSignoff(self.ctx, self.discovery, self.mapping, self.validate, self.library)

    def session(self, sid: str) -> Session:
        return self.ctx.store.load(sid)

    # ── combined steps ────────────────────────────────────────────────────
    def analyse(self, s: Session, user: str) -> dict:
        """Discovery then mapping proposals in one go (M1-M3 run in one process, as agreed)."""
        if not s.files:
            raise ValueError("add at least one retailer file first")
        reports = self.discovery.run(s, user)
        maps = self.mapping.propose(s, user)
        return {"discovery": [{"file_id": r["file_id"], "open_questions": len(r["open_questions"]),
                               "reused_layout": r["reused_layout"]} for r in reports],
                "mappings": maps, "gate_a": s.gates["A"].model_dump()}

    def run_harmonisation(self, s: Session, user: str) -> dict:
        """M4 matching; for repeat files (every layout known) with nothing left to decide, Gate B
        passes on its own: the crosswalk and the approved rules already cover everything."""
        summary = self.codes.run(s, user)
        auto = bool(s.files) and all(f.known_layout for f in s.files) and \
            not self.codes.queue(s, only_open=True) and not self.values.open_issues(s)
        if auto:
            s.gates["B"] = s.gates["B"].model_copy(update={"status": "approved", "decided_by": "library",
                                                           "decided_at": now(), "note": "known layout, nothing to review"})
            s.stage = "validate"
            self.ctx.store.save(s)
            self.ctx.store.trace(s, "M4", "gate_b.auto_approved", "library",
                                 why="every code resolved from the crosswalk or above the auto-accept threshold")
        return {"summary": summary, "gate_b": s.gates["B"].model_dump()}

    def harmonisation_view(self, s: Session) -> dict:
        codes = self.codes.load(s)
        return {"codes_summary": self.codes.summary(codes), "codes": self.codes.queue(s),
                "value_rules": self.values.rules(s) if s.gates["A"].status == "approved" else [],
                "gate_b": s.gates["B"].model_dump()}

    def approve_gate_b(self, s: Session, user: str, note: str | None = None) -> dict:
        if s.gates["A"].status != "approved":
            raise ValueError("Gate A must be approved first")
        pending = [r["match_id"] for r in self.codes.queue(s, only_open=True) if r["status"] == "needs_review"]
        if pending:
            raise ValueError(f"{len(pending)} codes flagged for review are undecided (e.g. {pending[:3]})")
        open_rules = self.values.open_issues(s)
        if open_rules:
            raise ValueError(f"{len(open_rules)} value rules with issues are undecided: "
                             f"{[r['rule_id'].split(':', 1)[1] for r in open_rules[:3]]}")
        learned = self.codes.learn(s, user)
        unmatched = sum(1 for r in self.codes.load(s) if r["status"] == "unmatched" and not r["decision"])
        s.gates["B"] = s.gates["B"].model_copy(update={"status": "approved", "decided_by": user, "decided_at": now(),
                                                       "note": note})
        s.stage = "validate"
        self.ctx.store.save(s)
        self.ctx.store.trace(s, "M4", "gate_b.approved", user, why=note or "harmonisation reviewed",
                             after={"crosswalk_learned": learned, "unmatched_left_empty": unmatched})
        if learned:
            self.ctx.store.trace(s, "M8", "crosswalk.learned", user, after=learned,
                                 why="reviewer decisions reused for this retailer from now on")
        return {"crosswalk_learned": learned, "unmatched_left_empty": unmatched}

    def overview(self, s: Session) -> dict:
        return {"session": s.model_dump(), "journey": self.journey.build(s),
                "mappings": [self.mapping.summary(s, d) for d in self.mapping.all(s)]}

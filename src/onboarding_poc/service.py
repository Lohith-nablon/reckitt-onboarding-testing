"""OnboardingService: the eight modules wired together for one process (the Databricks App).

Phase 1 · Mapping
  Start onboarding        M1 (session, business fields, mapping file)
  Add files               M1 (+ M8 known-layout check); raw files from ADLS or upload
  AI Discovery            M2
  Mapping                 M3 + conversion rules (M5), Gate A -> mapping JSON + SQL to ADLS
Hand-off
  Pipeline                Reckitt runs its Databricks pipeline by hand; the app checks UC for the rows
Phase 2 · Harmonisation (on the loaded tables, through Unity Catalog)
  Harmonisation           M4 codes + M5 value standards, Gate B
  Validate & preview      M6
  Sign off                M7, Gate C -> silver + crosswalk, then M8 learning
File journey              M8 (every screen)
"""
from __future__ import annotations

from onboarding_poc.config import AppConfig, get_config
from onboarding_poc.context import Context
from onboarding_poc.modules.m1_intake import Intake
from onboarding_poc.modules.m2_discovery import Discovery
from onboarding_poc.modules.m3_mapping import Mapping, publish_mappings
from onboarding_poc.modules.m4_code_harmonisation import CodeHarmonisation
from onboarding_poc.modules.m5_value_harmonisation import ValueHarmonisation, ValueStandardisation
from onboarding_poc.modules.m6_enrich_validate import EnrichValidate
from onboarding_poc.modules.m7_output_signoff import OutputSignoff
from onboarding_poc.modules.m8_journey_learning import Crosswalk, Journey, MappingLibrary
from onboarding_poc.modules.pipeline_handoff import PipelineHandoff
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
        self.values = ValueHarmonisation(self.ctx, self.mapping, self.discovery)      # conversions (Phase 1)
        self.standards = ValueStandardisation(self.ctx, self.mapping)                  # value standards (Phase 2)
        self.pipeline = PipelineHandoff(self.ctx, self.mapping, self.values)
        self.codes = CodeHarmonisation(self.ctx, self.discovery, self.mapping, self.crosswalk)
        self.validate = EnrichValidate(self.ctx, self.mapping, self.codes, self.values, self.standards, self.library)
        self.output = OutputSignoff(self.ctx, self.discovery, self.mapping, self.validate, self.codes, self.library)

    def session(self, sid: str) -> Session:
        return self.ctx.store.load(sid)

    # ── Phase 1 ────────────────────────────────────────────────────────────
    def analyse(self, s: Session, user: str) -> dict:
        """Discovery then mapping proposals in one go (M1-M3 run in one process, as agreed).
        Known layouts come back approved from the library and are published straight away."""
        if not s.files:
            raise ValueError("add at least one retailer file first")
        reports = self.discovery.run(s, user)
        maps = self.mapping.propose(s, user)
        published = None
        if s.gates["A"].status == "approved":
            published = self.publish(s, "library")
        return {"discovery": [{"file_id": r["file_id"], "open_questions": len(r["open_questions"]),
                               "reused_layout": r["reused_layout"]} for r in reports],
                "mappings": maps, "gate_a": s.gates["A"].model_dump(), "published": published}

    def approve_gate_a(self, s: Session, user: str, note: str | None = None) -> dict:
        """Gate A: conversions decided, mandatory columns mapped -> approve and publish JSON + SQL."""
        open_rules = self.values.open_issues(s)
        if open_rules:
            raise ValueError(f"{len(open_rules)} conversion rules need a decision first (Conversions tab): "
                             f"{[r['target_column'] + ' ' + r['kind'] for r in open_rules[:3]]}")
        res = self.mapping.approve(s, user, note)
        res["published"] = self.publish(s, user)
        return res

    def publish(self, s: Session, user: str) -> dict:
        manifest = publish_mappings(self.ctx, s, self.mapping.all(s), user, self.values.decimals(s),
                                    self.library.reference_docs())
        s.stage = "pipeline"
        for f in s.files:
            f.status = "awaiting_pipeline"
        self.ctx.store.save(s)
        return manifest

    # ── Phase 2 ────────────────────────────────────────────────────────────
    def run_harmonisation(self, s: Session, user: str) -> dict:
        """M4 matching on the loaded tables; for repeat files with nothing left to decide, Gate B
        passes on its own: the crosswalk and the approved rules already cover everything."""
        summary = self.codes.run(s, user)
        frames = self.validate.frames(s)
        auto = bool(s.files) and all(f.known_layout for f in s.files) and \
            not self.codes.queue(s, only_open=True) and not self.standards.open_issues(s, frames)
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
        started = s.stage in ("harmonisation", "validate", "signoff", "done") and bool(codes or s.gates["B"].status != "not_started")
        frames = self.validate.frames(s) if s.stage in ("harmonisation", "validate", "signoff", "done") else {}
        return {"codes_summary": self.codes.summary(codes), "codes": self.codes.queue(s),
                "value_rules": self.standards.rules(s, frames) if frames else [],
                "gate_b": s.gates["B"].model_dump(), "started": started,
                "bronze": [{"fqn": d["published"]["target_fqn"], "rows": len(df), "target_table": t.name}
                           for d, t, df in self.validate.loaded(s)] if frames else []}

    def approve_gate_b(self, s: Session, user: str, note: str | None = None) -> dict:
        if s.gates["A"].status != "approved":
            raise ValueError("Gate A must be approved first")
        pending = [r["match_id"] for r in self.codes.queue(s, only_open=True) if r["status"] == "needs_review"]
        if pending:
            raise ValueError(f"{len(pending)} codes flagged for review are undecided (e.g. {pending[:3]})")
        open_rules = self.standards.open_issues(s, self.validate.frames(s))
        if open_rules:
            raise ValueError(f"{len(open_rules)} value standards are undecided: {[r['rule_id'] for r in open_rules[:3]]}")
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

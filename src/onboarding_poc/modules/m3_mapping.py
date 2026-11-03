"""M3 AI-assisted mapping: source columns -> target columns, reviewed by a person (Gate A).

Proposals come from, in order of trust: the onboarding profile (business fields), conventions
learned from existing Veritas mappings and the mapping library, discovery types, and the LLM
for anything left. Every link carries a confidence and a reason; links below the review
threshold are flagged. The canvas shows source on the left, target on the right, like Veritas.

A reviewer can accept a link, change it (any action: column, constant, concatenation, date,
expression) or clear it. Changed links are marked "human_modified" and keep the reviewer's
name; they survive a re-proposal. Gate A approves every mapping in the session at once and is
refused while a mandatory target column is unmapped.

Known layouts come straight from the library, already approved: no review needed.
"""
from __future__ import annotations

import uuid

import pandas as pd
from pydantic import BaseModel, Field

from onboarding_poc.core.mapping.apply import MissingSourceColumn, apply_mapping
from onboarding_poc.core.mapping.models import ColumnMapping, MappingSpec
from onboarding_poc.core.mapping.proposer import MappingProposer
from onboarding_poc.core.mapping.transforms import ExpressionError, columns_used, parse
from onboarding_poc.core.mapping.veritas import to_veritas
from onboarding_poc.core.text import canon_header
from onboarding_poc.session import Session, now, rows_jsonable

_EDITABLE = {"action", "source_columns", "value", "parts", "separator", "date_format", "target_date_format",
             "expression"}


class _LLMLink(BaseModel):
    target: str
    source_column: str | None = Field(None, description="exact source header, or null if nothing fits")
    confidence: float = Field(ge=0, le=1)
    reason: str


class _LLMLinks(BaseModel):
    links: list[_LLMLink]


def engine_of(m: ColumnMapping) -> str:
    r = (m.rationale or "").lower()
    if m.reviewed_by:
        return "human"
    if "llm" in r or r.startswith("ai"):
        return "llm"
    if "onboarding profile" in r:
        return "profile"
    if "same retailer" in r or "same market" in r or "veritas" in r or "other market" in r:
        return "veritas_conventions"
    return "rules"


def describe(c: ColumnMapping) -> str:
    if c.action == "DIRECT_TEXT":
        return f"'{c.value}'"
    if c.action == "CONCATENATION" and c.parts:
        return " + ".join(p.column if p.column is not None else f"'{p.text}'" for p in c.parts)
    if c.action == "DATE":
        return f"{(c.source_columns or ['?'])[0]} ({c.date_format} -> {c.target_date_format or 'date'})"
    if c.action == "EXPRESSION":
        return c.expression or ""
    if c.action == "LOOKUP" and c.lookup:
        return f"{c.lookup.name}({', '.join(c.lookup.key_columns)}) -> {c.lookup.return_column}"
    return ", ".join(c.source_columns)


class Mapping:
    module = "M3"

    def __init__(self, ctx, discovery, intake, library):
        self.ctx, self.discovery, self.intake, self.library = ctx, discovery, intake, library

    # ── storage ────────────────────────────────────────────────────────────
    def load(self, s: Session, mapping_id: str) -> dict:
        d = self.ctx.store.get(s.session_id, f"mappings/{mapping_id}.json")
        if d is None:
            raise KeyError(f"mapping {mapping_id} not found")
        return d

    def spec(self, d: dict) -> MappingSpec:
        return MappingSpec.model_validate(d["spec"])

    def _save(self, s: Session, d: dict) -> None:
        self.ctx.store.put(s.session_id, f"mappings/{d['spec']['mapping_id']}.json", d)

    def all(self, s: Session) -> list[dict]:
        return [self.load(s, m) for m in s.mapping_ids]

    # ── propose ────────────────────────────────────────────────────────────
    def propose(self, s: Session, user: str) -> list[dict]:
        """(Re)build mappings for every file and sheet. Reviewer changes on an existing mapping
        for the same sheet and target are kept."""
        contracts = self.ctx.contracts(s)
        mcfg = self.ctx.markets.get(s.market)
        conv = self.intake.conventions(s)
        proposer = MappingProposer(self.ctx.catalogue, self.ctx.llm)
        existing = {(d["file_id"], d["sheet"], d["spec"]["target_table"]): d for d in self.all(s)}
        th = self.ctx.cfg.thresholds
        out, ids = [], []
        for f in s.files:
            sds = self.discovery.sheet_discoveries(s, f.file_id)
            parsed = {ps.sheet: ps for ps in self.ctx.parsed(s, f.file_id)}
            for sd in sds:
                ps = parsed[sd.sheet]
                lib_hits = {e["spec"]["target_table"]: e for e in
                            self.library.for_layout(s.market, s.retailer, sd.template_fingerprint)}
                targets = [p.target_table for p in sd.proposals if p.target_table in contracts.tables]
                targets += [t for t in lib_hits if t not in targets and t in contracts.tables]
                for tname in targets:
                    table = contracts.tables[tname]
                    prev = existing.get((f.file_id, sd.sheet, tname))
                    if tname in lib_hits:
                        e = lib_hits[tname]
                        spec = MappingSpec.model_validate(e["spec"])
                        spec = spec.model_copy(update={"mapping_id": prev["spec"]["mapping_id"] if prev else str(uuid.uuid4()),
                                                       "origin": f"library:v{e['version']}", "status": "approved"})
                        spec.source.source_columns = list(ps.frame.columns)
                        links = {c.target_column: {"state": "library", "by": e.get("approved_by"), "at": e.get("approved_at")}
                                 for c in spec.columns}
                        decided = "library"
                        why = f"layout known: library v{e['version']} approved by {e.get('approved_by')}"
                        self.library.mark_reused(e)
                    else:
                        spec = proposer.propose(sd, ps.frame, table, mcfg, conv, footer_rows=ps.header.footer_rows_dropped,
                                                file_format=ps.file_format, delimiter=ps.delimiter, profile=s.profile)
                        self._llm_review(s, spec, table, sd)
                        links = {c.target_column: {"state": "ai_suggested" if c.action else "unmapped", "by": None,
                                                   "at": None} for c in spec.columns}
                        if prev:
                            spec.mapping_id = prev["spec"]["mapping_id"]
                            spec.version = prev["spec"]["version"] + 1
                            old = MappingSpec.model_validate(prev["spec"])
                            for oc in old.columns:
                                if oc.reviewed_by and any(c.target_column == oc.target_column for c in spec.columns):
                                    spec.columns = [oc if c.target_column == oc.target_column else c for c in spec.columns]
                                    links[oc.target_column] = prev["links"].get(oc.target_column, links[oc.target_column])
                        decided = self.ctx.decided_by_ai
                        why = "proposed from discovery, onboarding profile and Veritas conventions"
                    d = {"file_id": f.file_id, "file_name": f.name, "sheet": sd.sheet, "spec": spec.model_dump(),
                         "links": links, "created_at": prev["created_at"] if prev else now(), "updated_at": now()}
                    d["veritas"], d["veritas_warnings"] = self._veritas(s, spec, table, user)
                    self._save(s, d)
                    ids.append(spec.mapping_id)
                    issues = spec.issues(th.mapping_review)
                    self.ctx.store.trace(s, self.module, "mapping.proposed", decided, subject=f"{f.file_id}:{tname}",
                                         source=f"{f.name} [{sd.sheet or 'data'}]", why=why,
                                         confidence=round(spec.min_confidence, 3),
                                         after={"mapped": sum(1 for c in spec.columns if c.action),
                                                "blocking": issues["blocking"], "review": issues["review"]})
                    out.append(self.summary(s, d))
                f.status = "mapped"
        s.mapping_ids = ids
        known = all(f.known_layout for f in s.files) and s.files
        if known:
            s.gates["A"].status, s.gates["A"].decided_by, s.gates["A"].decided_at = "approved", "library", now()
            s.gates["A"].note = "every file has a known layout"
        elif s.gates["A"].status in ("not_started", "approved"):
            s.gates["A"].status = "open"
        s.stage = "mapping"
        self.ctx.store.save(s)
        return out

    def _llm_review(self, s: Session, spec: MappingSpec, table, sd) -> None:
        """AI pass over what the rules left open: unmapped source-type targets and weak links.
        AI links stay below the review threshold unless the rules agree, so a person sees them."""
        if not self.ctx.llm.available:
            return
        th = self.ctx.cfg.thresholds.mapping_review
        open_cols = [c for c in spec.columns if table.has(c.target_column)
                     and table.column(c.target_column).field_source in ("source", "optional")
                     and (c.action is None or c.confidence < th) and not c.reviewed_by]
        if not open_cols:
            return
        samples = {p.column: [str(v) for v, _ in p.top_values[:4]] for p in sd.profile.column_profiles}
        targets = {c.target_column: {"dtype": table.column(c.target_column).dtype,
                                     "description": table.column(c.target_column).description,
                                     "current": describe(c) if c.action else None} for c in open_cols}
        prompt = (f"Map retailer file columns to the target table {table.name} ({table.description}).\n"
                  f"Target columns to fill: {targets}\nSource columns with sample values: {samples}\n"
                  "For each target return the single best source column (exact header) or null, a confidence "
                  "0-1 and a one-line reason a business user understands.")
        res = self.ctx.llm.structured(prompt, _LLMLinks, kind="mapping_review")
        self.ctx.store.trace(s, self.module, "mapping.llm_call", self.ctx.decided_by_ai, subject=table.name,
                             source=f"{len(open_cols)} open targets, {len(samples)} source columns",
                             why="ok" if res else "LLM unavailable or invalid answer; rules kept")
        if not res:
            return
        headers = {canon_header(h): h for h in spec.source.source_columns}
        for ln in res.links:
            c = next((x for x in open_cols if x.target_column == ln.target), None)
            src = headers.get(canon_header(ln.source_column)) if ln.source_column else None
            if c is None or src is None:
                continue
            if c.action == "DIRECT_COLUMN" and c.source_columns and canon_header(c.source_columns[0]) == canon_header(src):
                c.confidence = round(min(0.95, max(c.confidence, ln.confidence) + 0.1), 3)
                c.rationale = f"{c.rationale}; AI agrees: {ln.reason}"
            elif c.action is None or ln.confidence > c.confidence + 0.1:
                if table.column(c.target_column).dtype == "date":
                    continue        # dates need a format: left to rules / reviewer
                c.action, c.source_columns = "DIRECT_COLUMN", [src]
                c.confidence = round(min(ln.confidence, th - 0.05), 3)
                c.rationale = f"AI: {ln.reason}"

    def _veritas(self, s: Session, spec: MappingSpec, table, user: str) -> tuple[dict | None, list[str]]:
        if not table.veritas_dataset:
            return None, []
        mcfg = self.ctx.markets.get(s.market)
        legc = (s.profile.get("legc") or {}).get("value") or mcfg.legc
        try:
            return to_veritas(spec, table, legc=legc, retailer_display=mcfg.display_name(s.retailer), developed_by=user)
        except Exception as exc:     # an expression Veritas cannot express, etc.
            return None, [str(exc)]

    # ── views ──────────────────────────────────────────────────────────────
    def summary(self, s: Session, d: dict) -> dict:
        spec = self.spec(d)
        issues = spec.issues(self.ctx.cfg.thresholds.mapping_review)
        states: dict[str, int] = {}
        for v in d["links"].values():
            states[v["state"]] = states.get(v["state"], 0) + 1
        return {"mapping_id": spec.mapping_id, "file_id": d["file_id"], "file_name": d["file_name"], "sheet": d["sheet"],
                "target_table": spec.target_table, "version": spec.version, "status": spec.status, "origin": spec.origin,
                "issues": issues, "link_states": states, "min_confidence": round(spec.min_confidence, 3),
                "mapped": sum(1 for c in spec.columns if c.action), "total": len(spec.columns),
                "veritas_warnings": d.get("veritas_warnings", [])}

    def canvas(self, s: Session, mapping_id: str) -> dict:
        """Veritas-style canvas: sources left, targets right, one link per target column."""
        d = self.load(s, mapping_id)
        spec = self.spec(d)
        table = self.ctx.contracts(s).tables[spec.target_table]
        th = self.ctx.cfg.thresholds
        rep = next((x for x in self.discovery.load(s, d["file_id"])["report"]["sheets"] if x["sheet"] == d["sheet"]), None)
        disc = {c["column"]: c for c in (rep or {}).get("columns", [])}
        used: dict[str, list[str]] = {}
        links = []
        for c in spec.columns:
            srcs = list(c.source_columns) + [p.column for p in c.parts if p.column]
            if c.action == "EXPRESSION" and c.expression:
                srcs += columns_used(c.expression)
            if c.lookup and c.lookup.name.startswith("harmonisation."):
                srcs += c.lookup.key_columns
            srcs = list(dict.fromkeys(srcs))
            for x in srcs:
                used.setdefault(canon_header(x), []).append(c.target_column)
            ln = d["links"].get(c.target_column, {})
            links.append({"target": c.target_column, "sources": srcs, "action": c.action, "describe": describe(c),
                          "value": c.value, "parts": [p.model_dump() for p in c.parts], "separator": c.separator,
                          "date_format": c.date_format, "target_date_format": c.target_date_format,
                          "expression": c.expression, "lookup": c.lookup.model_dump() if c.lookup else None,
                          "confidence": round(c.confidence, 3), "rationale": c.rationale, "engine": engine_of(c),
                          "state": ln.get("state"), "decided_by": ln.get("by"), "decided_at": ln.get("at"),
                          "needs_review": bool(c.action and c.confidence < th.mapping_review and ln.get("state") not in
                                               ("accepted", "human_modified", "library")),
                          "blocking": c.action is None and (c.mandatory or c.is_pk)})
        sources = [{"column": col, "semantic_type": disc.get(col, {}).get("semantic_type"),
                    "meaning": disc.get(col, {}).get("meaning"), "examples": disc.get(col, {}).get("examples", []),
                    "used_by": used.get(canon_header(col), [])} for col in spec.source.source_columns]
        targets = [{"column": tc.name, "dtype": tc.dtype, "mandatory": tc.mandatory, "pk": tc.pk,
                    "field_source": tc.field_source, "description": tc.description} for tc in table.columns]
        return {**self.summary(s, d), "sources": sources, "targets": targets, "links": links,
                "veritas": d.get("veritas")}

    def preview(self, s: Session, mapping_id: str, rows: int = 20) -> dict:
        d = self.load(s, mapping_id)
        spec = self.spec(d)
        table = self.ctx.contracts(s).tables[spec.target_table]
        frame = self.ctx.frame(s, d["file_id"], d["sheet"]).head(rows)
        try:
            out = apply_mapping(frame, spec, table)
        except (MissingSourceColumn, ExpressionError, ValueError) as exc:
            return {"columns": [], "rows": [], "error": str(exc)}
        return {"columns": list(out.columns), "rows": rows_jsonable(out),
                "coercion_errors": out.attrs.get("coercion_errors", {})}

    # ── review ─────────────────────────────────────────────────────────────
    def update_link(self, s: Session, mapping_id: str, target_column: str, change: dict, user: str,
                    note: str | None = None) -> dict:
        d = self.load(s, mapping_id)
        spec = self.spec(d)
        cm = spec.column(target_column)
        frame_cols = {canon_header(c) for c in spec.source.source_columns}
        bad = set(change) - _EDITABLE
        if bad:
            raise ValueError(f"cannot change {sorted(bad)} on a link")
        new = cm.model_copy(update=change)
        if new.action is None:
            new = ColumnMapping(target_column=cm.target_column, mandatory=cm.mandatory, is_pk=cm.is_pk)
        refs = list(new.source_columns) + [p.column for p in new.parts if p.column]
        if new.action == "EXPRESSION":
            if not new.expression:
                raise ValueError("expression is empty")
            try:
                parse(new.expression)
            except ExpressionError as exc:
                raise ValueError(f"expression: {exc}") from exc
            refs += columns_used(new.expression)
        missing = [r for r in refs if canon_header(r) not in frame_cols]
        if missing:
            raise ValueError(f"source column(s) not in file: {missing}")
        if new.action == "DIRECT_TEXT" and new.value in (None, ""):
            raise ValueError("a constant needs a value")
        if new.action in ("DIRECT_COLUMN", "DATE") and not new.source_columns:
            raise ValueError(f"{new.action} needs a source column")
        before = describe(cm)
        new.reviewed_by, new.confidence = user, 1.0
        new.rationale = f"set by {user}" + (f": {note}" if note else "")
        spec.columns = [new if c.target_column == target_column else c for c in spec.columns]
        spec.status = "proposed"
        d["spec"] = spec.model_dump()
        d["links"][target_column] = {"state": "human_modified" if new.action else "unmapped", "by": user, "at": now()}
        d["veritas"], d["veritas_warnings"] = self._veritas(s, spec, self.ctx.contracts(s).tables[spec.target_table], user)
        d["updated_at"] = now()
        self._save(s, d)
        self._reopen(s, user, f"{target_column} changed")
        self.ctx.store.trace(s, self.module, "mapping.link_changed", user, subject=f"{d['file_id']}:{spec.target_table}.{target_column}",
                             before=before, after=describe(new), why=note or "changed by reviewer", confidence=1.0)
        return next(x for x in self.canvas(s, mapping_id)["links"] if x["target"] == target_column)

    def accept_link(self, s: Session, mapping_id: str, target_columns: list[str], user: str) -> int:
        d = self.load(s, mapping_id)
        spec = self.spec(d)
        n = 0
        for t in target_columns:
            c = spec.column(t)
            if c.action and d["links"][t]["state"] == "ai_suggested":
                d["links"][t] = {"state": "accepted", "by": user, "at": now()}
                self.ctx.store.trace(s, self.module, "mapping.link_accepted", user,
                                     subject=f"{d['file_id']}:{spec.target_table}.{t}", after=describe(c),
                                     why=c.rationale, confidence=round(c.confidence, 3))
                n += 1
        self._save(s, d)
        return n

    def _reopen(self, s: Session, user: str, why: str) -> None:
        if s.gates["A"].status == "approved":
            s.gates["A"] = s.gates["A"].model_copy(update={"status": "reopened", "decided_by": None, "decided_at": None})
            for g in ("B", "C"):
                if s.gates[g].status == "approved":
                    s.gates[g].status = "reopened"
            self.ctx.store.trace(s, self.module, "gate_a.reopened", user, why=why)
        self.ctx.store.save(s)

    # ── Gate A ─────────────────────────────────────────────────────────────
    def approve(self, s: Session, user: str, note: str | None = None) -> dict:
        docs = self.all(s)
        if not docs:
            raise ValueError("nothing to approve: propose mappings first")
        th = self.ctx.cfg.thresholds
        blocking = {f"{d['file_name']} -> {d['spec']['target_table']}": self.spec(d).issues(th.mapping_review)["blocking"]
                    for d in docs}
        blocking = {k: v for k, v in blocking.items() if v}
        if blocking:
            raise ValueError(f"mandatory target columns are unmapped: {blocking}")
        contracts = self.ctx.contracts(s)
        for d in docs:
            spec = self.spec(d)
            if spec.status != "approved":
                spec.status, spec.approved_by, spec.approved_at = "approved", user, now()
                d["spec"] = spec.model_dump()
                d["veritas"], d["veritas_warnings"] = self._veritas(s, spec, contracts.tables[spec.target_table], user)
                self._save(s, d)
        s.gates["A"] = s.gates["A"].model_copy(update={"status": "approved", "decided_by": user, "decided_at": now(),
                                                       "note": note})
        self.ctx.store.save(s)
        unreviewed = sum(1 for d in docs for v in d["links"].values() if v["state"] == "ai_suggested")
        self.ctx.store.trace(s, self.module, "gate_a.approved", user, why=note or "mappings approved",
                             after={"mappings": len(docs), "links_not_individually_reviewed": unreviewed})
        return {"approved": len(docs), "links_not_individually_reviewed": unreviewed}

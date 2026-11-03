"""M2 AI Discovery: what is in each file, in words a business user can check.

Two passes per sheet:
  1. Rules (always): header detection, column profiles, semantic types from names and value
     patterns, date formats, currency, grain, fact/dimension, which target table it feeds.
  2. AI (when an LLM is configured): a sample of 10-20 rows plus the rule profile goes to the
     model, which returns table-, column- and data-level metadata: what the table is, each
     column's meaning, semantic type, archetype, date format, currency/unit and open questions.

The two are merged column by column. Agreement raises confidence; disagreement or low
confidence flags the column for review. A person can confirm or change any column type
(recorded as "human"), and that choice drives mapping. Known layouts skip the AI pass and the
review: the approved mapping already says what every column is.
"""
from __future__ import annotations

import re

import pandas as pd
from pydantic import BaseModel, Field

from onboarding_poc.core.discovery.hints import HintsInferencer
from onboarding_poc.core.discovery.profiler import detect_date_format
from onboarding_poc.core.discovery.service import DiscoveryService, brief_to_markdown
from onboarding_poc.core.schemas import ColumnHint, SheetDiscovery, SourceFile
from onboarding_poc.core.text import currency_in_header
from onboarding_poc.session import Session, now

_ARCHETYPE = {"code": "identifier", "measure": "measure", "date": "date", "text": "attribute", "flag": "flag",
              "geo": "location"}


class _LLMColumn(BaseModel):
    column: str
    meaning: str = Field(description="one plain-English sentence a business user understands")
    semantic_type: str | None = Field(None, description="one of the allowed semantic types, or null")
    archetype: str = Field(description="identifier | measure | date | attribute | flag | location")
    confidence: float = Field(ge=0, le=1)
    date_format: str | None = Field(None, description="Java style, e.g. dd-MM-yyyy, only for dates")
    currency: str | None = None
    unit: str | None = Field(None, description="unit of measure for quantities, e.g. EA, CS, KG")
    question: str | None = Field(None, description="what a person must confirm, if anything")


class _LLMTable(BaseModel):
    description: str = Field(description="what one row of this table is, in plain English")
    entity: str | None = Field(None, description="sales | stock | product | store | promotion | other")
    grain: list[str] = Field(default_factory=list, description="columns that make a row unique")
    period: str | None = Field(None, description="daily | weekly | monthly | snapshot | unknown")
    data_quality_notes: list[str] = Field(default_factory=list)


class _LLMDiscovery(BaseModel):
    table: _LLMTable
    columns: list[_LLMColumn]
    questions: list[str] = Field(default_factory=list)


def _java(fmt: str | None) -> str | None:
    if not fmt:
        return None
    return (fmt.replace("%Y", "yyyy").replace("%y", "yy").replace("%m", "MM").replace("%d", "dd")
            .replace("%H", "HH").replace("%M", "mm").replace("%S", "ss").replace("%b", "MMM"))


class Discovery:
    module = "M2"

    def __init__(self, ctx):
        self.ctx = ctx

    def _service(self, s: Session, use_llm: bool) -> DiscoveryService:
        th = self.ctx.cfg.thresholds
        hints = HintsInferencer(self.ctx.catalogue, self.ctx.llm if use_llm else None)
        return DiscoveryService(self.ctx.contracts(s), self.ctx.catalogue, self.ctx.markets, hints, th.mapping_review)

    # ── run ────────────────────────────────────────────────────────────────
    def run(self, s: Session, user: str, file_ids: list[str] | None = None) -> list[dict]:
        reports = []
        for f in s.files:
            if file_ids and f.file_id not in file_ids:
                continue
            reports.append(self.run_file(s, f.file_id, user))
        if s.stage in ("start", "files"):
            s.stage = "discovery"
        self.ctx.store.save(s)
        return reports

    def run_file(self, s: Session, file_id: str, user: str) -> dict:
        f = s.file(file_id)
        use_llm = self.ctx.llm.available and not f.known_layout
        svc = self._service(s, use_llm)
        src = SourceFile(uri=f.key, name=f.name, market=s.market, retailer=s.retailer, size_bytes=f.size_bytes)
        parsed = self.ctx.parsed(s, file_id)
        sds = [svc.discover_sheet(src, ps) for ps in parsed]
        frames = {ps.sheet: ps.frame for ps in parsed}
        brief = svc.brief(src, sds, frames)
        decided_by = self.ctx.decided_by_ai if use_llm else ("library" if f.known_layout else "rules")
        sheets = []
        for sd, ps, bs in zip(sds, parsed, brief.sheets):
            ai = self._ai_pass(s, sd, ps.frame) if use_llm else None
            sheets.append(self._sheet_report(s, sd, ps.frame, bs, ai))
        questions = [{"id": f"q{i + 1}", **q.model_dump(), "answer": None, "answered_by": None}
                     for i, q in enumerate(brief.open_questions)]
        for sh in sheets:
            for q in sh.pop("_ai_questions", []):
                questions.append({"id": f"q{len(questions) + 1}", "topic": "ai", "column": None, "question": q,
                                  "proposed_answer": None, "confidence": None, "answer": None, "answered_by": None})
        report = {"file_id": file_id, "file_name": f.name, "generated_at": now(), "decided_by": decided_by,
                  "reused_layout": f.known_layout, "sample_rows_sent": self.ctx.cfg.discovery.sample_rows if use_llm else 0,
                  "sheets": sheets, "open_questions": [] if f.known_layout else questions,
                  "markdown": brief_to_markdown(brief)}
        self.ctx.store.put(s.session_id, f"discovery/{file_id}.json",
                           {"report": report, "sheet_discovery": [sd.model_dump(mode="json") for sd in sds]})
        f.status = "discovered"
        self.ctx.store.trace(
            s, self.module, "discovery.completed" if not f.known_layout else "discovery.skipped_known_layout",
            decided_by, subject=file_id, source=f"{f.name}: {sum(x['table']['rows'] for x in sheets)} rows, "
            f"{report['sample_rows_sent']} sample rows sent to the LLM",
            why=f"{sum(len(x['columns']) for x in sheets)} columns typed; {len(report['open_questions'])} open questions",
            after={"sheets": [{"sheet": x["sheet"], "classification": x["table"]["classification"],
                               "targets": [t["target_table"] for t in x["table"]["targets"]]} for x in sheets]})
        return report

    def _ai_pass(self, s: Session, sd: SheetDiscovery, frame: pd.DataFrame) -> _LLMDiscovery | None:
        n = self.ctx.cfg.discovery.sample_rows
        sample = frame.head(n).fillna("").astype(str)
        cols = list(frame.columns)[: self.ctx.cfg.discovery.max_columns_per_call]
        rules = {c: (h.semantic_type, round(h.confidence, 2)) for c, h in sd.column_hints.items()}
        types = ", ".join(sorted(self.ctx.catalogue.types))
        prompt = (
            f"A retailer ({s.retailer}, market {s.market}) sent a data file '{sd.source.name}'"
            f"{' sheet ' + sd.sheet if sd.sheet else ''} with {sd.row_count} rows. Describe it for a business user.\n"
            f"Allowed semantic types: {types}.\n"
            f"Rule-based guesses (column: type, confidence): {rules}\n"
            f"Title rows above the header: {sd.header.title_rows}\n"
            f"First {len(sample)} rows (CSV):\n{sample[cols].to_csv(index=False)}\n"
            "For every column give meaning, semantic_type (from the allowed list or null), archetype, confidence, "
            "date_format (Java style) for dates, currency for money, unit for quantities, and a question when a "
            "person must confirm something. Do not invent columns.")
        out = self.ctx.llm.structured(prompt, _LLMDiscovery, kind="discovery")
        self.ctx.store.trace(s, self.module, "discovery.llm_call", self.ctx.decided_by_ai,
                             subject=f"{sd.source.name}:{sd.sheet or ''}",
                             source=f"{len(sample)} sample rows, {len(cols)} columns",
                             why="ok" if out else "LLM unavailable or invalid answer; rules used")
        return out

    def _sheet_report(self, s: Session, sd: SheetDiscovery, frame: pd.DataFrame, bs: dict,
                      ai: _LLMDiscovery | None) -> dict:
        th = self.ctx.cfg.thresholds
        mcfg = self.ctx.markets.get(s.market)
        ai_cols = {c.column: c for c in (ai.columns if ai else [])}
        prof = {p.column: p for p in sd.profile.column_profiles}
        columns = []
        for col, h in sd.column_hints.items():
            a = ai_cols.get(col)
            sem, conf, src, why = h.semantic_type, h.confidence, h.source, h.rationale or ""
            if a and a.semantic_type in self.ctx.catalogue.types:
                if a.semantic_type == sem:
                    conf, why = min(1.0, max(conf, a.confidence) + 0.05), (why + "; AI agrees").strip("; ")
                elif sem is None or a.confidence > conf + 0.05:
                    sem, conf, src, why = a.semantic_type, a.confidence, "llm", f"AI: {a.meaning}"
                    sd.column_hints[col] = ColumnHint(semantic_type=sem, confidence=conf, source="llm", rationale=why)
                else:
                    why = (why + f"; AI suggests {a.semantic_type} ({a.confidence:.2f})").strip("; ")
                    conf = min(conf, th.discovery_review - 0.01)
                    sd.column_hints[col] = ColumnHint(semantic_type=sem, confidence=conf, source=h.source, rationale=why)
            p = prof.get(col)
            kind = self.ctx.catalogue.types[sem].kind if sem in self.ctx.catalogue.types else None
            archetype = (a.archetype if a and a.archetype else None) or _ARCHETYPE.get(kind or "", None) or \
                ("measure" if p and p.data_type in ("integer", "decimal") else "date" if p and p.data_type == "date"
                 else "attribute")
            date_fmt = None
            if archetype == "date" or kind == "date":
                fmt, _, _ = detect_date_format(frame[col].dropna().tolist(), mcfg.date_dayfirst)
                date_fmt = _java(fmt) or (a.date_format if a else None)
            currency = currency_in_header(col) or (a.currency if a else None) or \
                (mcfg.currency if sem in ("net_value", "unit_price") else None)
            columns.append({
                "column": col, "semantic_type": sem, "confidence": round(conf, 3), "source": src, "rationale": why,
                "meaning": a.meaning if a else _rule_meaning(sem, self.ctx.catalogue),
                "archetype": archetype, "data_type": p.data_type if p else None,
                "null_rate": round(p.null_rate, 3) if p else None, "distinct": p.distinct_count if p else None,
                "examples": [str(v) for v, _ in (p.top_values[:4] if p else [])],
                "pattern": p.pattern_detected if p else None,
                "date_format": date_fmt, "currency": currency, "unit": a.unit if a else None,
                "needs_review": sem is None or conf < th.discovery_review,
            })
        table = {"rows": sd.row_count, "columns": len(columns), "header_row": bs["header_row"],
                 "header_confidence": bs["header_confidence"], "title_rows": bs["title_rows"],
                 "footer_rows_dropped": bs["footer_rows_dropped"], "classification": sd.classification,
                 "classification_confidence": sd.classification_confidence, "entity": sd.entity,
                 "grain": sd.grain_candidate, "date_range": sd.date_range, "date": bs["date"],
                 "currency": bs["currency"], "currency_source": bs["currency_source"],
                 "ean_valid_share": bs["ean_valid_share"], "targets": bs["targets"],
                 "description": ai.table.description if ai else _rule_table_description(sd),
                 "period": ai.table.period if ai else None,
                 "data_quality_notes": ai.table.data_quality_notes if ai else []}
        return {"sheet": sd.sheet, "fingerprint": sd.template_fingerprint, "table": table, "columns": columns,
                "_ai_questions": [q for q in (ai.questions if ai else [])] +
                                 [f"[{c.column}] {c.question}" for c in (ai.columns if ai else []) if c.question]}

    # ── human review ───────────────────────────────────────────────────────
    def load(self, s: Session, file_id: str) -> dict:
        d = self.ctx.store.get(s.session_id, f"discovery/{file_id}.json")
        if d is None:
            raise KeyError(f"no discovery for file {file_id}; run discovery first")
        return d

    def sheet_discoveries(self, s: Session, file_id: str) -> list[SheetDiscovery]:
        return [SheetDiscovery.model_validate(x) for x in self.load(s, file_id)["sheet_discovery"]]

    def set_column_type(self, s: Session, file_id: str, sheet: str | None, column: str, semantic_type: str | None,
                        user: str, note: str | None = None) -> dict:
        if semantic_type is not None and semantic_type not in self.ctx.catalogue.types:
            raise ValueError(f"unknown semantic type {semantic_type}")
        d = self.load(s, file_id)
        sd_raw = next(x for x in d["sheet_discovery"] if x["sheet"] == sheet)
        rep = next(x for x in d["report"]["sheets"] if x["sheet"] == sheet)
        col = next((c for c in rep["columns"] if c["column"] == column), None)
        if col is None:
            raise KeyError(f"column {column} not in sheet {sheet}")
        before = {"semantic_type": col["semantic_type"], "confidence": col["confidence"], "source": col["source"]}
        col.update(semantic_type=semantic_type, confidence=1.0, source="human", needs_review=False,
                   rationale=f"set by {user}" + (f": {note}" if note else ""))
        sd_raw["column_hints"][column] = ColumnHint(semantic_type=semantic_type, confidence=1.0, source="human",
                                                    rationale=col["rationale"]).model_dump()
        for q in d["report"]["open_questions"]:
            if q.get("column") == column and q["answer"] is None:
                q.update(answer=semantic_type or "not used", answered_by=user)
        self.ctx.store.put(s.session_id, f"discovery/{file_id}.json", d)
        self.ctx.store.trace(s, self.module, "discovery.column_type_set", user, subject=f"{file_id}:{column}",
                             before=before, after={"semantic_type": semantic_type}, why=note or "confirmed by reviewer",
                             confidence=1.0)
        self._reopen_mapping(s, user, f"column type of {column} changed")
        return col

    def answer(self, s: Session, file_id: str, question_id: str, answer: str, user: str) -> dict:
        d = self.load(s, file_id)
        q = next(x for x in d["report"]["open_questions"] if x["id"] == question_id)
        before = q.get("answer")
        q.update(answer=answer, answered_by=user, answered_at=now())
        self.ctx.store.put(s.session_id, f"discovery/{file_id}.json", d)
        self.ctx.store.trace(s, self.module, "discovery.question_answered", user, subject=f"{file_id}:{question_id}",
                             before=before, after=answer, why=q["question"])
        return q

    def _reopen_mapping(self, s: Session, user: str, why: str) -> None:
        if s.gates["A"].status == "approved":
            s.gates["A"].status = "reopened"
            self.ctx.store.save(s)
            self.ctx.store.trace(s, "M3", "gate_a.reopened", user, why=why)


def _rule_meaning(sem: str | None, catalogue) -> str:
    if not sem:
        return "Not recognised yet: confirm what this column holds."
    return re.sub(r"_", " ", sem).capitalize() + (f" ({catalogue.types[sem].kind})" if sem in catalogue.types else "")


def _rule_table_description(sd: SheetDiscovery) -> str:
    what = {"fact": "transactions / measures", "dimension": "reference records", "unknown": "unclassified rows"}
    ent = f" about {sd.entity}s" if sd.entity and sd.entity != "mixed" else ""
    grain = f", one row per {' + '.join(sd.grain_candidate)}" if sd.grain_candidate else ""
    return f"{sd.row_count} rows of {what.get(sd.classification, 'data')}{ent}{grain}."

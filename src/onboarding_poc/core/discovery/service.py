"""Discovery: profile each sheet, infer semantics, classify, propose targets, draft the brief."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from onboarding_poc.core.contracts import Catalogue, Contracts, Markets
from onboarding_poc.core.discovery.classifier import classify, find_grain, propose_targets, types_present
from onboarding_poc.core.discovery.hints import HintsInferencer
from onboarding_poc.core.discovery.profiler import detect_date_format, profile_frame
from onboarding_poc.core.ingest.reader import ParsedSheet
from onboarding_poc.core.schemas import DiscoveryBrief, OpenQuestion, SheetDiscovery, SourceFile
from onboarding_poc.core.text import currency_in_header, normalise_gtin

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class DiscoveryService:
    def __init__(self, contracts: Contracts, catalogue: Catalogue, markets: Markets, hints: HintsInferencer,
                 mapping_review_threshold: float = 0.8):
        self.contracts = contracts
        self.catalogue = catalogue
        self.markets = markets
        self.hints = hints
        self.review_threshold = mapping_review_threshold

    def discover_sheet(self, src: SourceFile, ps: ParsedSheet) -> SheetDiscovery:
        mcfg = self.markets.get(src.market)
        key = ".".join(x for x in [src.market or "NA", src.retailer or "NA", Path(src.name).stem, ps.sheet] if x)
        profile, columns = profile_frame(ps.frame, key, dayfirst=mcfg.date_dayfirst)
        hints = self.hints.infer(profile.column_profiles,
                                 table_context=f"market={src.market}, retailer={src.retailer}, file={src.name}, "
                                               f"sheet={ps.sheet}, title={ps.header.title_rows}")
        classification, conf, entity = classify(hints, self.catalogue)
        grain = find_grain(ps.frame, hints)
        date_range = None
        date_cols = types_present(hints).get("week_ending", [])
        if date_cols:
            fmt, _, _ = detect_date_format(ps.frame[date_cols[0]].dropna().tolist(), mcfg.date_dayfirst)
            if fmt:
                d = pd.to_datetime(ps.frame[date_cols[0]], format=fmt, errors="coerce").dropna()
                if len(d):
                    date_range = (d.min().date().isoformat(), d.max().date().isoformat())
        return SheetDiscovery(
            source=src, sheet=ps.sheet, table_key=key, template_fingerprint=ps.fingerprint, header=ps.header,
            row_count=len(ps.frame), columns=columns, profile=profile, column_hints=hints,
            classification=classification, classification_confidence=conf, entity=entity, grain_candidate=grain,
            date_range=date_range, proposals=propose_targets(hints, classification, entity, self.contracts),
        )

    # ── Discovery brief ────────────────────────────────────────────────────

    def brief(self, src: SourceFile, sheets: list[SheetDiscovery], frames: dict[str | None, pd.DataFrame]) -> DiscoveryBrief:
        mcfg = self.markets.get(src.market)
        qs: list[OpenQuestion] = []
        prefilled: dict = {"market": src.market, "retailer": src.retailer,
                           "retailer_banner_in_customer_master": mcfg.banner(src.retailer or "")}
        if not src.market or not src.retailer:
            qs.append(OpenQuestion(topic="context", question="Market / retailer could not be read from the file "
                                   "path. Which market and retailer does this file belong to?"))
        sheet_rows = []
        for sd in sheets:
            df = frames[sd.sheet]
            present = types_present(sd.column_hints)
            label = f"[{sd.sheet}] " if sd.sheet else ""
            # currency
            currency, cur_src = None, None
            for col in present.get("net_value", []):
                c = currency_in_header(col)
                if c:
                    currency, cur_src = c, f"header '{col}'"
            if not currency and mcfg.currency and present.get("net_value"):
                currency, cur_src = mcfg.currency, f"market default ({src.market})"
            # date format + week end day
            date_info = {}
            for col in present.get("week_ending", []):
                fmt, rate, ambiguous = detect_date_format(df[col].dropna().tolist(), mcfg.date_dayfirst)
                if not fmt:
                    continue
                d = pd.to_datetime(df[col], format=fmt, errors="coerce").dropna()
                days = d.dt.dayofweek.value_counts()
                wday = _WEEKDAYS[int(days.idxmax())] if len(days) else None
                date_info = {"column": col, "format": fmt, "parse_rate": round(rate, 3), "ambiguous": ambiguous,
                             "week_end_day": wday}
                if ambiguous:
                    qs.append(OpenQuestion(topic="date", question=f"{label}Is '{col}' day-first or month-first?",
                                           proposed_answer=fmt, confidence=0.6))
                if wday and mcfg.week_end_day and wday != mcfg.week_end_day:
                    qs.append(OpenQuestion(
                        topic="calendar",
                        question=f"{label}Dates fall on {wday}, market default week end is {mcfg.week_end_day}. "
                                 "Should dates be shifted to the RB week end?",
                        proposed_answer="keep retailer date", confidence=0.5))
                break
            # EAN coverage -> how much will need description matching
            ean_cov = None
            for col in present.get("ean", []):
                vals = df[col]
                ean_cov = round(float(vals.map(lambda v: normalise_gtin(v) is not None).mean()), 3)
                if ean_cov < 0.9:
                    qs.append(OpenQuestion(
                        topic="product codes",
                        question=f"{label}Only {ean_cov:.0%} of '{col}' values are valid EANs (missing, truncated "
                                 "or Excel scientific notation). Can the retailer send EAN as text?",
                        proposed_answer="match remaining products on description + attributes", confidence=0.8))
            # weak or unknown columns
            for col, h in sd.column_hints.items():
                if h.semantic_type is None:
                    qs.append(OpenQuestion(topic="column", column=col, question=f"{label}What does column '{col}' "
                                           "contain? Not needed by any target unless confirmed.", confidence=0.0))
                elif h.confidence < self.review_threshold:
                    qs.append(OpenQuestion(topic="column", column=col, question=f"{label}Confirm '{col}' is {h.semantic_type}.",
                                           proposed_answer=h.semantic_type, confidence=h.confidence))
            primary = next((p for p in sd.proposals if p.primary), None)
            if primary and primary.missing_mandatory:
                qs.append(OpenQuestion(
                    topic="target", question=f"{label}{primary.target_table} needs "
                    f"{', '.join(primary.missing_mandatory)} but no source column was found. Where does it come from?"))
            if sd.classification == "unknown":
                qs.append(OpenQuestion(topic="classification", question=f"{label}Is this sheet a fact or a "
                                       "dimension? It could not be classified."))
            if sd.header.confidence < 0.5:
                qs.append(OpenQuestion(topic="layout", question=f"{label}Header row detected at row "
                                       f"{sd.header.header_row + 1} with low confidence. Is that correct?",
                                       proposed_answer=str(sd.header.header_row + 1), confidence=sd.header.confidence))
            sheet_rows.append({
                "sheet": sd.sheet, "rows": sd.row_count, "header_row": sd.header.header_row + 1,
                "header_confidence": sd.header.confidence, "title_rows": sd.header.title_rows,
                "footer_rows_dropped": sd.header.footer_rows_dropped,
                "classification": sd.classification, "entity": sd.entity,
                "grain": sd.grain_candidate, "date_range": sd.date_range, "date": date_info,
                "currency": currency, "currency_source": cur_src, "ean_valid_share": ean_cov,
                "targets": [p.model_dump() for p in sd.proposals],
                "columns": [{"column": c, "semantic_type": h.semantic_type, "confidence": h.confidence,
                             "source": h.source} for c, h in sd.column_hints.items()],
                "template_fingerprint": sd.template_fingerprint,
            })
        return DiscoveryBrief(file=src.name, market=src.market, retailer=src.retailer,
                              generated_at=datetime.now(timezone.utc), sheets=sheet_rows, prefilled=prefilled,
                              open_questions=qs)


def brief_to_markdown(b: DiscoveryBrief) -> str:
    out = [f"# Discovery brief: {b.file}", "",
           f"Market **{b.market}**, retailer **{b.retailer}**, generated {b.generated_at:%Y-%m-%d %H:%M} UTC", ""]
    for s in b.sheets:
        out.append(f"## Sheet: {s['sheet'] or '(single sheet)'}")
        out.append("")
        tgt = ", ".join(f"{t['target_table']} ({t['score']:.0%}{', primary' if t['primary'] else ''})"
                        for t in s["targets"])
        rows = [
            ("Rows", s["rows"]), ("Header row", f"{s['header_row']} (confidence {s['header_confidence']:.2f})"),
            ("Title rows above header", "; ".join(s["title_rows"]) or "none"),
            ("Footer rows dropped", s["footer_rows_dropped"]),
            ("Classification", f"{s['classification']}{' / ' + s['entity'] if s['entity'] else ''}"),
            ("Grain", ", ".join(s["grain"]) or "not unique on key columns"),
            ("Date range", " to ".join(s["date_range"]) if s["date_range"] else "n/a"),
            ("Date format", f"{s['date'].get('format')} (week ends {s['date'].get('week_end_day')})" if s["date"] else "n/a"),
            ("Currency", f"{s['currency']} from {s['currency_source']}" if s["currency"] else "unknown"),
            ("Valid EAN share", f"{s['ean_valid_share']:.0%}" if s["ean_valid_share"] is not None else "no EAN column"),
            ("Proposed targets", tgt),
        ]
        out += ["| Item | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows] + [""]
        out += ["| Column | Semantic type | Confidence | Source |", "|---|---|---|---|"]
        out += [f"| {c['column']} | {c['semantic_type'] or '-'} | {c['confidence']:.2f} | {c['source']} |"
                for c in s["columns"]]
        out.append("")
    out += ["## Open questions for the market connect", ""]
    if not b.open_questions:
        out.append("None. Everything needed was inferred from the data.")
    for i, q in enumerate(b.open_questions, 1):
        pa = f" Proposed: *{q.proposed_answer}*." if q.proposed_answer else ""
        out.append(f"{i}. **{q.topic}**: {q.question}{pa}")
    return "\n".join(out) + "\n"

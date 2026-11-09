"""M4 Code harmonisation: retailer product and store codes -> RB codes (Gate B, with M5).

Runs after Gate A, on the approved mappings. Order per code:
  1. code crosswalk (learned from earlier sign-offs): instant, confidence 1.0
  2. EAN / GTIN found in the ERP product master
  3. description and attributes (brand, variant, size, pack) against MDM; address for stores
  4. LLM adjudication between close candidates (when configured)

Each code gets the top 5 MDM candidates. Above the auto-accept threshold the code is accepted;
between the floors it is flagged for review; below it is unmatched and the reviewer picks from
the candidates or leaves it empty. Decisions are written to the crosswalk at Gate B, so the
same retailer code never needs review again.
"""
from __future__ import annotations

import pandas as pd

from onboarding_poc.core.harmonisation.outlet import OutletMatcher
from onboarding_poc.core.harmonisation.product import ProductMatcher
from onboarding_poc.core.schemas import SheetDiscovery
from onboarding_poc.session import Session, now

_PRODUCT_TYPES = ["retailer_sku_code", "ean", "product_description"]
_OUTLET_TYPES = ["retailer_outlet_code", "outlet_name", "address_line", "city", "postcode"]


def _best_col(sd: SheetDiscovery, semantic: str, min_conf: float = 0.5) -> str | None:
    best = [(c, h.confidence) for c, h in sd.column_hints.items() if h.semantic_type == semantic and h.confidence >= min_conf]
    return max(best, key=lambda x: x[1])[0] if best else None


def entity_frame(sheets: list[tuple[SheetDiscovery, pd.DataFrame]], types: list[str]) -> pd.DataFrame:
    parts = []
    for sd, f in sheets:
        cols = {t: _best_col(sd, t) for t in types}
        key_type = next((t for t in types if cols[t]), None)
        if key_type is None or (key_type != types[0] and types == _OUTLET_TYPES):
            continue
        part = pd.DataFrame({"__key": f[cols[key_type]]})
        for t, c in cols.items():
            if c:
                part[t] = f[c]
        parts.append(part)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df = df[df["__key"].notna()]
    return df.groupby("__key", sort=False).first().reset_index()


def entity_frame_tables(loaded: list, types: list[str]) -> pd.DataFrame:
    """Union of entity attributes over the loaded bronze tables, one row per retailer code.
    Columns are found by the target schema's semantic types (retailer_sku_code, ean, ...)."""
    parts = []
    for table, df in loaded:
        cols = {t: next((c.name for c in table.columns if c.semantic == t and c.name in df.columns), None) for t in types}
        if not cols[types[0]]:
            continue
        part = pd.DataFrame({"__key": df[cols[types[0]]]})
        for t, c in cols.items():
            if c:
                part[t] = df[c].values
        parts.append(part)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    out = out[out["__key"].notna()]
    return out.groupby("__key", sort=False).first().reset_index()


class CodeHarmonisation:
    module = "M4"

    def __init__(self, ctx, discovery, mapping, crosswalk):
        self.ctx, self.discovery, self.mapping, self.crosswalk = ctx, discovery, mapping, crosswalk

    def _matchers(self, s: Session):
        th, contracts = self.ctx.cfg.thresholds, self.ctx.contracts(s)
        mdm = self.ctx.mdm.frames()
        mp = contracts.masters
        prod = ProductMatcher(mdm["erp_product"], mp["erp_product"].columns, self.ctx.catalogue.abbreviations,
                              th.match_auto_accept, th.match_review_floor, self.ctx.llm) if len(mdm["erp_product"]) else None
        outl = OutletMatcher(mdm["rb_customer"], mp["rb_customer"].columns, th.match_auto_accept,
                             th.match_review_floor) if len(mdm["rb_customer"]) else None
        return prod, outl

    def run(self, s: Session, user: str) -> dict:
        if s.gates["A"].status != "approved":
            raise ValueError("Gate A (mapping approval) must be approved before harmonisation")
        if s.stage not in ("harmonisation", "validate", "signoff", "done"):
            raise ValueError("Phase 2 starts once Reckitt's pipeline has loaded the tables: confirm the load first")
        from onboarding_poc.modules.pipeline_handoff import load_bronze
        contracts = self.ctx.contracts(s)
        docs = [d for d in self.mapping.all(s) if d.get("published")]
        # the loaded bronze rows of this session, read from Unity Catalog through the SQL warehouse
        loaded = [(contracts.tables[d["spec"]["target_table"]], load_bronze(self.ctx, s, d)) for d in docs]
        need = {"product", "outlet"}
        skipped: list[dict] = []
        prod_m, out_m = self._matchers(s)
        mcfg = self.ctx.markets.get(s.market)
        prev = {(m["entity"], m["source_key"]): m for m in self.load(s)}
        results = []
        for entity, types, matcher in (("product", _PRODUCT_TYPES, prod_m), ("outlet", _OUTLET_TYPES, out_m)):
            if entity not in need or matcher is None:
                continue
            frame = entity_frame_tables(loaded, types)
            if not len(frame):
                continue
            frame[types[0]] = frame["__key"]
            for t in types:
                if t not in frame.columns:
                    frame[t] = None
            attrs = [t for t in types[1:] if frame[t].notna().any()]
            if not attrs:
                # codes alone cannot be matched (no EAN, description, name or address loaded): leave them
                # to the dimension table that carries the attributes, instead of flooding the review list
                skipped.append({"entity": entity, "codes": len(frame),
                                "why": f"no {entity} attributes in the loaded tables ({', '.join(types[1:])})"})
                continue
            cw, rejected = self.crosswalk.codes(entity, s.market, s.retailer)
            frame = frame[~frame["__key"].astype(str).isin(rejected)]
            ms = matcher.match(frame, s.market, s.retailer, cw) if entity == "product" else \
                matcher.match(frame, s.market, s.retailer, mcfg.banner(s.retailer), cw)
            for m in ms:
                r = {"match_id": f"{entity[0]}:{m.source_key}", **m.model_dump(mode="json"),
                     "decision": None, "decided_by": None, "decided_at": None, "final_code": None}
                p = prev.get((entity, m.source_key))
                if p and p.get("decision"):            # keep reviewer decisions on re-run
                    r.update({k: p[k] for k in ("decision", "decided_by", "decided_at", "final_code")})
                elif m.method == "crosswalk":
                    r.update(decision="crosswalk", decided_by="library", final_code=m.rb_code)
                elif m.status == "auto_accepted":
                    r.update(decision="auto_accepted", decided_by=self.ctx.decided_by_ai, final_code=m.rb_code)
                results.append(r)
        self.ctx.store.put(s.session_id, "harmonisation/codes.json", results)
        if s.gates["B"].status in ("not_started", "approved"):
            s.gates["B"].status = "open"
        s.stage = "harmonisation"
        for f in s.files:
            f.status = "harmonised"
        self.ctx.store.save(s)
        summ = self.summary(results)
        if skipped:
            summ["skipped"] = skipped
            self.ctx.store.trace(s, self.module, "codes.skipped", "system", after=skipped,
                                 why="codes without attributes are not matched")
        self.ctx.store.trace(s, self.module, "codes.matched", self.ctx.decided_by_ai,
                             source=f"bronze: {', '.join(d['published']['target_fqn'] for d in docs)} · MDM: {', '.join(f'{k} {v['rows']} rows' for k, v in self.ctx.mdm.info().items() if isinstance(v, dict))}",
                             after=summ, why="crosswalk, then EAN, then description/address with top-5 candidates")
        return summ

    def load(self, s: Session) -> list[dict]:
        return self.ctx.store.get(s.session_id, "harmonisation/codes.json", [])

    @staticmethod
    def summary(results: list[dict]) -> dict:
        out: dict = {}
        for r in results:
            e = out.setdefault(r["entity"], {"total": 0, "crosswalk": 0, "auto_accepted": 0, "needs_review": 0,
                                             "unmatched": 0, "decided": 0, "pending": 0})
            e["total"] += 1
            k = "crosswalk" if r["method"] == "crosswalk" else r["status"]
            e[k] = e.get(k, 0) + 1
            if r["decision"] in ("accepted", "chosen", "rejected", "left_empty"):
                e["decided"] += 1
            elif r["status"] in ("needs_review", "unmatched") and not r["decision"]:
                e["pending"] += 1
        return out

    def queue(self, s: Session, entity: str | None = None, only_open: bool = False) -> list[dict]:
        rs = self.load(s)
        if entity:
            rs = [r for r in rs if r["entity"] == entity]
        if only_open:
            rs = [r for r in rs if r["status"] in ("needs_review", "unmatched") and not r["decision"]]
        order = {"needs_review": 0, "unmatched": 1, "auto_accepted": 2, "approved": 3}
        return sorted(rs, key=lambda r: (r["decision"] is not None, order.get(r["status"], 9), -r["confidence"]))

    def decide(self, s: Session, match_id: str, action: str, user: str, rb_code: str | None = None,
               note: str | None = None) -> dict:
        """action: accept (AI's code) | choose (a candidate or any MDM code) | reject (not ours) |
        leave_empty (unknown for now)."""
        rs = self.load(s)
        r = next((x for x in rs if x["match_id"] == match_id), None)
        if r is None:
            raise KeyError(f"match {match_id} not found")
        before = {"decision": r["decision"], "code": r["final_code"] or r["rb_code"]}
        if action == "accept":
            if not r["rb_code"]:
                raise ValueError("no proposed code to accept; choose a candidate")
            r.update(decision="accepted", final_code=r["rb_code"])
        elif action == "choose":
            if not rb_code:
                raise ValueError("choose needs rb_code")
            mdm = self.ctx.mdm.frames()["erp_product" if r["entity"] == "product" else "rb_customer"]
            key = self.ctx.contracts(s).masters["erp_product" if r["entity"] == "product" else "rb_customer"].key
            if len(mdm) and rb_code not in set(mdm[key].dropna()):
                raise ValueError(f"{rb_code} is not in the MDM extract")
            r.update(decision="chosen", final_code=rb_code)
        elif action == "reject":
            r.update(decision="rejected", final_code=None)
        elif action == "leave_empty":
            r.update(decision="left_empty", final_code=None)
        else:
            raise ValueError(f"unknown action {action}")
        r.update(decided_by=user, decided_at=now(), note=note)
        self.ctx.store.put(s.session_id, "harmonisation/codes.json", rs)
        if s.gates["B"].status == "approved":
            s.gates["B"].status = "reopened"
            self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, f"codes.{action}", user, subject=match_id, before=before,
                             after={"decision": r["decision"], "code": r["final_code"]},
                             why=note or r["reason"], confidence=r["confidence"])
        return r

    def bulk_accept(self, s: Session, user: str, min_confidence: float) -> int:
        n = 0
        for r in self.queue(s, only_open=True):
            if r["rb_code"] and r["confidence"] >= min_confidence:
                self.decide(s, r["match_id"], "accept", user, note=f"bulk accept >= {min_confidence}")
                n += 1
        return n

    def accepted_codes(self, s: Session) -> dict[str, dict[str, tuple[str, float]]]:
        out = {"product": {}, "outlet": {}}
        for r in self.load(s):
            if r["final_code"] and r["decision"] in ("crosswalk", "auto_accepted", "accepted", "chosen"):
                out[r["entity"]][r["source_key"]] = (r["final_code"],
                                                     1.0 if r["decision"] in ("accepted", "chosen") else r["confidence"])
        return out

    def learn(self, s: Session, user: str) -> dict:
        """Gate B: write every person's decision to the crosswalk."""
        out = {}
        for entity in ("product", "outlet"):
            ds = [{"source_key": r["source_key"], "decision": "rejected" if r["decision"] == "rejected" else "accepted",
                   "rb_code": r["final_code"], "method": r["method"], "confidence": r["confidence"],
                   "source_label": r["source_label"]}
                  for r in self.load(s) if r["entity"] == entity and r["decision"] in ("accepted", "chosen", "rejected")]
            if ds:
                out[entity] = self.crosswalk.learn(entity, s.market, s.retailer, ds, user, s.session_id)
        return out

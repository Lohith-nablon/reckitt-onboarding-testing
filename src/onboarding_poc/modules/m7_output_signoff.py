"""M7 Output and sign-off (Gate C).

Sign-off is refused while Gate A or B is open, validation has not run on the current mappings,
or any blocking check fails. On sign-off, everything goes to outputs/<session>/ in S3:

  bronze/<table>__<file>.csv|parquet   bronze-ready rows in the physical layout
  mappings/<table>__<file>.veritas.json Veritas mapping JSON (for Veritas / comparison)
  mappings/<table>__<file>.spec.json    internal mapping spec
  discovery/<file>.json|.md             discovery report
  validation/<table>__<file>.json       checks and Veritas compare
  trace.json                            every decision in the session
  manifest.json                         what was written, by whom, from which inputs

Then M8 learns: mappings go to the mapping library. Delta loading stays with Reckitt.
"""
from __future__ import annotations

import io
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from onboarding_poc.core.mapping.models import MappingSpec
from onboarding_poc.core.quality.layout import _hash_row, to_physical
from onboarding_poc.session import Session, now


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


class OutputSignoff:
    module = "M7"

    def __init__(self, ctx, discovery, mapping, validate, library):
        self.ctx, self.discovery, self.mapping, self.validate, self.library = ctx, discovery, mapping, validate, library

    def readiness(self, s: Session) -> dict:
        problems = []
        for g, label in (("A", "mapping approval"), ("B", "harmonisation review")):
            if s.gates[g].status != "approved":
                problems.append(f"Gate {g} ({label}) is {s.gates[g].status}")
        reps = {r["mapping_id"]: r for r in self.validate.reports(s)}
        for d in self.mapping.all(s):
            mid = d["spec"]["mapping_id"]
            r = reps.get(mid)
            tag = f"{d['file_name']} -> {d['spec']['target_table']}"
            if r is None:
                problems.append(f"{tag}: not validated yet")
            elif r.get("generated_at", "") < d.get("updated_at", ""):
                problems.append(f"{tag}: mapping changed after validation; validate again")
            elif r["blocking"]:
                problems.append(f"{tag}: blocking checks {r['blocking']}")
        return {"ready": not problems, "problems": problems,
                "rows": sum(r["rows"] for r in reps.values()), "tables": sorted({r["target_table"] for r in reps.values()})}

    def _bronze(self, s: Session, tgt: pd.DataFrame, table, file_name: str, submission: str) -> pd.DataFrame:
        t = datetime.now(timezone.utc)
        if table.physical:
            out, _ = to_physical(tgt, table, submission_id=submission, retailer=s.retailer, batch_id=s.session_id,
                                 source_file=file_name, now=t)
            return out
        cols = [c.name for c in table.columns if c.name in tgt.columns]
        out = tgt[cols].copy()
        out["load_batch_id"] = s.session_id
        out["load_ts"] = t.isoformat(timespec="seconds")
        out["source_file"] = file_name
        out["record_hash"] = [_hash_row(r) for r in tgt[cols].itertuples(index=False, name=None)]
        return out

    def signoff(self, s: Session, user: str, note: str | None = None) -> dict:
        r = self.readiness(s)
        if not r["ready"]:
            raise ValueError("not ready for sign-off: " + "; ".join(r["problems"]))
        st, base = self.ctx.storage, f"{self.ctx.cfg.paths.outputs}/{s.session_id}"
        fmt = self.ctx.cfg.output.format
        contracts = self.ctx.contracts(s)
        written, learned = [], []
        for d in self.mapping.all(s):
            tgt, checks, spec = self.validate.build(s, d)
            table = contracts.tables[spec.target_table]
            submission = uuid.uuid4().hex[:16]
            out = self._bronze(s, tgt, table, d["file_name"], submission)
            stem = f"{_slug(spec.target_table)}__{_slug(Path(d['file_name']).stem)}{'__' + _slug(d['sheet']) if d['sheet'] else ''}"
            if fmt == "parquet":
                buf = io.BytesIO()
                out.astype(str).where(out.notna(), None).to_parquet(buf, index=False)
                key = f"{base}/bronze/{stem}.parquet"
                st.write_bytes(key, buf.getvalue(), "application/octet-stream")
            else:
                key = f"{base}/bronze/{stem}.csv"
                st.write_bytes(key, out.to_csv(index=False).encode("utf-8"), "text/csv")
            written.append({"kind": "bronze", "key": key, "uri": st.uri(key), "rows": len(out), "table": spec.target_table,
                            "submission_id": submission, "file": d["file_name"]})
            if d.get("veritas"):
                k = f"{base}/mappings/{stem}.veritas.json"
                st.write_json(k, d["veritas"])
                written.append({"kind": "veritas_mapping", "key": k, "uri": st.uri(k)})
            k = f"{base}/mappings/{stem}.spec.json"
            st.write_json(k, d["spec"])
            written.append({"kind": "mapping_spec", "key": k, "uri": st.uri(k)})
            v = self.ctx.store.get(s.session_id, f"validation/{spec.mapping_id}.json")
            if v:
                k = f"{base}/validation/{stem}.json"
                st.write_json(k, {kk: vv for kk, vv in v.items() if kk != "preview"})
                written.append({"kind": "validation", "key": k, "uri": st.uri(k)})
            # M8 learning: the signed-off mapping becomes the library entry for this layout
            lib = self.library.save(spec, d.get("veritas"), s, d["file_name"], user)
            learned.append({"target_table": spec.target_table, "file": d["file_name"], **lib})
            self.ctx.store.trace(s, "M8", "library.mapping_saved", user, subject=f"{d['file_id']}:{spec.target_table}",
                                 after=lib, why="signed off: next file with this layout skips discovery and mapping")
        for f in s.files:
            rep = self.discovery.load(s, f.file_id)["report"]
            k = f"{base}/discovery/{_slug(Path(f.name).stem)}.json"
            st.write_json(k, {kk: vv for kk, vv in rep.items() if kk != "markdown"})
            st.write_text(f"{base}/discovery/{_slug(Path(f.name).stem)}.md", rep.get("markdown") or "", "text/markdown")
            written.append({"kind": "discovery_report", "key": k, "uri": st.uri(k)})
            f.status = "signed_off"
        s.gates["C"] = s.gates["C"].model_copy(update={"status": "approved", "decided_by": user, "decided_at": now(),
                                                       "note": note})
        s.status, s.stage = "signed_off", "done"
        manifest = {"session_id": s.session_id, "market": s.market, "retailer": s.retailer, "signed_off_by": user,
                    "signed_off_at": now(), "note": note, "files": [f.model_dump() for f in s.files],
                    "gates": {k: g.model_dump() for k, g in s.gates.items()}, "outputs": written, "library": learned,
                    "llm": self.ctx.llm.label}
        st.write_json(f"{base}/manifest.json", manifest)
        s.outputs = {"base": st.uri(base), "items": written, "library": learned}
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "gate_c.signed_off", user, why=note or "output signed off",
                             after={"files_written": len(written), "rows": r["rows"]})
        st.write_json(f"{base}/trace.json", self.ctx.store.events(s.session_id))
        return manifest

    def outputs(self, s: Session) -> list[dict]:
        base = f"{self.ctx.cfg.paths.outputs}/{s.session_id}/"
        return [{"key": k, "name": k[len(base):], "uri": self.ctx.storage.uri(k)} for k in self.ctx.storage.list(base)]

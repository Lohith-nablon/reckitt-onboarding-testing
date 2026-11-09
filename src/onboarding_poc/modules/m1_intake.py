"""M1 Intake: start an onboarding session, add retailer files and the mapping file.

Two ways in, as agreed: manual upload in the UI, or an S3 path with the client's credentials.
Every file is copied to inbox/<session>/<file>/ so the session never depends on the client
bucket after intake. The mapping file (market target schema) is read into target tables; without
one the built-in POS_SELLOUT stand-in is used.

Each new file is fingerprinted per sheet and checked against the mapping library (M8). A file
whose every sheet has an approved mapping is a known layout: discovery and mapping review are
skipped.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from onboarding_poc.core.contracts import Contracts
from onboarding_poc.core.onboarding import FieldValue, OnboardingRegistry, completeness, suggestions
from onboarding_poc.core.mapping.proposer import Conventions
from onboarding_poc.core.schema_import import build_tables, read_schema
from onboarding_poc.session import FileState, Session, SheetState, TargetState, new_id, now, rows_jsonable
from onboarding_poc.storage import parse_s3_uri, s3_client

ALLOWED = {".csv", ".txt", ".tsv", ".xlsx", ".xls", ".xlsm"}
SCHEMA_ALLOWED = {".csv", ".xlsx", ".xls", ".sql", ".ddl", ".json"}
MAX_BYTES = 200 * 1024 * 1024


class IntakeError(ValueError):
    pass


class Intake:
    module = "M1"

    def __init__(self, ctx, library):
        self.ctx, self.library = ctx, library

    # ── session ────────────────────────────────────────────────────────────
    def create(self, name: str, market: str, retailer: str, user: str, profile: dict | None = None) -> Session:
        market, retailer = market.strip().upper(), retailer.strip().upper()
        if not market or not retailer:
            raise IntakeError("market and retailer are required")
        t = now()
        s = Session(session_id=new_id("onb-"), name=name.strip() or f"{retailer} {market}", market=market,
                    retailer=retailer, created_by=user, created_at=t, updated_at=t)
        for k, v in (profile or {}).items():
            if v not in (None, ""):
                s.profile[k] = {"value": str(v), "status": "confirmed", "provided_by": user}
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "session.created", user, subject=s.session_id,
                             after={"market": market, "retailer": retailer, "profile": list(s.profile)})
        return s

    def registry(self, s: Session) -> OnboardingRegistry:
        return OnboardingRegistry(self.ctx.cfg.core_config_dir, self.ctx.contracts(s))

    def conventions(self, s: Session) -> Conventions:
        mcfg = self.ctx.markets.get(s.market)
        return Conventions.build(self.library.veritas_docs(), s.market, s.retailer, self.ctx.markets.by_legc,
                                 mcfg.display_name(s.retailer))

    def form(self, s: Session) -> dict:
        """Business fields for the Start onboarding screen, with suggested values (and where they
        came from) for anything not filled yet."""
        reg = self.registry(s)
        contracts = self.ctx.contracts(s)
        sugg = suggestions(s.market, s.retailer, contracts, self.ctx.markets, self.conventions(s))
        values = {k: FieldValue(key=k, value=v.get("value"), status=v.get("status"), provided_by=v.get("provided_by"))
                  for k, v in s.profile.items()}
        fields = []
        for f in reg.fields.values():
            v = s.profile.get(f.key) or {}
            sg = sugg.get(f.key)
            fields.append({**f.model_dump(), "value": v.get("value"), "status": v.get("status"),
                           "provided_by": v.get("provided_by"),
                           "suggestion": {"value": sg[0], "why": sg[1]} if sg and not v.get("value") else None})
        return {"groups": reg.groups, "fields": fields,
                "completeness": completeness(list(reg.fields.values()), values)}

    def update_profile(self, s: Session, values: dict, user: str) -> Session:
        changed = {}
        for k, v in values.items():
            before = (s.profile.get(k) or {}).get("value")
            if v in (None, ""):
                if k in s.profile:
                    s.profile.pop(k)
                    changed[k] = (before, None)
                continue
            if str(v) != before:
                s.profile[k] = {"value": str(v), "status": "confirmed", "provided_by": user}
                changed[k] = (before, str(v))
        self.ctx.store.save(s)
        for k, (b, a) in changed.items():
            self.ctx.store.trace(s, self.module, "profile.field_set", user, subject=k, before=b, after=a,
                                 why="entered on Start onboarding")
        return s

    # ── target schema (mapping file) ──────────────────────────────────────
    def set_target_schema(self, s: Session, filename: str, data: bytes, user: str) -> dict:
        ext = Path(filename).suffix.lower()
        if ext not in SCHEMA_ALLOWED:
            raise IntakeError(f"mapping file type {ext} not supported ({', '.join(sorted(SCHEMA_ALLOWED))})")
        try:
            rows = read_schema(data, filename)
            tables, report = build_tables(rows, self.ctx.catalogue)
        except Exception as exc:
            raise IntakeError(f"could not read the mapping file: {exc}") from exc
        if not tables:
            raise IntakeError("no target columns found: the file needs at least 'table' and 'column' headers")
        key = f"{self.ctx.cfg.paths.inbox}/{s.session_id}/target/{filename}"
        self.ctx.storage.write_bytes(key, data)
        before = list(s.target.tables) if s.target.source == "uploaded" else ["(built-in POS_SELLOUT)"]
        s.target = TargetState(source="uploaded", file_name=filename, key=key, tables=tables,
                               import_report=report.model_dump())
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "target.schema_loaded", user, subject=filename, source=key,
                             before=before, after=list(tables),
                             why=f"{sum(t['columns'] for t in report.tables.values())} target columns read; "
                                 f"{sum(len(t['guesses']) for t in report.tables.values())} guesses to check")
        self._refresh_library_hits(s)
        return {"tables": list(tables), "report": report.model_dump()}

    def target_view(self, s: Session) -> dict:
        c: Contracts = self.ctx.contracts(s)
        return {"source": s.target.source, "file_name": s.target.file_name, "report": s.target.import_report,
                "tables": [{"name": t.name, "kind": t.kind, "veritas_dataset": t.veritas_dataset,
                            "columns": [{"name": col.name, "dtype": col.dtype, "mandatory": col.mandatory, "pk": col.pk,
                                         "field_source": col.field_source, "semantic": col.semantic,
                                         "description": col.description} for col in t.columns]}
                           for t in c.tables.values()]}

    # ── retailer files ─────────────────────────────────────────────────────
    def add_upload(self, s: Session, filename: str, data: bytes, user: str, source: str = "upload") -> FileState:
        filename = Path(filename).name
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED:
            raise IntakeError(f"{filename}: file type {ext} not supported ({', '.join(sorted(ALLOWED))})")
        if not data:
            raise IntakeError(f"{filename} is empty")
        if len(data) > MAX_BYTES:
            raise IntakeError(f"{filename} is larger than {MAX_BYTES // 1024 // 1024} MB")
        sha = hashlib.sha256(data).hexdigest()
        dup = next((f for f in s.files if f.sha256 == sha), None)
        if dup:
            raise IntakeError(f"{filename} is the same file as {dup.name}, already in this session")
        fid = "f" + sha[:10]
        key = f"{self.ctx.cfg.paths.inbox}/{s.session_id}/{fid}/{filename}"
        self.ctx.storage.write_bytes(key, data)
        f = FileState(file_id=fid, name=filename, key=key, source=source, size_bytes=len(data), sha256=sha,
                      added_by=user, added_at=now())
        s.files.append(f)
        try:
            sheets = self.ctx.parsed(s, fid)
        except Exception as exc:
            s.files.remove(f)
            self.ctx.storage.delete(key)
            raise IntakeError(f"{filename} could not be read: {exc}") from exc
        if not sheets:
            s.files.remove(f)
            raise IntakeError(f"{filename} has no data rows")
        f.sheets = [SheetState(sheet=ps.sheet, fingerprint=ps.fingerprint, rows=len(ps.frame), columns=list(ps.frame.columns),
                               header_row=ps.header.header_row, footer_rows=ps.header.footer_rows_dropped,
                               file_format=ps.file_format, delimiter=ps.delimiter) for ps in sheets]
        self._library_hits(s, f)
        if s.stage == "start":
            s.stage = "files"
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "file.added", user, subject=fid, source=source,
                             after={"name": filename, "bytes": len(data), "sheets": [
                                 {"sheet": x.sheet, "rows": x.rows, "columns": len(x.columns), "header_row": x.header_row}
                                 for x in f.sheets]},
                             why="known layout: approved mapping found in the library" if f.known_layout else
                             "new layout: goes through AI Discovery and mapping review")
        return f

    def add_from_s3(self, s: Session, uri: str, user: str, access_key: str | None = None, secret_key: str | None = None,
                    session_token: str | None = None, region: str | None = None) -> list[FileState]:
        """Copy one object, or every supported object under a prefix, from the client's bucket.
        Credentials are used for this call only and never stored."""
        bucket, key = parse_s3_uri(uri)
        client = s3_client(self.ctx.cfg.storage.s3, access_key, secret_key, session_token, region)
        keys = [key] if key and not key.endswith("/") else []
        if not keys:
            r = client.list_objects_v2(Bucket=bucket, Prefix=key)
            keys = [o["Key"] for o in r.get("Contents", []) if Path(o["Key"]).suffix.lower() in ALLOWED]
        if not keys:
            raise IntakeError(f"no supported files at {uri}")
        out = []
        for k in keys[:50]:
            data = client.get_object(Bucket=bucket, Key=k)["Body"].read()
            out.append(self.add_upload(s, Path(k).name, data, user, source=f"s3://{bucket}/{k}"))
        return out

    def landing(self, prefix: str = "") -> list[dict]:
        """Raw files in the bronze landing zone (ADLS in Reckitt), for the Add files screen."""
        base = self.ctx.cfg.paths.landing.strip("/")
        keys = self.ctx.storage.list(f"{base}/{prefix.strip('/')}" if prefix else base)
        return [{"key": k, "name": Path(k).name, "path": k[len(base) + 1:], "uri": self.ctx.storage.uri(k)}
                for k in keys if Path(k).suffix.lower() in ALLOWED]

    def add_from_landing(self, s: Session, keys: list[str], user: str) -> list[FileState]:
        base = self.ctx.cfg.paths.landing.strip("/") + "/"
        out = []
        for k in keys:
            if not k.startswith(base) or ".." in k:
                raise IntakeError(f"{k} is not in the landing zone")
            out.append(self.add_upload(s, Path(k).name, self.ctx.storage.read_bytes(k), user,
                                       source=self.ctx.storage.uri(k)))
        return out

    def remove_file(self, s: Session, file_id: str, user: str) -> None:
        f = s.file(file_id)
        if s.gates["A"].status == "approved":
            raise IntakeError("mappings are approved; reopen Gate A before removing files")
        s.files.remove(f)
        s.mapping_ids = [m for m in s.mapping_ids
                         if (self.ctx.store.get(s.session_id, f"mappings/{m}.json") or {}).get("file_id") != file_id]
        self.ctx.store.save(s)
        self.ctx.store.trace(s, self.module, "file.removed", user, subject=file_id, before=f.name)

    def preview(self, s: Session, file_id: str, rows: int = 20) -> dict:
        out = []
        for ps in self.ctx.parsed(s, file_id):
            out.append({"sheet": ps.sheet, "header_row": ps.header.header_row, "header_confidence": ps.header.confidence,
                        "title_rows": ps.header.title_rows, "footer_rows_dropped": ps.header.footer_rows_dropped,
                        "columns": list(ps.frame.columns),
                        "rows": rows_jsonable(ps.frame.head(rows)), "total_rows": len(ps.frame)})
        return {"file_id": file_id, "sheets": out}

    # ── known layouts ──────────────────────────────────────────────────────
    def _library_hits(self, s: Session, f: FileState) -> None:
        targets = set(self.ctx.contracts(s).tables)
        for sh in f.sheets:
            hits = [e for e in self.library.for_layout(s.market, s.retailer, sh.fingerprint)
                    if e["spec"]["target_table"] in targets]
            sh.library_hit = ",".join(f"{e['spec']['target_table']}@v{e['version']}" for e in hits) or None
        f.known_layout = bool(f.sheets) and all(sh.library_hit for sh in f.sheets)

    def _refresh_library_hits(self, s: Session) -> None:
        for f in s.files:
            self._library_hits(s, f)
        self.ctx.store.save(s)

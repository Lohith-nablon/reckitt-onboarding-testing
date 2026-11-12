"""FastAPI app: the REST API behind the seven screens, plus the React front end (frontend/dist)
served from the same Databricks App.

Every write is attributed to the signed-in user. On Databricks Apps the user comes from the
X-Forwarded-Email header the platform sets; locally (auth.mode = none) a dev user is used.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from onboarding_poc.config import AppConfig, get_config
from onboarding_poc.modules.m1_intake import IntakeError
from onboarding_poc.service import OnboardingService


# ── request bodies ──────────────────────────────────────────────────────────
class NewSession(BaseModel):
    name: str = ""
    market: str
    retailer: str
    profile: dict[str, Any] = {}


class ProfileUpdate(BaseModel):
    values: dict[str, Any]


class S3Intake(BaseModel):
    uri: str
    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    region: str | None = None


class FromLanding(BaseModel):
    key: str
    market: str | None = None
    retailer: str | None = None


class LandingFiles(BaseModel):
    keys: list[str]


class ColumnType(BaseModel):
    sheet: str | None = None
    column: str
    semantic_type: str | None
    note: str | None = None


class Answer(BaseModel):
    answer: str


class LinkChange(BaseModel):
    change: dict[str, Any]
    note: str | None = None


class AcceptLinks(BaseModel):
    targets: list[str]


class Note(BaseModel):
    note: str | None = None


class CodeDecision(BaseModel):
    action: str                 # accept | choose | reject | leave_empty
    rb_code: str | None = None
    note: str | None = None


class BulkAccept(BaseModel):
    min_confidence: float = 0.8


class RuleDecision(BaseModel):
    approve: bool = True
    value: str | None = None
    note: str | None = None


class StandardDecision(BaseModel):
    mapping: dict[str, str] | None = None     # value -> standard; empty = accept as proposed
    note: str | None = None


def create_app(cfg: AppConfig | None = None, service: OnboardingService | None = None) -> FastAPI:
    cfg = cfg or get_config()
    svc = service or OnboardingService(cfg)
    app = FastAPI(title="Reckitt Retailer Onboarding POC", version="0.1.0",
                  description="Retailer files to bronze-ready output: M1-M8 behind seven screens.")
    app.state.svc = svc
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"],
                       allow_headers=["*"], allow_credentials=True)

    @app.exception_handler(IntakeError)
    @app.exception_handler(ValueError)
    async def _bad(_: Request, exc: Exception):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def _crash(_: Request, exc: Exception):
        # any other failure (no permission in Unity Catalog, warehouse stopped, ...) as readable JSON
        logger.exception("request failed")
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {str(exc)[:600]}"})

    @app.exception_handler(KeyError)
    async def _missing(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": str(exc.args[0]) if exc.args else "not found"})

    def user(request: Request) -> str:
        if cfg.auth.mode == "databricks_app":
            u = request.headers.get("x-forwarded-email") or request.headers.get("x-forwarded-preferred-username")
            if not u:
                raise HTTPException(401, "no signed-in user (X-Forwarded-Email missing)")
            return u
        return request.headers.get("x-dev-user") or cfg.auth.dev_user

    def sess(sid: str):
        return svc.session(sid)

    # ── platform ───────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        return {"ok": True, "storage": cfg.storage.backend, "llm": svc.ctx.llm.label,
                "warehouse": cfg.warehouse.backend}

    @app.get("/api/me")
    def me(u: str = Depends(user)):
        return {"user": u}

    @app.get("/api/config")
    def config():
        return cfg.public()

    @app.get("/api/catalogue/semantic-types")
    def semantic_types():
        return [{"name": t.name, "kind": t.kind, "role": t.role} for t in svc.ctx.catalogue.types.values()]

    @app.get("/api/markets")
    def markets():
        return [{"code": m.code, "currency": m.currency, "retailers": [{"code": r, **v} for r, v in m.retailers.items()]}
                for m in svc.ctx.markets.markets.values()]

    @app.get("/api/mdm")
    def mdm_info():
        return svc.ctx.mdm.info()

    @app.get("/api/mdm/search")
    def mdm_search(entity: str = "product", q: str = "", limit: int = 20):
        name = "erp_product" if entity == "product" else "rb_customer"
        df = svc.ctx.mdm.frames()[name]
        if not len(df):
            return []
        spec = svc.ctx.default_contracts.masters[name]
        hay = df.astype(str).agg(" ".join, axis=1).str.lower()
        hit = df[hay.str.contains(q.lower(), regex=False)] if q else df
        label = spec.columns.get("description") or spec.columns.get("name")
        return [{"code": r[spec.key], "label": r.get(label)} for r in hit.head(limit).to_dict("records")]

    @app.post("/api/admin/seed")
    def admin_seed(u: str = Depends(user)):
        """Load the demo dataset (MDM, Veritas #4285, landing-zone files) into the configured storage."""
        from onboarding_poc.seed import seed
        keys = seed(cfg, svc.ctx.storage, svc.ctx.warehouse if cfg.mdm.source == "uc" else None)
        if keys.get("rb_master_error"):
            svc.ctx.mdm.invalidate()
            raise ValueError("demo files loaded, but the RB code tables could not be written: "
                             f"{keys['rb_master_error']}. Check that schema {cfg.mdm.uc_catalog or cfg.uc.bronze_catalog}."
                             f"{cfg.mdm.uc_schema} exists (setup.sql) and the app has CREATE TABLE and MODIFY on it.")
        svc.ctx.mdm.invalidate()
        return {"loaded": keys, "by": u}

    @app.get("/api/admin/environment")
    def environment():
        from onboarding_poc.mapping_store import MappingStore
        ms = MappingStore(svc.ctx)
        out = {"storage": cfg.storage.backend, "storage_root": svc.ctx.storage.uri(""), "warehouse": cfg.warehouse.backend,
               "pipeline_run_allowed": cfg.warehouse.allow_pipeline_run or cfg.warehouse.backend == "local",
               "mapping_store": {"format": "delta", "mode": ms.mode}, "llm": svc.ctx.llm.label,
               "bronze": f"{cfg.uc.bronze_catalog}.{cfg.uc.bronze_schema}", "silver": f"{cfg.uc.silver_catalog}.{cfg.uc.silver_schema}",
               "landing_files": len(svc.intake.landing()), "pipeline_mode": cfg.pipeline.mode,
               "mdm_source": cfg.mdm.source,
               "mdm_tables": list(svc.ctx.mdm.tables().values()) if cfg.mdm.source == "uc" else None,
               "audit_table": svc.ctx.audit.table if svc.ctx.audit is not None else None}
        try:
            out["mapping_store"]["location"] = ms.location()
        except Exception as exc:
            out["mapping_store"]["error"] = str(exc)
        return out

    @app.get("/api/sessions/{sid}/mapping-store")
    def mapping_store(sid: str):
        from onboarding_poc.mapping_store import MappingStore
        from onboarding_poc.session import rows_jsonable
        ms = MappingStore(svc.ctx)
        df = ms.read(sid)
        show = [c for c in df.columns if c not in ("mapping_json", "spec_json", "sql")]
        return {"location": ms.location(), "mode": ms.mode, "rows": len(df), "columns": show,
                "preview": rows_jsonable(df[show])}

    @app.get("/api/library/mappings")
    def library():
        return svc.library.entries()

    @app.get("/api/library/crosswalk")
    def crosswalk():
        return svc.crosswalk.stats()

    # ── M1 Start onboarding / Add files ───────────────────────────────────
    @app.get("/api/sessions")
    def list_sessions():
        return [{k: getattr(s, k) for k in ("session_id", "name", "market", "retailer", "stage", "status", "created_by",
                                            "created_at", "updated_at")} | {"files": len(s.files)}
                for s in svc.ctx.store.list()]

    @app.post("/api/sessions")
    def create_session(body: NewSession, u: str = Depends(user)):
        return svc.intake.create(body.name, body.market, body.retailer, u, body.profile).model_dump()

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str):
        return svc.overview(sess(sid))

    @app.get("/api/sessions/{sid}/form")
    def form(sid: str):
        return svc.intake.form(sess(sid))

    @app.put("/api/sessions/{sid}/profile")
    def profile(sid: str, body: ProfileUpdate, u: str = Depends(user)):
        s = svc.intake.update_profile(sess(sid), body.values, u)
        return svc.intake.form(s)

    @app.get("/api/sessions/{sid}/target")
    def target(sid: str):
        return svc.intake.target_view(sess(sid))

    @app.post("/api/sessions/{sid}/target")
    async def upload_target(sid: str, file: UploadFile = File(...), u: str = Depends(user)):
        return svc.intake.set_target_schema(sess(sid), file.filename or "mapping_file", await file.read(), u)

    @app.post("/api/sessions/{sid}/files")
    async def upload_files(sid: str, files: list[UploadFile] = File(...), u: str = Depends(user)):
        s = sess(sid)
        added, errors = [], []
        for f in files:
            try:
                added.append(svc.intake.add_upload(s, f.filename or "file", await f.read(), u).model_dump())
            except IntakeError as exc:
                errors.append({"file": f.filename, "error": str(exc)})
        return {"added": added, "errors": errors}

    @app.post("/api/sessions/{sid}/files/s3")
    def files_from_s3(sid: str, body: S3Intake, u: str = Depends(user)):
        fs = svc.intake.add_from_s3(sess(sid), body.uri, u, body.access_key, body.secret_key, body.session_token,
                                    body.region)
        return {"added": [f.model_dump() for f in fs], "errors": []}

    @app.get("/api/landing")
    def landing(prefix: str = ""):
        """Raw retailer files in the bronze landing zone (ADLS)."""
        return svc.intake.landing(prefix)

    @app.get("/api/inbox")
    def inbox():
        """New files waiting in the landing zone (and which ones are already onboarded)."""
        return svc.inbox()

    @app.post("/api/inbox/start")
    def inbox_start(body: FromLanding, u: str = Depends(user)):
        return svc.start_from_landing(body.key, u, body.market, body.retailer)

    @app.get("/api/metrics")
    def metrics_system():
        """KPIs across all sessions: totals, monthly roll-up, accuracy over time."""
        return svc.metrics.system()

    @app.get("/api/sessions/{sid}/metrics")
    def metrics_session(sid: str):
        return svc.metrics.session(sess(sid))

    @app.get("/api/admin/check-uc")
    def check_uc():
        from onboarding_poc.connectivity import check
        out = check(svc.ctx)
        if svc.ctx.audit is not None:
            out["audit"] = {"table": svc.ctx.audit.table, "written": svc.ctx.audit.written,
                            "last_error": svc.ctx.audit.last_error}
        return out

    @app.get("/api/samples")
    def samples():
        return svc.intake.landing()

    @app.post("/api/sessions/{sid}/files/landing")
    def add_landing(sid: str, body: LandingFiles, u: str = Depends(user)):
        return {"added": [f.model_dump() for f in svc.intake.add_from_landing(sess(sid), body.keys, u)], "errors": []}

    @app.delete("/api/sessions/{sid}/files/{fid}")
    def remove_file(sid: str, fid: str, u: str = Depends(user)):
        svc.intake.remove_file(sess(sid), fid, u)
        return {"removed": fid}

    @app.get("/api/sessions/{sid}/files/{fid}/preview")
    def file_preview(sid: str, fid: str, rows: int = 20):
        return svc.intake.preview(sess(sid), fid, rows)

    # ── M2 AI Discovery ────────────────────────────────────────────────────
    @app.post("/api/sessions/{sid}/analyse")
    def analyse(sid: str, u: str = Depends(user)):
        return svc.analyse(sess(sid), u)

    @app.post("/api/sessions/{sid}/discovery/run")
    def run_discovery(sid: str, u: str = Depends(user)):
        return svc.discovery.run(sess(sid), u)

    @app.get("/api/sessions/{sid}/discovery/{fid}")
    def discovery(sid: str, fid: str):
        return svc.discovery.load(sess(sid), fid)["report"]

    @app.put("/api/sessions/{sid}/discovery/{fid}/columns")
    def column_type(sid: str, fid: str, body: ColumnType, u: str = Depends(user)):
        return svc.discovery.set_column_type(sess(sid), fid, body.sheet, body.column, body.semantic_type, u, body.note)

    @app.post("/api/sessions/{sid}/discovery/{fid}/questions/{qid}")
    def answer(sid: str, fid: str, qid: str, body: Answer, u: str = Depends(user)):
        return svc.discovery.answer(sess(sid), fid, qid, body.answer, u)

    # ── M3 Mapping + Gate A ────────────────────────────────────────────────
    @app.get("/api/sessions/{sid}/mappings")
    def mappings(sid: str):
        s = sess(sid)
        return [svc.mapping.summary(s, d) for d in svc.mapping.all(s)]

    @app.post("/api/sessions/{sid}/mappings/propose")
    def propose(sid: str, u: str = Depends(user)):
        return svc.mapping.propose(sess(sid), u)

    @app.get("/api/sessions/{sid}/mappings/{mid}/canvas")
    def canvas(sid: str, mid: str):
        return svc.mapping.canvas(sess(sid), mid)

    @app.get("/api/sessions/{sid}/mappings/{mid}/preview")
    def mapping_preview(sid: str, mid: str, rows: int = 20):
        return svc.mapping.preview(sess(sid), mid, rows)

    @app.put("/api/sessions/{sid}/mappings/{mid}/links/{target}")
    def change_link(sid: str, mid: str, target: str, body: LinkChange, u: str = Depends(user)):
        return svc.mapping.update_link(sess(sid), mid, target, body.change, u, body.note)

    @app.post("/api/sessions/{sid}/mappings/{mid}/accept")
    def accept_links(sid: str, mid: str, body: AcceptLinks, u: str = Depends(user)):
        return {"accepted": svc.mapping.accept_link(sess(sid), mid, body.targets, u)}

    @app.get("/api/sessions/{sid}/mappings/{mid}/veritas")
    def veritas(sid: str, mid: str):
        d = svc.mapping.load(sess(sid), mid)
        if not d.get("veritas"):
            raise HTTPException(404, "this target has no Veritas dataset")
        return Response(json.dumps(d["veritas"], indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{mid}.veritas.json"'})

    @app.get("/api/sessions/{sid}/conversions")
    def conversions(sid: str):
        return svc.values.rules(sess(sid))

    @app.post("/api/sessions/{sid}/conversions/{rule_id:path}")
    def conversion_decision(sid: str, rule_id: str, body: RuleDecision, u: str = Depends(user)):
        return svc.values.decide(sess(sid), rule_id, u, body.approve, body.value, body.note)

    @app.post("/api/sessions/{sid}/gates/A/approve")
    def gate_a(sid: str, body: Note, u: str = Depends(user)):
        return svc.approve_gate_a(sess(sid), u, body.note)

    # ── Hand-off: Reckitt's pipeline ───────────────────────────────────────
    @app.get("/api/sessions/{sid}/pipeline")
    def pipeline_status(sid: str, check: bool = True):
        return svc.pipeline_status(sid, check)

    @app.post("/api/sessions/{sid}/pipeline/start-job")
    def pipeline_start_job(sid: str, u: str = Depends(user)):
        """Start the pipeline job again (retry after a failure)."""
        s = sess(sid)
        if s.gates["A"].status != "approved" or s.stage != "pipeline":
            raise ValueError("the pipeline job runs after Gate A, while the session is at the Pipeline step")
        return svc.start_pipeline_job(s, u)

    @app.get("/api/sessions/{sid}/pipeline/sql/{mid}")
    def pipeline_sql(sid: str, mid: str):
        return Response(svc.pipeline.sql(sess(sid), mid), media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{mid}.sql"'})

    @app.post("/api/sessions/{sid}/pipeline/run-locally")
    def pipeline_run(sid: str, u: str = Depends(user)):
        return svc.pipeline.run_locally(sess(sid), u)

    @app.post("/api/sessions/{sid}/pipeline/confirm")
    def pipeline_confirm(sid: str, body: Note, u: str = Depends(user)):
        return svc.pipeline.confirm(sess(sid), u, body.note)

    # ── M4 + M5 Harmonisation + Gate B ─────────────────────────────────────
    @app.post("/api/sessions/{sid}/harmonisation/run")
    def harmonise(sid: str, u: str = Depends(user)):
        return svc.run_harmonisation(sess(sid), u)

    @app.get("/api/sessions/{sid}/harmonisation")
    def harmonisation(sid: str):
        return svc.harmonisation_view(sess(sid))

    @app.post("/api/sessions/{sid}/harmonisation/codes/bulk-accept")
    def bulk_accept(sid: str, body: BulkAccept, u: str = Depends(user)):
        return {"accepted": svc.codes.bulk_accept(sess(sid), u, body.min_confidence)}

    @app.post("/api/sessions/{sid}/harmonisation/codes/{match_id}")
    def code_decision(sid: str, match_id: str, body: CodeDecision, u: str = Depends(user)):
        return svc.codes.decide(sess(sid), match_id, body.action, u, body.rb_code, body.note)

    @app.post("/api/sessions/{sid}/harmonisation/standards/{rule_id:path}")
    def standard_decision(sid: str, rule_id: str, body: StandardDecision, u: str = Depends(user)):
        return svc.standards.decide(sess(sid), rule_id, u, body.mapping, body.note)

    @app.post("/api/sessions/{sid}/gates/B/approve")
    def gate_b(sid: str, body: Note, u: str = Depends(user)):
        return svc.approve_gate_b(sess(sid), u, body.note)

    # ── M6 Validate & preview ──────────────────────────────────────────────
    @app.post("/api/sessions/{sid}/validate")
    def validate(sid: str, u: str = Depends(user)):
        return svc.validate.run(sess(sid), u)

    @app.get("/api/sessions/{sid}/validate")
    def validation(sid: str):
        return svc.validate.reports(sess(sid))

    # ── M7 Sign off + Gate C ───────────────────────────────────────────────
    @app.get("/api/sessions/{sid}/signoff")
    def readiness(sid: str):
        return svc.output.readiness(sess(sid))

    @app.post("/api/sessions/{sid}/gates/C/approve")
    def gate_c(sid: str, body: Note, u: str = Depends(user)):
        return svc.output.signoff(sess(sid), u, body.note)

    @app.get("/api/sessions/{sid}/silver")
    def silver(sid: str, rows: int = 50):
        return svc.output.silver_preview(sess(sid), rows)

    @app.get("/api/sessions/{sid}/outputs")
    def outputs(sid: str):
        return svc.output.outputs(sess(sid))

    @app.get("/api/sessions/{sid}/outputs/download")
    def download(sid: str, key: str):
        prefix = f"{cfg.paths.outputs}/{sid}/"
        if not key.startswith(prefix) or ".." in key:
            raise HTTPException(400, "key outside this session's outputs")
        data = svc.ctx.storage.read_bytes(key)
        return Response(data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{Path(key).name}"'})

    # ── M8 File journey + trace ────────────────────────────────────────────
    @app.get("/api/sessions/{sid}/journey")
    def journey(sid: str):
        return svc.journey.build(sess(sid))

    @app.get("/api/sessions/{sid}/trace")
    def trace(sid: str, module: str | None = None, subject: str | None = None):
        ev = svc.ctx.store.events(sid)
        if module:
            ev = [e for e in ev if e["module"] == module]
        if subject:
            ev = [e for e in ev if (e.get("subject") or "").startswith(subject)]
        return ev

    # ── front end (same app) ───────────────────────────────────────────────
    dist = Path(cfg.frontend_dist)
    if (dist / "index.html").is_file():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            f = dist / path
            if path and f.is_file() and dist.resolve() in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def root():
            return {"app": "Reckitt Retailer Onboarding POC", "api_docs": "/docs",
                    "frontend": "not built: cd frontend && npm install && npm run build"}

    return app


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app

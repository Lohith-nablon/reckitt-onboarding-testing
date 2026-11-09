"""Shared services for all modules: config, storage, session store, LLM, catalogues, MDM cache,
parsed-file cache. Built once per process (the Databricks App) and passed to every module."""
from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from onboarding_poc.config import AppConfig
from onboarding_poc.core.contracts import Contracts, build_contracts, load_catalogue, load_contracts, load_markets
from onboarding_poc.core.ingest.reader import ParsedSheet, parse_file
from onboarding_poc.llm import LLM
from onboarding_poc.session import Session, SessionStore
from onboarding_poc.storage import build_storage


@dataclass
class MDMCache:
    """MDM is read from S3 and cached in memory (agreed for the POC). Revisit when the extracts
    approach ~1 GB: move to a DB lookup or retrieval index."""
    ctx: "Context"
    _frames: dict = field(default_factory=dict)
    _loaded_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def frames(self) -> dict[str, pd.DataFrame]:
        cfg = self.ctx.cfg
        with self._lock:
            if self._frames and time.time() - self._loaded_at < cfg.mdm.cache_ttl_s:
                return self._frames
            out, total = {}, 0
            for name, fname in (("erp_product", cfg.mdm.product_file), ("rb_customer", cfg.mdm.customer_file)):
                key = f"{cfg.paths.mdm}/{fname}"
                if not self.ctx.storage.exists(key):
                    logger.warning("MDM file missing: {}", key)
                    out[name] = pd.DataFrame()
                    continue
                data = self.ctx.storage.read_bytes(key)
                total += len(data)
                out[name] = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False).replace("", None)
            if total > cfg.mdm.size_warn_mb * 1024 * 1024:
                logger.warning("MDM is {:.0f} MB: above the in-memory limit agreed for the POC", total / 1e6)
            self._frames, self._loaded_at = out, time.time()
            self.size_bytes = total
            return out

    def info(self) -> dict:
        fr = self.frames()
        return {name: {"rows": len(df), "columns": list(df.columns)} for name, df in fr.items()} | \
               {"cached_at": self._loaded_at, "size_bytes": getattr(self, "size_bytes", 0)}

    def invalidate(self) -> None:
        with self._lock:
            self._frames = {}


class Context:
    def __init__(self, cfg: AppConfig, storage=None, llm: LLM | None = None):
        self.cfg = cfg
        self.storage = storage or build_storage(cfg)
        self.store = SessionStore(self.storage, cfg.paths.sessions)
        self.llm = llm or LLM(cfg.llm)
        self.catalogue = load_catalogue(cfg.core_config_dir)
        self.markets = load_markets(cfg.core_config_dir)
        self.default_contracts = load_contracts(cfg.core_config_dir)
        self.mdm = MDMCache(self)
        self._parsed: OrderedDict[str, list[ParsedSheet]] = OrderedDict()
        self._parsed_lock = threading.Lock()
        self._warehouse = None
        self.bronze_cache: dict = {}

    @property
    def warehouse(self):
        """Unity Catalog through the SQL warehouse (or the local DuckDB stand-in), opened on first use."""
        if self._warehouse is None:
            from onboarding_poc.warehouse import build_warehouse
            self._warehouse = build_warehouse(self.cfg)
        return self._warehouse

    @property
    def decided_by_ai(self) -> str:
        return f"ai:{self.llm.label}" if self.llm.available else "rules"

    def contracts(self, s: Session) -> Contracts:
        """Target tables for this session: the uploaded mapping file (target schema) when there
        is one, the built-in POS_SELLOUT stand-in otherwise. Masters always come from config."""
        if s.target.source == "uploaded" and s.target.tables:
            masters = {k: v.model_dump() for k, v in self.default_contracts.masters.items()}
            return build_contracts({k: dict(v) for k, v in s.target.tables.items()}, masters)
        return self.default_contracts

    def parsed(self, s: Session, file_id: str, header_rows: dict | None = None) -> list[ParsedSheet]:
        """Parsed sheets of a session file, cached in memory (LRU of 32). Rebuilt from the raw file
        in storage after a restart, so nothing but JSON state needs to survive."""
        f = s.file(file_id)
        ck = f"{s.session_id}/{file_id}/{sorted((header_rows or {}).items(), key=str)}"
        with self._parsed_lock:
            if ck in self._parsed:
                self._parsed.move_to_end(ck)
                return self._parsed[ck]
        data = self.storage.read_bytes(f.key)
        sheets = parse_file(data, f.name, self.catalogue, header_rows) if header_rows else \
            parse_file(data, f.name, self.catalogue)
        with self._parsed_lock:
            self._parsed[ck] = sheets
            while len(self._parsed) > 32:
                self._parsed.popitem(last=False)
        return sheets

    def frame(self, s: Session, file_id: str, sheet: str | None) -> pd.DataFrame:
        for ps in self.parsed(s, file_id):
            if ps.sheet == sheet:
                return ps.frame
        raise KeyError(f"sheet {sheet!r} not in file {file_id}")

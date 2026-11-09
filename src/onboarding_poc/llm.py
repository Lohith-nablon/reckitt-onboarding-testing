"""LLM client: model serving / AI Gateway through the OpenAI-compatible API.

provider  none          no LLM: every module falls back to deterministic rules
          openai        any OpenAI-compatible gateway (base_url + model)
          azure_openai  Azure OpenAI (base_url = resource endpoint, model = deployment)
          databricks    Databricks model serving / AI Gateway (base_url = https://<host>/serving-endpoints)

`structured()` returns a pydantic object or None; callers always have a rule-based fallback,
so an LLM outage slows review down but never stops a session. Every call is recorded by the
caller in the trace (prompt kind, model, ok/failed), never the data itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import TypeVar

from loguru import logger
from pydantic import BaseModel

from onboarding_poc.config import LLMSettings

T = TypeVar("T", bound=BaseModel)


class LLM:
    def __init__(self, settings: LLMSettings, cache=None):
        self.s = settings
        self.provider = settings.provider
        self._client = None
        self._cache: dict[str, str] = {}
        self.calls: list[dict] = []
        if self.provider == "none":
            return
        key = os.environ.get(settings.api_key_env, "")
        if not key and self.provider == "databricks":
            key = os.environ.get("DATABRICKS_TOKEN", "")
        if not key and self.provider == "databricks" and os.environ.get("DATABRICKS_CLIENT_ID") and settings.model:
            # inside a Databricks App: the SDK gives an OpenAI client authenticated as the app
            try:
                from databricks.sdk import WorkspaceClient
                self._client = WorkspaceClient().serving_endpoints.get_open_ai_client()
                return
            except Exception as exc:
                logger.warning("could not open the serving endpoint as the app: {}", exc)
        if not (settings.model and (settings.base_url or self.provider == "openai") and key):
            logger.warning("LLM provider {} is incomplete (base_url/model/{}); running without LLM",
                           self.provider, settings.api_key_env)
            return
        try:
            from openai import AzureOpenAI, OpenAI
        except ImportError:
            logger.warning("openai package missing; running without LLM")
            return
        if self.provider == "azure_openai":
            self._client = AzureOpenAI(azure_endpoint=settings.base_url, api_key=key, api_version=settings.api_version,
                                       timeout=settings.timeout_s, max_retries=settings.max_retries)
        else:
            self._client = OpenAI(base_url=settings.base_url or None, api_key=key, timeout=settings.timeout_s,
                                  max_retries=settings.max_retries)

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.s.model}" if self.available else "rules"

    def structured(self, prompt: str, schema: type[T], kind: str) -> T | None:
        if self._client is None:
            return None
        ck = hashlib.sha256((kind + schema.__name__ + prompt).encode()).hexdigest()
        if ck in self._cache:
            return schema.model_validate_json(self._cache[ck])
        system = ("You are a data onboarding assistant. Answer only with a JSON object that matches this JSON "
                  f"schema:\n{json.dumps(schema.model_json_schema())}")
        try:
            r = self._client.chat.completions.create(
                model=self.s.model, temperature=self.s.temperature,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            text = r.choices[0].message.content or "{}"
            result = schema.model_validate_json(text)
        except Exception as exc:  # network, quota, parsing
            logger.warning("LLM call failed ({}): {}", kind, exc)
            self.calls.append({"kind": kind, "ok": False, "error": str(exc)[:200]})
            return None
        self.calls.append({"kind": kind, "ok": True})
        self._cache[ck] = result.model_dump_json()
        return result

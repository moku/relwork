"""Async client for an OpenAI-compatible local LLM server (vLLM).

Structured output goes through ``response_format`` json_schema; vLLM silently
ignores ``extra_body.guided_json`` so it is never used here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

import httpx
from loguru import logger
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> None:
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class LLM:
    def __init__(self) -> None:
        _load_env_file()
        self.base_url = os.environ["RELWORK_LLM_BASE_URL"].rstrip("/")
        self.model = os.environ.get("RELWORK_LLM_MODEL", "local-llm")
        api_key = os.environ.get("RELWORK_LLM_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(10.0, read=3600.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def structured(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        thinking: bool = False,
        max_tokens: int = 16000,
        retries: int = 2,
    ) -> T:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                },
            },
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            usage = data.get("usage", {})
            logger.info(
                "llm {} prompt={} completion={} finish={}",
                schema.__name__,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                choice.get("finish_reason"),
            )
            try:
                return schema.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
                logger.warning("llm parse failed (attempt {}): {}", attempt + 1, exc)
        raise RuntimeError(f"LLM structured output failed: {last_error}")

"""The only place that talks to the model gateway.

Two rules the rest of the system depends on:

1. **Callers name an alias, never a model.** `config/models.yaml` maps the alias
   to a model id and its parameters. Swapping a model is a config edit, and the
   id that actually served the call is returned so it can be recorded in
   `recommendations.model` — provenance, not decoration.
2. **This layer raises; nodes degrade.** A node that cannot reach its model sets
   `degraded=True` and carries on with a hole in the evidence. It does not
   invent a fact and it does not kill the case. `completeness_check` decides
   what the hole is worth. That is why there is no "return a default answer"
   path here: silently substituting a plausible answer for a failed call is the
   one thing that would make the audit trail a lie.

The fleet this runs against is shared and genuinely flaky — several catalogued
model ids return 503 because nothing is deployed behind them. Hence the fallback
chain, and hence the long default timeout: a cold start on this gateway is
routinely 90-180s while a warm call is under a second.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

CONFIG = Path(os.environ.get("MODELS_CONFIG", "config/models.yaml"))
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))
RETRIES = int(os.environ.get("LLM_RETRIES", "2"))


class LLMUnavailable(RuntimeError):
    """Every alias in the chain failed. The caller degrades — it does not guess."""


# Model id substring -> pretraining lineage. Order matters: the Sahabat builds
# are fine-tunes, so "gemma3-27b-sahabat" is Google and "Llama-Sahabat" is Meta.
_LINEAGE = (
    ("gpt-oss", "openai"), ("openai/", "openai"),
    ("gemma", "google"), ("llama", "meta"),
    ("qwen", "alibaba"), ("kimi", "moonshot"),
)


def lineage(model_id: str) -> str:
    """Which family a model comes from — the unit the critic rule is written in.

    Compared on the ids that actually *served* the calls, not on config: the
    proposer's fallback chain reaches the critic's own model, so an outage of
    the proposer's primary would turn cross-family review into self-review with
    nothing in the configuration having changed.
    """
    m = (model_id or "").lower()
    return next((family for key, family in _LINEAGE if key in m), m)


@dataclass
class Completion:
    """One gateway reply, plus the provenance needed to reproduce it."""

    content: str
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""  # the id that actually served it, which may be a fallback
    latency_s: float = 0.0
    raw_message: dict = field(default_factory=dict)


def _load() -> dict:
    with CONFIG.open() as fh:
        return yaml.safe_load(fh)


class Gateway:
    def __init__(self, config: dict | None = None) -> None:
        cfg = config or _load()
        self.aliases: dict[str, dict] = cfg["aliases"]
        self.fallbacks: dict[str, list[str]] = cfg.get("fallbacks") or {}
        self.base = os.environ["LLM_BASE_URL"].rstrip("/")
        self.key = os.environ["LITELLM_API_KEY"]
        self._client = httpx.AsyncClient(timeout=TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    def model_for(self, alias: str) -> str:
        """The id an alias resolves to right now. Used to check invariants
        (e.g. that proposer and critic have not collapsed onto one model)."""
        return self.aliases[alias]["model"]

    def _chain(self, alias: str) -> list[tuple[str, dict]]:
        spec = self.aliases[alias]
        chain = [(spec["model"], spec.get("params") or {})]
        # Fallbacks inherit the alias's params: a fallback that silently ran at a
        # different temperature would not be the same experiment.
        chain += [(m, spec.get("params") or {}) for m in self.fallbacks.get(alias, [])]
        return chain

    async def chat(
        self,
        alias: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> Completion:
        last = "no attempt made"
        for model, params in self._chain(alias):
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                **params,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            for attempt in range(RETRIES + 1):
                t0 = time.time()
                try:
                    r = await self._client.post(
                        f"{self.base}/chat/completions",
                        headers={"Authorization": f"Bearer {self.key}"},
                        json=body,
                    )
                    if r.status_code == 200:
                        msg = r.json()["choices"][0]["message"]
                        return Completion(
                            content=msg.get("content") or "",
                            tool_calls=msg.get("tool_calls") or [],
                            model=model,
                            latency_s=round(time.time() - t0, 2),
                            raw_message=msg,
                        )
                    last = f"{model}: HTTP {r.status_code}"
                    # 429 is the gateway pacing us (L7): wait as long as it asks
                    # and try the same model again — moving down the chain would
                    # swap models for a reason that has nothing to do with them.
                    if r.status_code == 429 and attempt < RETRIES:
                        wait = r.headers.get("retry-after", "")  # seconds, or an HTTP date
                        await asyncio.sleep(float(wait) if wait.isdigit() else 2**attempt)
                        continue
                    # 5xx is the gateway or the deployment; retrying the same id
                    # can work. Any other 4xx is our request and will fail
                    # identically, so move to the next model rather than burning retries.
                    if r.status_code < 500:
                        break
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last = f"{model}: {type(e).__name__}"
                if attempt < RETRIES:
                    await asyncio.sleep(2**attempt)
        raise LLMUnavailable(f"alias {alias!r} exhausted its chain ({last})")

    async def embed(self, alias: str, texts: list[str]) -> list[list[float]]:
        spec = self.aliases[alias]
        try:
            r = await self._client.post(
                f"{self.base}/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                # encoding_format is not optional here: without it the gateway
                # answers 422, and the catalogue's own example asks for base64,
                # which the caller would then have to decode.
                json={"model": spec["model"], "input": texts, "encoding_format": "float"},
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # ⚠️ Found at L3. Without this a read timeout left the method as a
            # raw httpx error, so `search_policy`'s `except LLMUnavailable`
            # never caught it and the keyword fallback — written for exactly
            # this failure — never ran. The tool died instead, and policy
            # retrieval came back empty. One exception type is the contract this
            # module promises; anything else leaks transport into its callers.
            raise LLMUnavailable(f"embed {alias!r}: {type(e).__name__}") from e
        if r.status_code != 200:
            # Body included: an embedding request fails for reasons a status code
            # alone does not explain — a rejected field, a text over the model's
            # context, a batch too large.
            raise LLMUnavailable(f"embed {alias!r}: HTTP {r.status_code} {r.text[:200]}")
        return [d["embedding"] for d in r.json()["data"]]

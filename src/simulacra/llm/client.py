"""A lean Ollama client.

Covers the four capabilities notes.md calls for -- streaming chat, structured
outputs, thinking toggle, embeddings -- plus tool calling, over one shared
connection pool.

Design notes specific to this hardware:

* `think` defaults to False everywhere. Measured: qwen3.5:2b with thinking on
  consumed its entire 200-token budget reasoning and emitted no content at all.
  Thinking is a batch-time luxury, not a turn-loop one.
* Requests are serialized through a lock. Ollama will happily accept concurrent
  requests, but on CPU they contend for the same cores and every one of them
  gets slower. One at a time is strictly faster end to end.
* Every call records telemetry so `client.stats()` can answer "is this actually
  as fast as the plan assumed?" without a separate harness.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import ModelPolicy, Settings


class OllamaError(RuntimeError):
    pass


@dataclass
class CallRecord:
    """What one request actually cost."""

    model: str
    kind: str
    wall_s: float
    prompt_tokens: int = 0
    eval_tokens: int = 0
    eval_s: float = 0.0
    load_s: float = 0.0


@dataclass
class Telemetry:
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, r: CallRecord) -> None:
        self.calls.append(r)

    def summary(self) -> dict[str, Any]:
        if not self.calls:
            return {}
        by_kind: dict[str, list[CallRecord]] = {}
        for c in self.calls:
            by_kind.setdefault(c.kind, []).append(c)
        return {
            kind: {
                "n": len(rs),
                "wall_s_avg": round(sum(r.wall_s for r in rs) / len(rs), 2),
                "wall_s_max": round(max(r.wall_s for r in rs), 2),
                "tok_per_s": round(
                    sum(r.eval_tokens for r in rs) / max(sum(r.eval_s for r in rs), 1e-9), 1
                ),
            }
            for kind, rs in by_kind.items()
        }


Message = dict[str, Any]


class OllamaClient:
    def __init__(self, settings: Settings | None = None, *, host: str | None = None):
        self.settings = settings or Settings()
        self.host = (host or self.settings.host).rstrip("/")
        self._http = httpx.Client(base_url=self.host, timeout=self.settings.request_timeout)
        self._lock = threading.Lock()
        self.telemetry = Telemetry()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- probing -----------------------------------------------------------

    def available_models(self) -> list[str]:
        r = self._http.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def health(self) -> tuple[bool, str]:
        """Cheap preflight for startup: is the daemon up and are our models pulled?"""
        try:
            have = set(self.available_models())
        except httpx.HTTPError as e:
            return False, f"cannot reach Ollama at {self.host}: {e}"
        missing = [
            p.name
            for p in (self.settings.chat, self.settings.embed)
            # Ollama reports "name:latest" for bare names.
            if p.name not in have and f"{p.name}:latest" not in have
        ]
        if missing:
            return False, "missing models: " + ", ".join(f"ollama pull {m}" for m in missing)
        return True, "ok"

    def warm(self, policy: ModelPolicy) -> float:
        """Force a model resident. Call at startup so turn one isn't a cold load."""
        t0 = time.perf_counter()
        self._post("/api/chat", {
            "model": policy.name,
            "messages": [],
            "keep_alive": policy.keep_alive,
        })
        return time.perf_counter() - t0

    # -- core calls --------------------------------------------------------

    def _options(self, policy: ModelPolicy) -> dict[str, Any]:
        return {
            "num_ctx": policy.num_ctx,
            "num_predict": policy.num_predict,
            "temperature": policy.temperature,
        }

    def _body(self, policy: ModelPolicy, messages: Sequence[Message], **extra) -> dict[str, Any]:
        body = {
            "model": policy.name,
            "messages": list(messages),
            "think": policy.think,
            "keep_alive": policy.keep_alive,
            "options": self._options(policy),
        }
        body.update(extra)
        return body

    def _post(self, path: str, body: dict) -> dict:
        with self._lock:
            r = self._http.post(path, json=body)
        if r.status_code >= 400:
            raise OllamaError(f"{path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _log(self, kind: str, policy: ModelPolicy, data: dict, wall: float) -> None:
        self.telemetry.record(
            CallRecord(
                model=policy.name,
                kind=kind,
                wall_s=wall,
                prompt_tokens=data.get("prompt_eval_count", 0) or 0,
                eval_tokens=data.get("eval_count", 0) or 0,
                eval_s=(data.get("eval_duration", 0) or 0) / 1e9,
                load_s=(data.get("load_duration", 0) or 0) / 1e9,
            )
        )

    def complete(
        self, messages: Sequence[Message], policy: ModelPolicy, *, kind: str = "complete"
    ) -> str:
        """Blocking, unstreamed text. Use only where the caller can't stream."""
        t0 = time.perf_counter()
        data = self._post("/api/chat", self._body(policy, messages, stream=False))
        self._log(kind, policy, data, time.perf_counter() - t0)
        return data.get("message", {}).get("content", "")

    def stream(
        self, messages: Sequence[Message], policy: ModelPolicy, *, kind: str = "stream"
    ) -> Iterator[str]:
        """Yield content deltas as they arrive.

        This is the default for anything the player reads. At ~15 tok/s the model
        emits roughly 11 words/sec against a human reading speed of ~4, so
        streamed prose arrives faster than it can be read and the wait vanishes.
        """
        t0 = time.perf_counter()
        body = self._body(policy, messages, stream=True)
        final: dict = {}
        with self._lock:
            with self._http.stream("POST", "/api/chat", json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    raise OllamaError(f"/api/chat -> {r.status_code}: {r.text[:300]}")
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if piece := chunk.get("message", {}).get("content"):
                        yield piece
                    if chunk.get("done"):
                        final = chunk
        self._log(kind, policy, final, time.perf_counter() - t0)

    def structured(
        self,
        messages: Sequence[Message],
        schema: dict,
        policy: ModelPolicy,
        *,
        kind: str = "structured",
        retries: int = 1,
    ) -> dict:
        """Constrained JSON via Ollama's `format` grammar.

        The grammar guarantees well-formed JSON matching the schema, so failures
        here are semantic, not syntactic. We still retry once, because a
        truncated response (num_predict exhausted) yields invalid JSON.
        """
        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.perf_counter()
            data = self._post(
                "/api/chat", self._body(policy, messages, stream=False, format=schema)
            )
            self._log(kind, policy, data, time.perf_counter() - t0)
            raw = data.get("message", {}).get("content", "")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                last = e
                # Almost always truncation; buy headroom for the retry.
                policy = policy.with_(num_predict=int(policy.num_predict * 1.5))
        raise OllamaError(f"structured output did not parse after {retries + 1} tries: {last}")

    def embed(self, texts: Sequence[str], policy: ModelPolicy | None = None) -> list[list[float]]:
        policy = policy or self.settings.embed
        t0 = time.perf_counter()
        data = self._post("/api/embed", {
            "model": policy.name,
            "input": list(texts),
            "keep_alive": policy.keep_alive,
        })
        self._log("embed", policy, data, time.perf_counter() - t0)
        return data.get("embeddings", [])

    def chat_with_tools(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict],
        policy: ModelPolicy,
        *,
        kind: str = "tools",
    ) -> dict:
        """One tool-calling round trip. Returns the raw assistant message.

        Held in reserve rather than used in the turn loop: on a 1.7b model a tool
        decision costs about what a structured call costs but is far less
        reliable, so structured outputs carry the POC. See plan.md, "Tool calling".
        """
        t0 = time.perf_counter()
        data = self._post("/api/chat", self._body(policy, messages, stream=False, tools=list(tools)))
        self._log(kind, policy, data, time.perf_counter() - t0)
        return data.get("message", {})

    def stats(self) -> dict[str, Any]:
        return self.telemetry.summary()

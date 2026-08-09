"""Shared fixtures. Nothing here touches a model."""

from __future__ import annotations

import threading

import pytest

from simulacra.world.theme import Theme


class FakeClient:
    """Stands in for OllamaClient.

    `script` is a list of responses handed out in order; the last one repeats.
    Streaming is character-chunked so the narrator's prefix guard sees a real
    stream rather than one lump.
    """

    def __init__(self, script=None, structured_result=None, delay: float = 0.0):
        self.script = list(script or ["A room of wet stone. Something has been dragged through it."])
        self.structured_result = structured_result
        self.delay = delay
        self.calls: list[str] = []
        self.stream_calls = 0
        self.closed_early = 0
        self._lock = threading.Lock()

    def _next(self) -> str:
        with self._lock:
            return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def stream(self, messages, policy, *, kind="stream"):
        self.calls.append(kind)
        self.stream_calls += 1
        text = self._next()
        delivered = 0
        try:
            for i in range(0, len(text), 8):
                if self.delay:
                    threading.Event().wait(self.delay)
                delivered += 1
                yield text[i : i + 8]
        except GeneratorExit:
            # Records that the consumer abandoned us mid-generation.
            self.closed_early += 1
            raise

    def complete(self, messages, policy, *, kind="complete"):
        self.calls.append(kind)
        return self._next()

    def structured(self, messages, schema, policy, *, kind="structured", retries=1):
        self.calls.append(kind)
        if isinstance(self.structured_result, Exception):
            raise self.structured_result
        return self.structured_result

    def embed(self, texts, policy=None):
        return [[0.0] * 768 for _ in texts]

    def health(self):
        return True, "ok"

    def warm(self, policy):
        return 0.0

    def close(self):
        pass


@pytest.fixture(scope="session")
def theme() -> Theme:
    return Theme.load("simulacra")


@pytest.fixture
def fake_client():
    return FakeClient()

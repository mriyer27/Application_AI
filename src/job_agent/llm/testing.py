"""In-process fake Ollama server for tests (no network).

Usage::

    fake = FakeOllama(models=["qwen2.5:7b-instruct"])
    fake.queue({"summary": "...", ...})          # next /api/chat reply (dict -> JSON content)
    client = fake.client(config.llm)             # OllamaClient on an httpx.MockTransport
    ...
    fake.chat_requests[0]["messages"]            # what the client sent

Replies may be a ``dict`` / ``BaseModel`` (JSON-encoded as the message content),
a ``str`` (raw content), an ``httpx.Response`` (returned as is) or an
exception instance (raised by the transport, e.g. ``httpx.ReadTimeout``).
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from typing import Any

import httpx
from pydantic import BaseModel

from job_agent.config import LLMSection
from job_agent.llm.client import OllamaClient


class FakeOllama:
    def __init__(self, models: Iterable[str] = ("qwen2.5:7b-instruct",), *, version: str = "0.12.6",
                 down: bool = False) -> None:
        self.models = list(models)
        self.version = version
        self.down = down
        self.replies: deque[Any] = deque()
        self.chat_requests: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []

    # -- configuration ----------------------------------------------------- #
    def queue(self, *replies: Any) -> "FakeOllama":
        self.replies.extend(replies)
        return self

    @property
    def chat_calls(self) -> int:
        return len(self.chat_requests)

    # -- transport --------------------------------------------------------- #
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [
                {"name": m, "model": m, "size": 1_000_000_000, "details": {"parameter_size": "7B",
                                                                         "quantization_level": "Q4_K_M"}}
                for m in self.models]})
        if path == "/api/generate":
            return httpx.Response(200, json={"done": True, "done_reason": "unload"})
        if path == "/api/chat":
            body = json.loads(request.content.decode("utf-8"))
            self.chat_requests.append(body)
            if body.get("model") not in self.models:
                return httpx.Response(404, json={"error": f"model '{body.get('model')}' not found"})
            if not self.replies:
                raise AssertionError("FakeOllama: no chat reply queued")
            reply = self.replies.popleft()
            if isinstance(reply, BaseException):
                if isinstance(reply, httpx.RequestError):
                    reply.request = request
                raise reply
            if isinstance(reply, httpx.Response):
                return reply
            if isinstance(reply, BaseModel):
                content = reply.model_dump_json()
            elif isinstance(reply, (dict, list)):
                content = json.dumps(reply)
            else:
                content = str(reply)
            return httpx.Response(200, json=chat_envelope(body["model"], content))
        return httpx.Response(404, json={"error": "not found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def client(self, llm_config: LLMSection, **kwargs: Any) -> OllamaClient:
        kwargs.setdefault("backoff_seconds", 0)
        return OllamaClient(llm_config, self.http_client(), **kwargs)


def chat_envelope(model: str, content: str, *, prompt_tokens: int = 900, eval_tokens: int = 300) -> dict[str, Any]:
    """A non-streaming ``/api/chat`` response body with realistic timing fields (nanoseconds)."""
    return {
        "model": model,
        "created_at": "2026-10-12T00:00:00Z",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 12_000_000_000,
        "load_duration": 2_000_000_000,
        "prompt_eval_count": prompt_tokens,
        "prompt_eval_duration": 6_000_000_000,
        "eval_count": eval_tokens,
        "eval_duration": 4_000_000_000,
    }

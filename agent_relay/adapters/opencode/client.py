"""Thin HTTP + SSE client for `opencode serve`. Endpoint variants are picked from `GET /doc` (spike S9)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

import httpx
from pydantic import JsonValue

from agent_relay.adapters.base import Decision, ModelInfo

PERMISSION_REPLY: dict[Decision, str] = {"allow": "once", "allow_session": "always", "deny": "reject"}


class PermissionVariant(StrEnum):
    CURRENT = "current"
    LEGACY = "legacy"


class OpenCodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServerEvent:
    type: str
    properties: dict[str, JsonValue]
    directory: str | None


def model_ref(model: str) -> dict[str, JsonValue]:
    provider, _, model_id = model.partition("/")
    if not model_id:
        raise OpenCodeError(f"model `{model}` must look like provider/model")
    return {"providerID": provider, "modelID": model_id}


def parse_providers(body: JsonValue) -> list[ModelInfo]:
    if not isinstance(body, dict):
        return []
    defaults = body.get("default") if isinstance(body.get("default"), dict) else {}
    assert isinstance(defaults, dict)
    found: list[ModelInfo] = []
    providers = body.get("providers")
    for provider in providers if isinstance(providers, list) else []:
        if not isinstance(provider, dict) or not isinstance(provider.get("id"), str):
            continue
        pid = str(provider["id"])
        pname = str(provider.get("name") or pid)
        models = provider.get("models")
        for mid, info in (models if isinstance(models, dict) else {}).items():
            label = info.get("name") if isinstance(info, dict) else None
            found.append(
                ModelInfo(
                    id=f"{pid}/{mid}", label=f"{pname}: {label or mid}", default=defaults.get(pid) == mid
                )
            )
    return sorted(found, key=lambda m: (not m.default, m.id))


def parse_sse(lines: list[str]) -> ServerEvent | None:
    data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
    if not data:
        return None
    try:
        envelope = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("payload", envelope)
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        return None
    properties = payload.get("properties")
    directory = envelope.get("directory")
    return ServerEvent(
        type=str(payload["type"]),
        properties=properties if isinstance(properties, dict) else {},
        directory=directory if isinstance(directory, str) else None,
    )


class OpenCodeClient:
    """One `opencode serve` instance. `auth` is basic auth (user `opencode`) for the managed server."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, auth: httpx.Auth | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._auth = auth
        self._variant: PermissionVariant | None = None

    async def _call(
        self,
        method: str,
        route: str,
        *,
        directory: str | None = None,
        body: JsonValue = None,
        timeout: float = 15.0,
    ) -> JsonValue:
        params = {"directory": directory} if directory else None
        response = await self._http.request(
            method,
            f"{self.base_url}{route}",
            params=params,
            json=body,
            auth=self._auth or httpx.USE_CLIENT_DEFAULT,
            timeout=timeout,
        )
        if response.status_code >= 400:
            raise OpenCodeError(f"{method} {route} → HTTP {response.status_code}: {response.text[:200]}")
        if not response.content:
            return None
        try:
            parsed: JsonValue = response.json()
        except json.JSONDecodeError:
            return response.text
        return parsed

    async def _read_doc(self) -> PermissionVariant:
        doc = await self._call("GET", "/doc")
        routes = doc.get("paths") if isinstance(doc, dict) else None
        current = isinstance(routes, dict) and "/permission/{requestID}/reply" in routes
        self._variant = PermissionVariant.CURRENT if current else PermissionVariant.LEGACY
        return self._variant

    async def variant(self) -> PermissionVariant:
        return self._variant or await self._read_doc()

    async def healthy(self) -> bool:
        """Probes the server every time; also refreshes the endpoint variant after an upgrade."""
        try:
            await self._read_doc()
        except (httpx.HTTPError, OpenCodeError):
            return False
        return True

    async def models(self) -> list[ModelInfo]:
        return parse_providers(await self._call("GET", "/config/providers"))

    async def create_session(self, directory: str, title: str) -> str:
        created = await self._call("POST", "/session", directory=directory, body={"title": title})
        if not isinstance(created, dict) or not isinstance(created.get("id"), str):
            raise OpenCodeError("POST /session returned no id")
        return str(created["id"])

    async def prompt(self, session_id: str, directory: str | None, text: str, model: str | None) -> None:
        body: dict[str, JsonValue] = {"parts": [{"type": "text", "text": text}]}
        if model:
            body["model"] = model_ref(model)
        await self._call("POST", f"/session/{session_id}/prompt_async", directory=directory, body=body)

    async def abort(self, session_id: str, directory: str | None) -> None:
        await self._call("POST", f"/session/{session_id}/abort", directory=directory)

    async def reply_permission(
        self, session_id: str, request_id: str, decision: Decision, directory: str | None
    ) -> None:
        reply = PERMISSION_REPLY[decision]
        if await self.variant() is PermissionVariant.CURRENT:
            await self._call(
                "POST", f"/permission/{request_id}/reply", directory=directory, body={"reply": reply}
            )
        else:
            await self._call(
                "POST",
                f"/session/{session_id}/permissions/{request_id}",
                directory=directory,
                body={"response": reply},
            )

    async def reply_question(
        self, request_id: str, answers: list[list[str]] | None, directory: str | None
    ) -> None:
        if answers is None:
            await self._call("POST", f"/question/{request_id}/reject", directory=directory)
            return
        body: dict[str, JsonValue] = {"answers": [list(a) for a in answers]}
        await self._call("POST", f"/question/{request_id}/reply", directory=directory, body=body)

    async def events(self) -> AsyncIterator[ServerEvent]:
        """`GET /global/event` as parsed events; returns when the server closes the stream."""
        async with self._http.stream(
            "GET",
            f"{self.base_url}/global/event",
            auth=self._auth or httpx.USE_CLIENT_DEFAULT,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(10.0, read=None),
        ) as response:
            if response.status_code >= 400:
                raise OpenCodeError(f"GET /global/event → HTTP {response.status_code}")
            block: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    block.append(line)
                    continue
                event = parse_sse(block)
                block = []
                if event is not None:
                    yield event
            event = parse_sse(block)
            if event is not None:
                yield event

"""1.8-17 - the HTTP request guard and the token verifier (spike S3).

Driven in-process through the real app ``mcp.http_app()`` builds, with httpx's
ASGITransport: the guard is the one ``server.py`` prepends by wrapping
``mcp.http_app`` on the instance, and fastmcp's own Host/Origin guard is left
at its default (off). stdio never builds this app, so it is unaffected.

A browser-based MCP inspector sends an ``Origin`` header and is refused 403
until its origin is listed in ``HTTP_ALLOWED_ORIGINS`` - by design: a loopback
server reachable from any web page is the DNS-rebinding hole this closes.
"""

from __future__ import annotations

import contextlib
import hmac

import httpx
import pytest

import config
import http_guard
import server

BASE = "http://127.0.0.1:8000"
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
RW, RO = "rw-token-0123456789", "ro-token-0123456789"


def _call(name: str, arguments: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


@contextlib.asynccontextmanager
async def _served(monkeypatch):
    """An httpx client on the guarded app, both tokens configured, bound to 127.0.0.1:8000.

    A context manager entered in the test body, not a fixture: the app's
    lifespan opens a task group that must close in the task that opened it.
    """
    auth = http_guard.build_auth(RW, RO)
    monkeypatch.setattr(server.mcp, "auth", auth)
    monkeypatch.setattr(server, "_auth", auth)
    monkeypatch.setattr(http_guard, "_bound", {"host": "127.0.0.1", "port": 8000})
    app = server.mcp.http_app(stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            yield client


def _bearer(token: str) -> dict:
    return {**HEADERS, "authorization": f"Bearer {token}"}


def test_every_app_builder_gets_the_guard_first():
    for transport in ("http", "sse"):
        app = server.mcp.http_app(transport=transport)
        names = [m.cls.__name__ for m in app.user_middleware]
        assert "HttpGuard" in names, transport
    assert server.mcp.http_app is server._guarded_http_app


@pytest.mark.asyncio
async def test_an_evil_origin_is_refused_403_whatever_the_token(monkeypatch):
    async with _served(monkeypatch) as http:
        await _test_an_evil_origin_is_refused_403_whatever_the_token(http)


async def _test_an_evil_origin_is_refused_403_whatever_the_token(http):
    for token in (RW, "wrong"):
        response = await http.post(
            "/mcp",
            json=_call("system_status"),
            headers={**_bearer(token), "origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "origin_not_allowed"


@pytest.mark.asyncio
async def test_a_listed_origin_passes(monkeypatch):
    async with _served(monkeypatch) as http:
        await _test_a_listed_origin_passes(http, monkeypatch)


async def _test_a_listed_origin_passes(http, monkeypatch):
    monkeypatch.setattr(config.settings, "http_allowed_origins", ("http://localhost:6274",))
    response = await http.post(
        "/mcp",
        json=_call("system_status"),
        headers={**_bearer(RW), "origin": "http://localhost:6274/"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_a_foreign_host_is_refused_421(monkeypatch):
    async with _served(monkeypatch) as http:
        await _test_a_foreign_host_is_refused_421(http)


async def _test_a_foreign_host_is_refused_421(http):
    response = await http.post(
        "/mcp",
        json=_call("system_status"),
        headers={**_bearer(RW), "host": "evil.example:8000"},
    )
    assert response.status_code == 421
    wrong_port = await http.post(
        "/mcp", json=_call("system_status"), headers={**_bearer(RW), "host": "127.0.0.1:9999"}
    )
    assert wrong_port.status_code == 421


@pytest.mark.asyncio
async def test_a_9_mib_body_is_refused_413_by_length_and_chunked(monkeypatch):
    async with _served(monkeypatch) as http:
        await _test_a_9_mib_body_is_refused_413_by_length_and_chunked(http)


async def _test_a_9_mib_body_is_refused_413_by_length_and_chunked(http):
    big = b"x" * (9 * 1024 * 1024)
    response = await http.post("/mcp", content=big, headers=_bearer(RW))
    assert response.status_code == 413

    async def chunks():
        for _ in range(9):
            yield b"x" * (1024 * 1024)

    chunked = await http.post("/mcp", content=chunks(), headers=_bearer(RW))
    assert chunked.request.headers.get("transfer-encoding") == "chunked"
    assert chunked.status_code == 413


@pytest.mark.asyncio
async def test_no_token_and_a_wrong_token_are_401(monkeypatch):
    async with _served(monkeypatch) as http:
        await _test_no_token_and_a_wrong_token_are_401(http)


async def _test_no_token_and_a_wrong_token_are_401(http):
    assert (
        await http.post("/mcp", json=_call("system_status"), headers=HEADERS)
    ).status_code == 401
    wrong = await http.post("/mcp", json=_call("system_status"), headers=_bearer("nope"))
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_the_read_only_token_is_held_to_read(monkeypatch):
    async with _served(monkeypatch) as http:
        await _test_the_read_only_token_is_held_to_read(http)


async def _test_the_read_only_token_is_held_to_read(http):
    made = await http.post("/mcp", json=_call("drawing_new"), headers=_bearer(RW))
    assert made.status_code == 200 and not made.json()["result"].get("isError")

    create = await http.post(
        "/mcp",
        json=_call("entity_create_line", {"x1": 0, "y1": 0, "x2": 1, "y2": 1}),
        headers=_bearer(RO),
    )
    assert create.status_code == 200
    result = create.json()["result"]
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert payload["kind"] == "policy_refused"
    assert payload["policy"] == "token_scope" and payload["allowed_cost"] == "read"

    info = await http.post("/mcp", json=_call("drawing_info"), headers=_bearer(RO))
    assert info.status_code == 200
    assert not info.json()["result"].get("isError")

    listed = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=_bearer(RO),
    )
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert "drawing_info" in names and "entity_create_line" not in names

    full = await http.post(
        "/mcp",
        json=_call("entity_create_line", {"x1": 0, "y1": 0, "x2": 1, "y2": 1}),
        headers=_bearer(RW),
    )
    assert not full.json()["result"].get("isError")


@pytest.mark.asyncio
async def test_the_verifier_compares_every_token_in_constant_time(monkeypatch):
    verifier = http_guard.build_auth(RW, RO)
    seen = []
    original = hmac.compare_digest

    def spy(a, b):
        seen.append(len(a))
        return original(a, b)

    monkeypatch.setattr(http_guard.hmac, "compare_digest", spy)
    token = await verifier.verify_token(RO)
    assert token is not None and token.scopes == ["mcp:read"]
    assert len(seen) == 2, "every configured token is compared, not just the match"
    assert await verifier.verify_token("rw-token-012345678X") is None
    assert (await verifier.verify_token(RW)).scopes == ["mcp:read", "mcp:write"]


def test_the_read_only_token_alone_enables_auth():
    verifier = http_guard.build_auth("", RO)
    assert isinstance(verifier, http_guard.ConstantTimeTokenVerifier)
    assert list(verifier.tokens) == [RO]
    assert http_guard.build_auth("", "") is None


def test_a_non_loopback_bind_requires_allowed_paths(monkeypatch):
    monkeypatch.setattr(config.settings, "allow_remote_http", True)
    monkeypatch.setattr(config.settings, "mcp_auth_token", RW)
    monkeypatch.setattr(config.settings, "allowed_paths", [])
    with pytest.raises(SystemExit, match="ALLOWED_PATHS"):
        server._validate_http_bind("0.0.0.0")


@pytest.mark.parametrize(
    ("header", "bind", "expected"),
    [
        ("127.0.0.1:8000", ("127.0.0.1", 8000), True),
        ("localhost:8000", ("127.0.0.1", 8000), True),
        ("[::1]:8000", ("127.0.0.1", 8000), True),
        ("localhost:8001", ("127.0.0.1", 8000), False),
        ("evil.example:8000", ("127.0.0.1", 8000), False),
        ("192.168.1.5:8000", ("192.168.1.5", 8000), True),
        ("cad.example:8000", ("0.0.0.0", 8000), False),
        (None, ("127.0.0.1", 8000), False),
    ],
)
def test_host_rules(header, bind, expected):
    assert http_guard.host_allowed(header, bind=bind) is expected


def test_allowed_hosts_extend_the_bind(monkeypatch):
    monkeypatch.setattr(config.settings, "http_allowed_hosts", ("cad.example",))
    assert http_guard.host_allowed("cad.example:8000", bind=("0.0.0.0", 8000))
    assert http_guard.host_allowed("cad.example", bind=("0.0.0.0", 8000))


def test_guarded_run_records_the_bind(monkeypatch):
    """The Host check compares against the address mcp.run_async was given."""
    captured = {}

    async def fake_run(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server, "_orig_run_async", fake_run)
    monkeypatch.setattr(http_guard, "_bound", None)
    import anyio

    anyio.run(lambda: server._guarded_run_async(transport="http", host="127.0.0.1", port=8123))
    assert http_guard.bound() == ("127.0.0.1", 8123)

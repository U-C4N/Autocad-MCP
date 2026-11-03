"""The HTTP transport's request guard and token verifier (1.8-17, spike S3).

``HttpGuard`` is a pure ASGI middleware (not ``BaseHTTPMiddleware``: that one
buffers streamed responses and breaks the event stream MCP answers on). The
server installs it by wrapping ``mcp.http_app`` on the instance, so every
builder - ``main()``, ``mcp.run``, ``fastmcp run``, a hand-made
``mcp.http_app()`` - gets it, the SSE app included. fastmcp's own Host/Origin
guard is off by default and absent from SSE (S3), so it is not relied on.

Per request, before any MCP handler runs:

* an ``Origin`` header, when present, must be one of ``HTTP_ALLOWED_ORIGINS``
  (default: none, so every browser origin is refused **403**). A browser-based
  MCP inspector sends an Origin and is refused until its origin is listed - the
  DNS-rebinding defence a loopback server needs;
* the ``Host`` header must be the bound ``host:port`` (any loopback name with
  the bound port) or one of ``HTTP_ALLOWED_HOSTS`` - else **421**;
* a body larger than ``MAX_HTTP_BODY_BYTES`` (8 MiB; 0 disables) is refused
  **413**: on its ``Content-Length`` before a byte is read, and - for a chunked
  body with no length - counted as it arrives (buffered up to the limit, then
  replayed to the app).

fastmcp appends user middleware after its authentication middleware (S3), so a
403/421/413 never reveals whether a token was valid.

``ConstantTimeTokenVerifier`` replaces ``StaticTokenVerifier``'s dictionary
lookup with ``hmac.compare_digest`` over every configured token.
``MCP_AUTH_TOKEN`` carries ``mcp:read`` and ``mcp:write``;
``MCP_AUTH_TOKEN_READONLY`` only ``mcp:read`` - the server then holds that
request to the ``read`` ceiling (``policy.REQUEST_CEILING``).
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import Callable

from fastmcp.server.auth import AccessToken, StaticTokenVerifier

import config

log = logging.getLogger("autocad_mcp.http_guard")

#: The scope a write needs; a token without it runs under the ``read`` ceiling.
WRITE_SCOPE = "mcp:write"
READ_SCOPE = "mcp:read"

LOOPBACK_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_WILDCARD_BINDS: frozenset[str] = frozenset({"0.0.0.0", "::", ""})

#: The (host, port) the server was started on, set by ``server._guarded_run_async``;
#: None when the app was built without it (then: loopback, fastmcp's port).
_bound: dict | None = None


def set_bind(host: str | None, port: int | None) -> None:
    global _bound
    _bound = {"host": str(host or "127.0.0.1"), "port": int(port) if port else None}


def bound() -> tuple[str, int | None]:
    if _bound is not None:
        return _bound["host"], _bound["port"]
    try:
        import fastmcp

        port = getattr(fastmcp.settings, "port", None)
    except Exception:  # pragma: no cover - defensive
        port = None
    return "127.0.0.1", int(port) if port else None


def _split_host(value: str) -> tuple[str, int | None]:
    """``"Example.com:8000"`` -> ``("example.com", 8000)``; ``"[::1]:80"`` -> ``("::1", 80)``."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        name, rest = value[1:end], value[end + 1 :]
        port = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        name, port = value.split(":", 1)
    else:
        name, port = value, ""
    try:
        return name, int(port) if port else None
    except ValueError:
        return name, -1  # a malformed port matches nothing


def host_allowed(header: str | None, *, bind: tuple[str, int | None] | None = None) -> bool:
    """True when ``header`` names this server: the bound host:port or an allowed host."""
    if not header:
        return False
    name, port = _split_host(header)
    allowed = tuple(getattr(config.settings, "http_allowed_hosts", ()) or ())
    for entry in allowed:
        if entry == "*":
            return True
        entry_name, entry_port = _split_host(entry)
        if entry_name == name and (entry_port is None or entry_port == port):
            return True
    bound_host, bound_port = bind or bound()
    bound_host = bound_host.strip("[]").lower()

    def port_ok() -> bool:
        return bound_port is None or port == bound_port or (port is None and bound_port == 80)

    if name in LOOPBACK_NAMES and port_ok():
        # A loopback name is never an attacker's DNS name, whatever the bind.
        return True
    if bound_host not in _WILDCARD_BINDS and bound_host not in LOOPBACK_NAMES:
        return name == bound_host and port_ok()
    return False


def origin_allowed(origin: str) -> bool:
    allowed = tuple(getattr(config.settings, "http_allowed_origins", ()) or ())
    normalised = origin.strip().rstrip("/").lower()
    return "*" in allowed or normalised in allowed


class HttpGuard:
    """Pure ASGI request guard: Origin 403, Host 421, body 413 (see the module doc)."""

    def __init__(self, app, bind: Callable[[], tuple[str, int | None]] | None = None):
        self.app = app
        self._bind = bind or bound

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        for raw_name, raw_value in scope.get("headers") or ():
            name = raw_name.decode("latin-1").lower()
            # A repeated Origin/Host is suspicious; the first one is checked and a
            # differing second one refused below.
            value = raw_value.decode("latin-1")
            if name in headers and name in ("origin", "host") and headers[name] != value:
                await _refuse(send, 400, "duplicate_header", f"two different {name} headers")
                return
            headers.setdefault(name, value)
        origin = headers.get("origin")
        if origin is not None and not origin_allowed(origin):
            log.warning("HTTP refused 403: Origin %r is not in HTTP_ALLOWED_ORIGINS", origin)
            await _refuse(
                send,
                403,
                "origin_not_allowed",
                f"Origin {origin!r} is not in HTTP_ALLOWED_ORIGINS; a browser page may not "
                "call this server. List the origin to allow it.",
            )
            return
        if not host_allowed(headers.get("host"), bind=self._bind()):
            log.warning("HTTP refused 421: Host %r is not this server", headers.get("host"))
            await _refuse(
                send,
                421,
                "host_not_allowed",
                f"Host {headers.get('host')!r} is not the address this server is bound to; "
                "add it to HTTP_ALLOWED_HOSTS to serve it.",
            )
            return
        limit = int(getattr(config.settings, "max_http_body_bytes", 0) or 0)
        if limit <= 0:
            await self.app(scope, receive, send)
            return
        length = headers.get("content-length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                await _refuse(send, 400, "bad_content_length", "Content-Length is not a number")
                return
            if declared > limit:
                await _refuse_too_large(send, declared, limit)
                return
            await self.app(scope, _counting(receive, limit), send)
            return
        # No length (chunked): read up to the limit, then replay the body.
        chunks: list[bytes] = []
        total = 0
        more = True
        disconnect = None
        while more:
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnect = message
                break
            body = message.get("body", b"") or b""
            total += len(body)
            if total > limit:
                await _refuse_too_large(send, total, limit)
                return
            chunks.append(body)
            more = bool(message.get("more_body"))
        await self.app(scope, _replay(b"".join(chunks), receive, disconnect), send)


def _counting(receive, limit: int):
    """``receive`` that stops a body exceeding its declared length past ``limit``."""
    seen = 0

    async def wrapped():
        nonlocal seen
        message = await receive()
        if message.get("type") == "http.request":
            seen += len(message.get("body", b"") or b"")
            if seen > limit:
                return {"type": "http.disconnect"}
        return message

    return wrapped


def _replay(body: bytes, receive, disconnect):
    sent = False

    async def wrapped():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect is not None:
            return disconnect
        return await receive()

    return wrapped


async def _refuse_too_large(send, size: int, limit: int) -> None:
    log.warning("HTTP refused 413: body of %d bytes over MAX_HTTP_BODY_BYTES=%d", size, limit)
    await _refuse(
        send,
        413,
        "body_too_large",
        f"request body of at least {size} bytes is over MAX_HTTP_BODY_BYTES={limit}",
    )


async def _refuse(send, status: int, error: str, message: str) -> None:
    body = json.dumps({"ok": False, "error": error, "message": message}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ConstantTimeTokenVerifier(StaticTokenVerifier):
    """``StaticTokenVerifier`` whose lookup is ``hmac.compare_digest`` over every token.

    The parent compares with ``dict.get`` (S3: ``jwt.py:653``), whose timing
    depends on the guess; this one compares the presented token against every
    configured token, always all of them.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = (token or "").encode("utf-8")
        match = None
        for candidate, data in self.tokens.items():
            if hmac.compare_digest(candidate.encode("utf-8"), presented) and match is None:
                match = (candidate, data)
        if match is None:
            return None
        candidate, data = match
        expires_at = data.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            return None
        scopes = list(data.get("scopes", []))
        if self.required_scopes and not set(self.required_scopes).issubset(scopes):
            return None
        return AccessToken(
            token=candidate,
            client_id=data["client_id"],
            scopes=scopes,
            expires_at=expires_at,
            claims=data,
        )


def build_auth(token: str | None, readonly_token: str | None):
    """The verifier for ``MCP_AUTH_TOKEN`` / ``MCP_AUTH_TOKEN_READONLY``, or None for neither."""
    tokens: dict[str, dict] = {}
    if token:
        tokens[token] = {"client_id": "mcp-http-client", "scopes": [READ_SCOPE, WRITE_SCOPE]}
    if readonly_token:
        if readonly_token == token:
            log.warning(
                "MCP_AUTH_TOKEN_READONLY equals MCP_AUTH_TOKEN; that token is read-only now."
            )
        tokens[readonly_token] = {"client_id": "mcp-http-readonly", "scopes": [READ_SCOPE]}
    return ConstantTimeTokenVerifier(tokens=tokens) if tokens else None


def token_ceiling() -> str | None:
    """``"read"`` when the request's access token lacks ``mcp:write``; None otherwise.

    None as well for stdio and for an unauthenticated (loopback) HTTP server:
    there is no token to hold the request to.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # no request context: stdio, a direct call
        return None
    if token is None:
        return None
    scopes = set(getattr(token, "scopes", None) or ())
    return None if WRITE_SCOPE in scopes else "read"

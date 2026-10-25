"""The live COM release harness (spec §6 Task 21, B63).

Every ``scripts/smoke_*_com.py`` runs on this. It drives ``ComBackend`` against
an AutoCAD that is already running, on documents it creates itself, records a
pass / fail per check and writes one machine-readable JSON result.

**Attach only.** The application is reached with ``GetActiveObject`` and
nothing else. ``ComBackend._acad_app`` falls back to ``Dispatch`` - which
launches AutoCAD - when it has no application cached, and drops the cached one
after a deadline overrun; so every job this harness runs (the backend's own
included) first puts the attached application back into the backend's
``_COM_STATE``, attaching again with ``GetActiveObject`` when it was dropped. A
seat that is not running is ``unavailable`` (exit 2), never launched. No COM
event sink is ever attached: on 2026-10-24 one left the seat refusing
``Documents.Add()`` until AutoCAD restarted (backends/document_guard.py).

**Its own documents, closed on every path.** ``new_document`` /
``open_document`` go through the backend (the code under test) and then hold
the ``Document`` object AutoCAD added - one that was not open before the call.
On the way out, also when a check raised, each is closed with ``Close(False)``
through that object (a ``SaveAs`` that renamed it does not lose it), unless
``keep`` is set; the JSON lists any it could not close. A document the harness
did not open is never activated, written, saved or closed.

**The active-document guard.** From its first document on, every job checks -
inside the same COM-thread job, before the call runs - that AutoCAD's active
document is one of the harness's own open documents, and refuses otherwise
(``ActiveDocumentChanged``, exit 2, ``status: "refused"``). An operator who
clicks their own drawing tab mid-run, or a run whose last document is closed,
therefore stops the run before anything reaches a drawing that is not its own.
The refusal never names the other document. ``ComBackend``'s own bound-document
guard (B11) is the product's; this one is the harness's.

**One shared RPC_E_CALL_REJECTED retry.** AutoCAD's message filter refuses a
call while it is busy (right after ``Documents.Add`` / ``Open`` / ``Close``,
measured on this seat); a refused call did not run. Every job is retried with
backoff (0.5 s doubling to 5 s) for up to ``REJECTED_PATIENCE_S`` and the
retries are counted in the JSON. A job of several COM calls whose later call
was refused is run again whole, which a smoke's checks then see.

**The JSON** (``SCHEMA``): ``status`` (``pass`` / ``fail`` / ``refused`` /
``unavailable``), ``exit_code``, ``checks`` (name, status, expected, observed,
stage; ``error`` for a check that raised), ``summary``, ``documents``, the
smoke's own ``report`` and ``environment``: the AutoCAD name, version and
build (``Application.Version`` - without the licence text AutoCAD appends in
parentheses - and ``_VERNUM`` read on the harness's own document), the OS,
Python, pywin32 and the server's version, commit and whether the checkout was
dirty.

Exit codes:

    0  every check passed (and at least one ran)
    1  a check failed or raised, or a document could not be closed
    2  no running AutoCAD to attach to (``unavailable``), or the active
       document changed under the run (``refused``)
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import platform
import re
import subprocess
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "acadmcp-live-harness/1"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNAVAILABLE = 2

#: ``RPC_E_CALL_REJECTED``: AutoCAD's message filter refused the call; it did not run.
RPC_E_CALL_REJECTED = -2147418111

#: How long one refused job is retried before the run gives up on it.
REJECTED_PATIENCE_S = 90.0

_FIRST_PAUSE_S = 0.5
_MAX_PAUSE_S = 5.0


class LiveUnavailable(RuntimeError):
    """No running AutoCAD to attach to (or pywin32 is missing): nothing ran."""


class ActiveDocumentChanged(RuntimeError):
    """AutoCAD's active document is not one this run opened: the run stops here."""


def hresult(exc: BaseException) -> int | None:
    """The HRESULT of a COM error, raw or as ``ComBackend._run`` re-raises it."""
    for candidate in (exc, exc.__cause__):
        if candidate is None:
            continue
        code = getattr(candidate, "hresult", None)
        if isinstance(code, int):
            return code
        if type(candidate).__name__ == "com_error" and candidate.args:
            first = candidate.args[0]
            if isinstance(first, int):
                return first
    return None


def _version_only(text: str) -> str:
    """``"25.1s (LMS Tech)"`` -> ``"25.1s"``: the parenthesised licence text is dropped."""
    return re.sub(r"\s*\(.*\)\s*$", "", str(text)).strip()


def _pywin32_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("pywin32")
    except PackageNotFoundError:
        return None


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def server_build() -> dict:
    """The server's version, commit and whether tracked files differ from it."""
    from version import __version__

    sha = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "version": __version__,
        "sha": sha.strip() if sha else None,
        "dirty": bool(status.strip()) if status is not None else None,
    }


def environment(autocad: dict | None) -> dict:
    return {
        "autocad": autocad,
        "os": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "pywin32": _pywin32_version(),
        "server": server_build(),
    }


class _Own:
    """One document this run opened. ``doc`` is touched on the COM thread only."""

    __slots__ = ("doc", "original", "name", "full_name", "closed")

    def __init__(self, doc, name: str, full_name: str):
        self.doc = doc
        self.original = name
        self.name = name
        self.full_name = full_name
        self.closed = False

    @property
    def label(self) -> str:
        return self.full_name or self.name


class LiveHarness:
    """One smoke run: attach, own documents, the guard, checks and the result."""

    def __init__(
        self,
        smoke: str,
        *,
        backend=None,
        client=None,
        progid: str | None = None,
        keep: bool = False,
        patience_s: float = REJECTED_PATIENCE_S,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ):
        self.smoke = smoke
        self.backend = backend
        self._client = client
        self._progid = progid
        self.keep = keep
        self.patience_s = float(patience_s)
        self._sleep = sleep or asyncio.sleep
        self.checks: list[dict] = []
        self.report: dict = {}
        self.autocad: dict | None = None
        self.rpc_retries = 0
        self._docs: list[_Own] = []
        self._guarding = False
        self._suspended = 0
        self._stage: str | None = None
        self._status: str | None = None  # "unavailable" / "refused" once decided
        self.reason: str | None = None
        self._cleanup_failed: list[str] = []
        self._started_at = datetime.now(UTC)
        self._clock = time.monotonic()
        self._original_run = None

    # ── checks ───────────────────────────────────────────────────────────────

    def stage(self, name: str | None) -> None:
        """Label the checks (and an exception) that follow with ``name``."""
        self._stage = name

    def check(self, name: str, ok: bool, *, expected: Any = None, observed: Any = None) -> bool:
        """Record one pass / fail; returns ``ok`` so a smoke can branch on it."""
        self.checks.append(
            {
                "name": name,
                "status": "pass" if ok else "fail",
                "expected": expected,
                "observed": observed,
                "stage": self._stage,
            }
        )
        return bool(ok)

    def error(self, exc: BaseException) -> None:
        """Record an exception that ended the run as a check that raised."""
        lines = traceback.format_exception_only(type(exc), exc)
        self.checks.append(
            {
                "name": f"{self._stage or self.smoke}: ran without raising",
                "status": "error",
                "error": "".join(lines).strip(),
                "stage": self._stage,
            }
        )

    def unavailable(self, reason: str) -> None:
        self._status, self.reason = "unavailable", reason

    def refuse(self, reason: str) -> None:
        self._status, self.reason = "refused", reason

    def status(self) -> str:
        if self._status is not None:
            return self._status
        if not self.checks or self._cleanup_failed:
            return "fail"
        return "pass" if all(row["status"] == "pass" for row in self.checks) else "fail"

    def exit_code(self) -> int:
        status = self.status()
        if status in ("unavailable", "refused"):
            return EXIT_UNAVAILABLE
        return EXIT_PASS if status == "pass" else EXIT_FAIL

    # ── attach and the job wrapper ───────────────────────────────────────────

    @property
    def progid(self) -> str:
        if self._progid is None:
            import config

            self._progid = config.settings.cad_progid
        return self._progid

    def _attach_sync(self):
        """The attached application, re-attached with GetActiveObject (COM thread)."""
        from backends import com_backend as com

        app = com._COM_STATE.get("app")
        if app is None:
            try:
                app = self._client.GetActiveObject(self.progid)
            except Exception as exc:
                raise LiveUnavailable(
                    f"could not attach to a running {self.progid} with GetActiveObject "
                    f"({type(exc).__name__}: {exc}); the harness never launches it"
                ) from exc
            com._COM_STATE["app"] = app
        return app

    def _guard_sync(self, app) -> None:
        """Refuse unless the active document is one of the run's own (COM thread)."""
        if not self._guarding or self._suspended:
            return
        own = {(str(o.doc.Name), str(o.doc.FullName)) for o in self._docs if not o.closed}
        active = app.ActiveDocument if int(app.Documents.Count) else None
        key = None if active is None else (str(active.Name), str(active.FullName))
        if key is None or key not in own:
            # Never name the other document: it may be an operator's drawing.
            raise ActiveDocumentChanged(
                "refused: AutoCAD's active document is not one this run opened "
                f"({len(own)} of its own open); the run stops before anything reaches it"
            )

    def _install(self) -> None:
        backend = self.backend
        original = backend._run
        self._original_run = original

        async def run(func, *args, **kwargs):
            @functools.wraps(func)
            def job(*a, **kw):
                app = self._attach_sync()
                self._guard_sync(app)
                return func(*a, **kw)

            deadline = time.monotonic() + self.patience_s
            pause = _FIRST_PAUSE_S
            while True:
                try:
                    return await original(job, *args, **kwargs)
                except (LiveUnavailable, ActiveDocumentChanged):
                    raise
                except Exception as exc:
                    if hresult(exc) != RPC_E_CALL_REJECTED or time.monotonic() > deadline:
                        raise
                    self.rpc_retries += 1
                    await self._sleep(pause)
                    pause = min(pause * 2.0, _MAX_PAUSE_S)

        backend._run = run

    async def raw(self, fn: Callable[[Any], Any]) -> Any:
        """``fn(app)`` on the COM thread, through the same attach, guard and retry."""

        def _raw():
            return fn(self._attach_sync())

        return await self.backend._run(_raw)

    @contextmanager
    def unguarded(self):
        """Calls that choose a document (or close one) run without the guard."""
        self._suspended += 1
        try:
            yield
        finally:
            self._suspended -= 1

    async def start(self) -> None:
        if self._client is None:
            try:
                import win32com.client as client
            except ImportError as exc:
                raise LiveUnavailable(f"pywin32 is not available: {exc}") from exc
            self._client = client
        if self.backend is None:
            try:
                from backends.com_backend import ComBackend
            except ImportError as exc:
                raise LiveUnavailable(f"the COM backend cannot load: {exc}") from exc
            self.backend = ComBackend()
        if not self.backend.is_connected:
            try:
                await self.backend.connect()
            except RuntimeError as exc:
                raise LiveUnavailable(str(exc)) from exc
        self._install()

        def _identify(app):
            return {
                "name": str(app.Name),
                "version": _version_only(app.Version),
                "build": None,
                "progid": self.progid,
            }

        self.autocad = await self.raw(_identify)

    # ── its own documents ────────────────────────────────────────────────────

    async def _keys(self) -> set[tuple[str, str]]:
        def _sync(app):
            docs = (app.Documents.Item(i) for i in range(int(app.Documents.Count)))
            return {(str(d.Name), str(d.FullName)) for d in docs}

        return await self.raw(_sync)

    async def _adopt(self, before: set, match: Callable[[str, str], bool], what: str) -> str:
        def _sync(app):
            for i in range(int(app.Documents.Count)):
                doc = app.Documents.Item(i)
                key = (str(doc.Name), str(doc.FullName))
                if key not in before and match(*key):
                    self._docs.append(_Own(doc, key[0], key[1]))
                    if self.autocad is not None and self.autocad.get("build") is None:
                        try:
                            self.autocad["build"] = str(doc.GetVariable("_VERNUM"))
                        except Exception:  # the build is evidence, not a requirement
                            pass
                    return key[0]
            return None

        name = await self.raw(_sync)
        if name is None:
            raise RuntimeError(f"{what}: no new document of that name is open in AutoCAD")
        self._guarding = True
        return name

    async def new_document(self, template: str | None = None) -> str:
        """``backend.drawing_new`` (bound by the backend), held as the run's own."""
        with self.unguarded():
            before = await self._keys()
            if template is None:
                result = await self.backend.drawing_new()
            else:
                result = await self.backend.drawing_new(template=template)
            wanted = str(result["name"])
            return await self._adopt(
                before, lambda name, _full: name == wanted, f"drawing_new reported {wanted}"
            )

    async def open_document(self, path: str) -> str:
        """``backend.drawing_open(path)``, held as the run's own."""
        with self.unguarded():
            before = await self._keys()
            result = await self.backend.drawing_open(str(path))
            wanted = _norm(result.get("path") or path)
            return await self._adopt(
                before,
                lambda _name, full: bool(full) and _norm(full) == wanted,
                f"drawing_open of {Path(str(path)).name}",
            )

    async def name_of(self, original: str) -> str:
        """The name one of the run's own documents has now (a ``SaveAs`` renames it)."""
        await self._refresh()
        own = next((o for o in self._docs if original in (o.original, o.name)), None)
        if own is None:
            raise ValueError(f"{original} is not one of this run's documents")
        return own.name

    async def _refresh(self) -> None:
        def _sync(app):
            for own in self._docs:
                if not own.closed and own.doc is not None:
                    own.name, own.full_name = str(own.doc.Name), str(own.doc.FullName)

        await self.raw(_sync)

    async def close_document(self, name: str) -> None:
        """Close one of the run's own documents with ``Close(False)`` and bind
        the session to another of its own, if one is left. ``name`` is the name
        the document has now or the one it was opened under (a ``SaveAs``
        renames it)."""
        with self.unguarded():
            await self._refresh()
            own = next(
                (o for o in self._docs if not o.closed and name in (o.name, o.original)), None
            )
            if own is None:
                raise ValueError(f"{name} is not one of this run's open documents")

            def _close(_app):
                own.doc.Close(False)
                own.closed = True
                own.doc = None

            await self.raw(_close)
            left = [o for o in self._docs if not o.closed]
            if left:
                await self.backend.document_activate(left[-1].label)

    async def finish(self) -> None:
        """Close every own document (unless ``keep``) and disconnect."""
        if self.backend is None:
            return
        with self.unguarded():
            if self._original_run is not None:
                try:
                    await self._refresh()
                except Exception:  # names are evidence; the close below uses the object
                    pass
            if not self.keep and self._original_run is not None:
                for own in [o for o in self._docs if not o.closed]:

                    def _close(_app, own=own):
                        own.doc.Close(False)
                        own.closed = True
                        own.doc = None

                    try:
                        await self.raw(_close)
                    except Exception as exc:
                        self._cleanup_failed.append(own.name)
                        print(
                            f"could not close {own.name}: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
            if self._original_run is not None:

                def _drop(_app):
                    for own in self._docs:
                        own.doc = None

                try:
                    await self.raw(_drop)
                except Exception:
                    pass
        try:
            await self.backend.disconnect()
        except Exception as exc:  # the result is already decided
            print(f"disconnect failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    # ── the result ───────────────────────────────────────────────────────────

    def result(self) -> dict:
        statuses = [row["status"] for row in self.checks]
        left_open = [o.name for o in self._docs if not o.closed]
        result = {
            "schema": SCHEMA,
            "smoke": self.smoke,
            "status": self.status(),
            "exit_code": self.exit_code(),
            "reason": self.reason,
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - self._clock, 3),
            "environment": environment(self.autocad),
            "summary": {
                "checks": len(statuses),
                "passed": statuses.count("pass"),
                "failed": statuses.count("fail"),
                "errors": statuses.count("error"),
                "rpc_retries": self.rpc_retries,
            },
            "checks": self.checks,
            "documents": {
                "opened": len(self._docs),
                "closed": sum(1 for o in self._docs if o.closed),
                "left_open": left_open,
                "kept": bool(self.keep),
            },
            "report": self.report,
        }
        if self._cleanup_failed:
            result["documents"]["close_failed"] = list(self._cleanup_failed)
        return result


def scrub(value: Any, home: str | None = None) -> Any:
    """``value`` with the user's home folder written as ``~`` in every string.

    A result is committed as evidence (benchmarks/results/live/); temporary
    folders and AutoCAD's support paths sit under the home folder, whose name
    is the operator's account and has no place in the repository.
    """
    home = str(Path.home()) if home is None else home
    if not home:
        return value
    parts = [re.escape(part) for part in re.split(r"[\\/]+", home) if part]
    lead = r"[\\/]" if home[:1] in ("/", "\\") else ""
    pattern = re.compile(lead + r"[\\/]".join(parts), re.IGNORECASE)

    def walk(item):
        if isinstance(item, str):
            return pattern.sub("~", item)
        if isinstance(item, dict):
            return {walk(key): walk(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)):
            return [walk(val) for val in item]
        return item

    return walk(value)


def _norm(path) -> str:
    try:
        return str(Path(str(path)).resolve()).lower()
    except (OSError, ValueError):
        return str(path).lower()


Body = Callable[[LiveHarness], Awaitable[dict | None]]


async def run_smoke(
    smoke: str,
    body: Body,
    *,
    keep: bool = False,
    json_path: str | None = None,
    backend=None,
    client=None,
    progid: str | None = None,
    stream=None,
    sleep=None,
    patience_s: float = REJECTED_PATIENCE_S,
) -> int:
    """Run ``body(harness)`` once on the live seat; print (and write) the JSON; the exit code."""
    h = LiveHarness(
        smoke,
        backend=backend,
        client=client,
        progid=progid,
        keep=keep,
        patience_s=patience_s,
        sleep=sleep,
    )
    try:
        await h.start()
        returned = await body(h)
        if returned:
            h.report.update(returned)
    except LiveUnavailable as exc:
        h.unavailable(str(exc))
    except ActiveDocumentChanged as exc:
        h.refuse(str(exc))
    except Exception as exc:
        h.error(exc)
    finally:
        await h.finish()
    result = scrub(json.loads(json.dumps(h.result(), default=str)))
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text, file=stream or sys.stdout)
    if json_path:
        target = Path(json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    return result["exit_code"]


def parser(description: str) -> argparse.ArgumentParser:
    """The flags every smoke takes; a smoke adds its own."""
    out = argparse.ArgumentParser(description=description)
    out.add_argument("--keep", action="store_true", help="leave the run's documents open")
    out.add_argument("--json", metavar="PATH", help="also write the JSON result to PATH")
    return out


def main_cli(smoke: str, body: Body, argv: list[str] | None = None, description: str = "") -> int:
    """``python scripts/smoke_<x>_com.py [--keep] [--json PATH]``."""
    args = parser(description or smoke).parse_args(argv)
    return asyncio.run(run_smoke(smoke, body, keep=args.keep, json_path=args.json))

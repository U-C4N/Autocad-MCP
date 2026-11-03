"""1.8-17 - the write-path policy: every writer names its extensions, and no tool
writes a file AutoCAD or Windows would load or run.

Writing ``acaddoc.lsp`` into a folder AutoCAD searches is a code-execution path
on a live seat: the file runs the next time a drawing opens. 1.7's
``data_extract`` checked no extension at all. Now ``validate_path(allow_write=
True)`` refuses the loadable names unconditionally (inside ``ALLOWED_PATHS``
too), and each writer passes ``kinds`` - the extension set it writes - which an
AST scan below holds every write call site to.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import pytest_asyncio
from fastmcp import Client

import config
import server
from security import (
    DIRECTORY,
    WRITE_REFUSED_SUFFIXES,
    PathRefusedError,
    validate_path,
)

REPO = Path(__file__).resolve().parents[1]

#: Where write call sites live: the tools, the engineering helpers they call,
#: the backends.
SCANNED = ("server.py", "engineering", "backends")


def _write_calls_without_kinds(source: str, filename: str) -> list[str]:
    """``file:line`` of every ``validate_path(..., allow_write=True)`` without ``kinds=``."""
    missing = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != "validate_path":
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        write = keywords.get("allow_write")
        if not (isinstance(write, ast.Constant) and write.value is False) and write is not None:
            if "kinds" not in keywords:
                missing.append(f"{filename}:{node.lineno}")
    return missing


def _sources():
    for entry in SCANNED:
        path = REPO / entry
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for file in files:
            yield file.relative_to(REPO).as_posix(), file.read_text(encoding="utf-8")


def test_the_scanner_flags_a_write_without_kinds():
    snippet = (
        "validate_path(p, allow_write=True)\n"
        "validate_path(p, allow_write=True, kinds=('.pdf',))\n"
        "validate_path(p)\n"
        "security.validate_path(p, allow_write=flag)\n"
    )
    assert _write_calls_without_kinds(snippet, "x.py") == ["x.py:1", "x.py:4"]


def test_every_write_call_site_passes_kinds():
    missing = [site for name, text in _sources() for site in _write_calls_without_kinds(text, name)]
    assert missing == [], f"write call sites without kinds=: {missing}"


# ── validate_path itself ────────────────────────────────────────────────────


@pytest.mark.parametrize("suffix", sorted(WRITE_REFUSED_SUFFIXES))
def test_a_loadable_name_is_refused_even_inside_allowed_paths(suffix, tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "allowed_paths", [tmp_path.resolve()])
    with pytest.raises(PathRefusedError) as excinfo:
        validate_path(str(tmp_path / f"acaddoc{suffix}"), allow_write=True)
    assert excinfo.value.kind == "path_refused"
    assert excinfo.value.details["reason"] == "executable_name"


@pytest.mark.parametrize(
    "name", ["acad.pgp", "ACAD.PGP", "acad2027.lsp", "AcadDoc.LSP", "acaddoc.lsp.", "acaddoc.lsp "]
)
def test_autoload_spellings_are_refused(name, tmp_path):
    with pytest.raises(PathRefusedError):
        validate_path(str(tmp_path / name), allow_write=True, kinds=(".csv",))


def test_an_alternate_data_stream_is_refused(tmp_path):
    with pytest.raises(PathRefusedError) as excinfo:
        validate_path(str(tmp_path / "report.csv:acaddoc.lsp"), allow_write=True, kinds=(".csv",))
    assert excinfo.value.details["reason"] == "alternate_data_stream"


def test_kinds_admit_only_the_writers_extensions(tmp_path):
    assert validate_path(str(tmp_path / "a.PDF"), allow_write=True, kinds=(".pdf",))
    with pytest.raises(PathRefusedError) as excinfo:
        validate_path(str(tmp_path / "a.dxf"), allow_write=True, kinds=(".pdf",))
    assert excinfo.value.details == {
        "reason": "extension_not_allowed",
        "extension": ".dxf",
        "allowed": [".pdf"],
    }
    with pytest.raises(PathRefusedError, match=r"\.csv or \.xlsx"):
        validate_path(str(tmp_path / "a.txt"), allow_write=True, kinds=(".csv", ".xlsx"))
    with pytest.raises(PathRefusedError, match="is a directory") as excinfo:
        validate_path(str(tmp_path), allow_write=True, kinds=(".csv",))
    assert excinfo.value.details["reason"] == "is_a_directory"


def test_a_directory_target_must_not_be_a_file(tmp_path):
    assert validate_path(str(tmp_path), allow_write=True, kinds=DIRECTORY) == tmp_path.resolve()
    afile = tmp_path / "out.txt"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(PathRefusedError):
        validate_path(str(afile), allow_write=True, kinds=DIRECTORY)


def test_reading_a_lisp_file_is_not_a_write(tmp_path):
    """Only writes are refused: a tool may still be pointed at a .lsp to read it."""
    assert validate_path(str(tmp_path / "acaddoc.lsp"))


# ── through the tools ───────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client():
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


@pytest.mark.asyncio
async def test_data_extract_acaddoc_lsp_is_refused(client, tmp_path):
    target = tmp_path / "x" / "acaddoc.lsp"
    target.parent.mkdir()
    result = await client.call_tool("data_extract", {"path": str(target)}, raise_on_error=False)
    payload = result.structured_content
    assert result.is_error
    assert payload["kind"] == "path_refused" and payload["category"] == "policy"
    assert payload["reason"] == "executable_name" and payload["extension"] == ".lsp"
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("drawing_save_as", {"path": "{dir}/acad.lsp"}),
        ("drawing_export_dxf", {"path": "{dir}/drawing.scr"}),
        ("drawing_export_pdf", {"path": "{dir}/sheet.dxf"}),
        ("drawing_template_save", {"path": "{dir}/t.mnl"}),
        ("pid_instrument_index", {"csv_path": "{dir}/acad.pgp"}),
        ("pipe_takeoff", {"output": "{dir}/run.bat"}),
    ],
)
async def test_each_writer_refuses_a_name_outside_its_kinds(client, tmp_path, tool, args):
    filled = {key: value.format(dir=tmp_path.as_posix()) for key, value in args.items()}
    result = await client.call_tool(tool, filled, raise_on_error=False)
    assert result.is_error
    assert result.structured_content["kind"] == "path_refused", result.structured_content
    assert not any(tmp_path.iterdir())

"""1.8-18 (F08): the AutoLISP read-tier allowlist.

Up to 1.7.0 ``system_run_lisp`` refused every ``vla-get-*`` and the standard
``(mapcar (function (lambda ...)))`` while letting ``(getpoint)`` through.
``security_lisp`` reads the expression (a real reader, a total walk) and runs
what passes the read tier; what does not goes to the 1.7 denylist unchanged.

The gate: at least 60 real read forms pass; every existing denylist case is
still refused; string-literal content never changes the verdict (a property
test); a fuzz test; every refusal carries symbol / line / column.
"""

from __future__ import annotations

import random
import re

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError

import config
import security_lisp
import server
from security import CommandRefusedError, lisp_denylist_matches, sanitize_lisp
from security_lisp import (
    NEVER,
    READ,
    WRITE,
    LispPolicyRefusedError,
    LispReadError,
    assess,
    check_expression,
    read_forms,
)


@pytest.fixture(autouse=True)
def _read_policy(monkeypatch):
    monkeypatch.setattr(config.settings, "lisp_policy", "read")
    monkeypatch.setattr(config.settings, "dangerous_commands_enabled", False)


# ── the corpus ──────────────────────────────────────────────────────────────

#: Real read forms - what an agent sends to look at a drawing. Every one must
#: pass the read tier on its own (not by falling through to the denylist).
READ_CORPUS: tuple[str, ...] = (
    "(+ 1 2 3)",
    "(* 2.5 (- 10 4))",
    "(/ 22.0 7)",
    "(1+ 41)",
    "(abs -3.5)",
    "(sqrt (+ (expt 3 2) (expt 4 2)))",
    "(rem 17 5)",
    "(fix 3.7)",
    "(float 3)",
    "(max 1 5 3)",
    "(min 1 5 3)",
    "(sin (/ pi 6))",
    "(cos 0.0)",
    "(atan 1.0 1.0)",
    "(angle (list 0 0) (list 10 10))",
    "(distance (list 0 0 0) (list 3 4 0))",
    "(polar (list 0 0) (/ pi 4) 10.0)",
    "(inters '(0 0) '(10 10) '(0 10) '(10 0))",
    "(trans '(1 2 0) 0 1)",
    "(list 1 2 3)",
    '(cons 0 "LINE")',
    "(car '(1 2 3))",
    "(cdr '(1 2 3))",
    "(cadr '(1 2 3))",
    "(caddr '(1 2 3))",
    "(length '(a b c))",
    "(nth 1 '(10 20 30))",
    "(reverse '(1 2 3))",
    "(append '(1 2) '(3))",
    '(assoc 8 \'((0 . "LINE") (8 . "WALLS")))',
    "(mapcar '+ '(1 2) '(10 20))",
    "(mapcar (function (lambda (x) (* x 2))) '(1 2 3))",
    "(mapcar '(lambda (p) (car p)) '((1 2) (3 4)))",
    "(foreach x '(1 2 3) (setq total (+ (if total total 0) x)))",
    '(if (> 3 2) "yes" "no")',
    '(cond ((= 1 2) "a") (t "b"))',
    "(progn (setq a 1) (setq b 2) (+ a b))",
    "(setq pt (list 1.0 2.0 0.0))",
    "(apply '+ '(1 2 3))",
    "(apply (function max) (list 4 9 2))",
    "((lambda (x y) (+ x y)) 3 4)",
    "(eval '(+ 1 2))",
    '(strcat "A" "-" "101")',
    '(substr "PUMP-101" 6 3)',
    '(strlen "hello")',
    '(strcase "abc")',
    "(rtos 12.3456 2 2)",
    "(itoa 42)",
    '(atoi "42")',
    '(atof "4.2")',
    '(vl-string-subst "B" "A" "AAA")',
    '(vl-string-search "load" "Max load 500 kg")',
    '(vl-string-trim " " "  x  ")',
    '(wcmatch "PUMP-101" "PUMP-*")',
    '(getvar "CLAYER")',
    '(getvar "DIMSCALE")',
    '(tblsearch "LAYER" "0")',
    '(tblnext "LAYER" t)',
    "(entget (entlast))",
    "(entnext)",
    '(handent "2A")',
    '(cdr (assoc 0 (entget (handent "2A"))))',
    '(dictsearch (namedobjdict) "ACAD_LAYOUT")',
    '(sslength (ssget "_X" (list (cons 0 "LINE"))))',
    '(ssname (ssget "_X" \'((0 . "CIRCLE"))) 0)',
    "(ssget \"_W\" '(0 0) '(100 100))",
    "(ssget \"_C\" '(0 0) '(100 100))",
    '(ssget "_CP" \'((0 0) (10 0) (10 10)))',
    '(vla-get-Area (vlax-ename->vla-object (handent "2A")))',
    "(vla-get-Layer (vlax-ename->vla-object (entlast)))",
    "(vlax-get-property (vlax-ename->vla-object (entlast)) 'Length)",
    '(vlax-curve-getArea (handent "2A"))',
    "(vlax-curve-getDistAtParam (entlast) (vlax-curve-getEndParam (entlast)))",
    "(vlax-safearray->list (vlax-variant-value "
    "(vla-get-Coordinates (vlax-ename->vla-object (entlast)))))",
    "(vl-catch-all-apply 'vla-get-Area (list (vlax-ename->vla-object (entlast))))",
    "(vl-sort '(3 1 2) '<)",
    "(vl-remove-if '(lambda (x) (> x 2)) '(1 2 3 4))",
    '(vl-princ-to-string (list 1 "a"))',
    "(progn\n  ; the area of the last entity\n  (vla-get-Area (vlax-ename->vla-object (entlast)))\n)",
    "(+ 1 ;| an inline note |; 2)",
    '(setq desc "Max load 500 kg")',
    '(setq layer "C:CENTER")',
    '(setq note "Design load = 12 kN")',
    "(vl-load-com)",
    '(equal (getvar "LUNITS") 2)',
    '(not (null (tblsearch "LTYPE" "CENTER")))',
    "(and (= 1 1) (or nil t))",
    '(member "B" (list "A" "B"))',
    "(subst 9 1 (list 1 2 1))",
    "(type (entlast))",
    "(angtos (/ pi 2) 0 2)",
    '(distof "12.5" 2)',
    "(chr 65)",
    '(ascii "A")',
    '(vlax-property-available-p (vlax-ename->vla-object (entlast)) "Area")',
    "(vlax-vla-object->ename (vlax-ename->vla-object (entlast)))",
    "(vl-position 3 (list 1 2 3))",
    "(last (list 1 2 3))",
    '(strcat "say \\"hi\\"" "\\\\")',
)

#: Every expression the 1.7 tests refused (tests/test_security.py,
#: tests/test_refusal_alternatives.py), plus one per 1.7 denylist pattern,
#: written the way an agent would send it.
DENYLIST_CASES: tuple[str, ...] = (
    '(startapp "cmd")',
    '(command "ERASE" "ALL" "")',
    '(vl-file-delete "C:/important.dwg")',
    '(load "malicious.lsp")',
    "(eval (read user-input))",
    "(setq f command)(f)",
    '(vlax-invoke obj "SendCommand" "x")',
    '(acet-sys-shell "cmd")',
    "(c:mycmd)",
    '(startapp (strcat "c" "md"))',
    '(vl-catch-all-apply (quote command) (list "ERASE"))',
    '(command "_.ERASE" "_L" "")',
    "(vla-get-activedocument (vlax-get-acad-object))",
    '(vl-cmdf "-LAYER" "_S" "0" "")(command-s "_ZOOM" "_E")',
    '(dos_command "x")',
    '(vl-file-rename "a" "b")',
    '(vl-file-copy "a" "b")',
    '(vl-file-directory-p "a")',
    '(vl-mkdir "x")',
    '(vl-directory-files "x")',
    "(read-line f)",
    '(write-line "x" f)',
    '(findfile "acad.lsp")',
    '(getfiled "t" "" "dwg" 0)',
    "(command)",
    '(command-s "_LINE")',
    '(vl-cmdf "_LINE")',
    "(vla-delete obj)",
    '(vla-SendCommand doc "_LINE ")',
    "(vlax-get-acad-object)",
    '(acet-file-delete "x")',
    "(vl-acad-defun (quote c:x))",
    "(vl-acad-undefun (quote c:x))",
    '(autoload "x" (list "y"))',
    '(arxload "x.arx")',
    "(eval x)",
    "(apply f lst)",
    "(funcall f)",
    "(function f)",
    "(c:my-cmd)",
    "(setq *push-error-using-command* t)",
    "(setq *push-error-using-stack* t)",
    "(mapcar 'command '(\"_LINE\"))",
    "(apply 'eval '((command)))",
)

#: Refused under LISP_POLICY=read by every tier, though 1.7's denylist ran
#: them: they prompt the operator (and hold the STA), define code, or do not read.
NEVER_CASES: tuple[str, ...] = (
    "(getpoint)",
    '(getpoint "Pick: ")',
    "(getstring)",
    '(getkword "[Yes/No]")',
    "(getcorner '(0 0))",
    "(entsel)",
    "(nentsel)",
    "(grread)",
    "(ssget)",
    '(ssget \'((0 . "LINE")))',
    '(ssget ":S")',
    '(ssget "_:L")',
    "(ssget mode)",
    '(read "(command)")',
    "(defun f () 1)",
    "(defun c:foo () 1)",
    "(eval (list (quote +) 1 2))",
    '(vlax-create-object "Excel.Application")',
    '(vlax-get-or-create-object "x")',
    '(dos_dir "C:\\\\")',
    '(vl-file-size "x")',
    "(vlr-command-reactor nil nil)",
    # 1.7's comment said its `vla[xr]?-` pattern covered vlr-*; it never
    # matched "vlr-" (no "vla" prefix), so the denylist ran reactors.
    "(vlr-object-reactor nil nil nil)",
    "(mapcar f '(1 2))",
    "(apply (function apply) '(+ (1 2)))",
    "(setq g getpoint)",
    "(+ 1 2",
    "(+ 1 2))",
    ")",
    '"abc',
    "(+ 1 '",
    "(1 . )",
    "(. 1)",
    "(+ 1 2) ; a trailing comment",
    ";| never closed",
    "1)) ERASE ALL  ((",
    # what reads one way here and another at AutoCAD's command line
    "(+ 1 ; )\n ERASE ALL\n 2)",
    "(+ 1 ;| ( |; 2)",
    '(+ 1 ; say "x\n 2)',
    "(+ 1 2)\x1b_ERASE ALL ",
    '(strcat "a\x1bb")',
    "(+ 1\x0c2)",
)


def _refusal(expression: str) -> LispPolicyRefusedError:
    with pytest.raises(LispPolicyRefusedError) as raised:
        sanitize_lisp(expression)
    return raised.value


# ── the read tier ───────────────────────────────────────────────────────────


def test_the_corpus_is_at_least_sixty_real_read_forms():
    assert len(set(READ_CORPUS)) >= 60


@pytest.mark.parametrize("expression", READ_CORPUS)
def test_a_real_read_form_passes_the_read_tier(expression):
    verdict = assess(expression)
    assert verdict.tier == READ, verdict.findings
    assert sanitize_lisp(expression) == expression
    stray = [hit for hit in lisp_denylist_matches(expression) if not verdict.covers(hit)]
    assert stray == [], "the denylist's second pass agrees with the reader"


def test_the_forms_1_7_refused_are_the_ones_now_read():
    """The point of F08: the denylist refused these; the read tier runs them."""
    refused_by_1_7 = [
        e for e in READ_CORPUS if check_expression_refused(e, "denylist") and assess(e).tier == READ
    ]
    assert len(refused_by_1_7) >= 12
    assert '(vla-get-Area (vlax-ename->vla-object (handent "2A")))' in refused_by_1_7
    assert "(mapcar (function (lambda (x) (* x 2))) '(1 2 3))" in refused_by_1_7


def check_expression_refused(expression: str, policy: str) -> bool:
    try:
        check_expression(expression, policy)
    except LispPolicyRefusedError:
        return True
    return False


# ── the denylist, still ─────────────────────────────────────────────────────


@pytest.mark.parametrize("expression", DENYLIST_CASES)
@pytest.mark.parametrize("policy", ["read", "denylist"])
def test_every_existing_denylist_case_is_still_refused(monkeypatch, expression, policy):
    monkeypatch.setattr(config.settings, "lisp_policy", policy)
    refusal = _refusal(expression)
    assert isinstance(refusal, CommandRefusedError) and refusal.channel == "lisp"
    assert str(refusal).startswith("LISP expression rejected: contains a restricted function.")
    assert refusal.policy == ("lisp_allowlist" if policy == "read" else "lisp_denylist")


@pytest.mark.parametrize("expression", NEVER_CASES)
def test_never_cases_are_refused_by_every_tier(expression):
    refusal = _refusal(expression)
    assert refusal.tier_needed == NEVER, refusal.fields()
    assert refusal.reason in ("never_runs", "unreadable")


def test_denylist_mode_is_1_7_exactly(monkeypatch):
    monkeypatch.setattr(config.settings, "lisp_policy", "denylist")
    refusal = _refusal('(vla-get-Area (vlax-ename->vla-object (handent "2A")))')
    assert refusal.reason == "denylist" and refusal.symbol == "vla-get-area"
    assert refusal.tier_needed == READ, "LISP_POLICY=read would run it"
    assert str(refusal) == (
        "LISP expression rejected: contains a restricted function. "
        "Restricted functions include startapp, command, load, eval, file I/O, etc. "
        "Set DANGEROUS_COMMANDS_ENABLED=true to allow all LISP expressions."
    )
    # What 1.7 let through still goes through, prompting forms included.
    assert sanitize_lisp("(getpoint)") == "(getpoint)"
    assert sanitize_lisp('(setvar "DIMSCALE" 1.0)') == '(setvar "DIMSCALE" 1.0)'


def test_off_checks_nothing(monkeypatch):
    monkeypatch.setattr(config.settings, "lisp_policy", "off")
    assert sanitize_lisp('(startapp "cmd")') == '(startapp "cmd")'
    with pytest.raises(ToolError, match="cannot be empty"):
        sanitize_lisp("  ")


def test_outside_the_read_tier_the_1_7_denylist_decides():
    # setvar is write tier and not denylisted: it runs, as in 1.7.
    assert sanitize_lisp('(setvar "DIMSCALE" 1.0)') == '(setvar "DIMSCALE" 1.0)'
    assert sanitize_lisp("(while nil)") == "(while nil)"
    # setvar beside a read the 1.7 denylist refuses: the whole goes to 1.7.
    refusal = _refusal('(progn (setvar "X" 1) (vla-get-Area o))')
    assert refusal.reason == "outside_read_tier" and refusal.symbol == "setvar"
    assert refusal.tier_needed == WRITE and "vla-get-area" in refusal.symbols


def test_the_policy_is_parsed_from_the_environment(monkeypatch, caplog):
    for raw, want in (("", "read"), ("DENYLIST", "denylist"), ("off", "off"), ("x", "read")):
        monkeypatch.setenv("LISP_POLICY", raw)
        assert config.Settings().lisp_policy == want
    assert "Unknown LISP_POLICY" in caplog.text
    assert security_lisp.parse_policy(None) == "read"


def test_dangerous_commands_enabled_still_bypasses(monkeypatch):
    monkeypatch.setattr(config.settings, "dangerous_commands_enabled", True)
    assert sanitize_lisp("(getpoint)") == "(getpoint)"


# ── every refusal says where ────────────────────────────────────────────────


@pytest.mark.parametrize("expression", DENYLIST_CASES + NEVER_CASES)
def test_every_refusal_carries_symbol_line_and_column(expression):
    refusal = _refusal(expression)
    fields = refusal.fields()
    assert set(fields) == {"policy", "symbol", "line", "column", "tier_needed", "reason"}
    assert isinstance(refusal.symbol, str) and refusal.symbol
    assert refusal.line >= 1 and refusal.column >= 1
    # line / column point at the named symbol (or the unreadable token).
    lines = expression.split("\n")
    at = lines[refusal.line - 1][refusal.column - 1 :].lower()
    assert at.startswith(refusal.symbol), (refusal.symbol, at)


def test_a_multi_line_refusal_points_at_the_right_line():
    refusal = _refusal('(progn\n  (+ 1 2)\n  (command "_ERASE" "_L" ""))')
    assert (refusal.symbol, refusal.line, refusal.column) == ("command", 3, 4)
    assert refusal.tier_needed == WRITE and refusal.verbs == ["ERASE"]
    assert "'command' at line 3, column 4" in str(refusal)


def test_refusals_map_symbols_to_typed_verbs():
    assert "ERASE" in _refusal('(entdel (handent "2A"))(command)').alternative_verbs
    assert "ID" in _refusal("(getpoint)").alternative_verbs
    assert "SELECT" in _refusal("(ssget)").alternative_verbs


# ── the reader ──────────────────────────────────────────────────────────────


def test_the_reader_reads_dotted_pairs_quotes_escapes_and_comments():
    (form,) = read_forms('\'((0 . "LINE") (8 . "A\\"B")) ;| x |;\n')
    assert form.kind == "list" and form.items[0].text == "quote" and form.items[0].synthetic
    pairs = form.items[1].items
    assert pairs[0].items[0].text == "0" and pairs[0].tail.text == "LINE"
    assert pairs[1].tail.text == 'A\\"B'
    assert [f.kind for f in read_forms("1 -2.5 .5 1e3 1+ abc")] == [
        "number",
        "number",
        "number",
        "number",
        "symbol",
        "symbol",
    ]


@pytest.mark.parametrize(
    ("text", "token", "offset"),
    [("(+ 1", "(", 0), ("(a))", ")", 3), ('(a "b)', '"', 3), ("(a . b c)", ".", 7)],
)
def test_the_reader_names_what_does_not_read(text, token, offset):
    with pytest.raises(LispReadError) as raised:
        read_forms(text)
    assert (raised.value.token, raised.value.offset) == (token, offset)


def test_deep_nesting_is_refused_not_a_recursion_error():
    depth = 5000
    refusal = _refusal("(" * depth + ")" * depth)
    assert refusal.tier_needed == NEVER and refusal.symbol == "("
    refusal = _refusal("'" + "(" * depth + ")" * depth)
    assert refusal.tier_needed == NEVER and refusal.symbol == "("
    assert refusal.column == 202, "the first list past the depth limit"


# ── string literals are data: the property test ─────────────────────────────

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_SSGET_MODE = re.compile(r"\(\s*ssget\s+$", re.IGNORECASE)
_HOSTILE = (
    "command",
    '(command "_ERASE" "ALL" "")',
    "load",
    "startapp",
    "vla-SendCommand",
    "c:foo",
    ")",
    "(",
    "))) (command",
    ";",
    ";|",
    "|;",
    "'",
    ".",
    "\n",
    "\\",
    '"',
    "Ø20 H7",
    "eval read apply",
    "",
)


def _random_contents(rng: random.Random) -> str:
    raw = "".join(rng.choice(_HOSTILE) for _ in range(rng.randint(0, 4)))
    return raw.replace("\\", "\\\\").replace('"', '\\"')


def _restring(expression: str, rng: random.Random) -> str:
    out, last = [], 0
    for match in _STRING.finditer(expression):
        out.append(expression[last : match.start()])
        if _SSGET_MODE.search(expression[: match.start()]):
            out.append(match.group(0))  # ssget's mode is a keyword, not data
        else:
            out.append('"' + _random_contents(rng) + '"')
        last = match.end()
    out.append(expression[last:])
    return "".join(out)


def _outcome(expression: str):
    try:
        sanitize_lisp(expression)
    except LispPolicyRefusedError as exc:
        return ("refused", exc.symbol, exc.tier_needed, exc.reason)
    return ("ran",)


@pytest.mark.parametrize("seed", range(4))
def test_string_literal_content_never_changes_the_verdict(seed):
    rng = random.Random(seed)
    corpus = [e for e in READ_CORPUS + DENYLIST_CASES + NEVER_CASES if _STRING.search(e)]
    corpus = [e for e in corpus if not isinstance(_safe_read(e), LispReadError)]
    assert len(corpus) > 60
    for expression in corpus:
        want = _outcome(expression)
        for _ in range(8):
            mutated = _restring(expression, rng)
            assert _outcome(mutated) == want, (expression, mutated)


def _safe_read(expression):
    try:
        return read_forms(expression)
    except LispReadError as exc:
        return exc


# ── fuzz ────────────────────────────────────────────────────────────────────

_TOKENS = (
    "(",
    ")",
    "'",
    '"',
    '"x"',
    '"_X"',
    ".",
    ";",
    ";|",
    "|;",
    "\n",
    " ",
    "\\",
    "command",
    "car",
    "vla-get-Area",
    "getpoint",
    "ssget",
    "lambda",
    "setq",
    "function",
    "apply",
    "eval",
    "quote",
    "mapcar",
    "defun",
    "c:x",
    "x",
    "1",
    "-2.5",
    "+",
    "entget",
    "load",
    "nil",
)


def _check_invariants(expression: str) -> None:
    try:
        result = sanitize_lisp(expression)
    except LispPolicyRefusedError as exc:
        assert exc.symbol and exc.line >= 1 and exc.column >= 1
        assert exc.tier_needed in (NEVER, WRITE, READ, "unclassified")
        return
    except ToolError as exc:  # only the empty expression
        assert "cannot be empty" in str(exc)
        return
    # Accepted: it reads, nothing in it is in no tier, and either the read
    # tier vouched for every node or the 1.7 denylist found nothing.
    assert result == expression
    verdict = assess(expression)
    assert verdict.tier != NEVER
    hits = lisp_denylist_matches(expression)
    if verdict.tier == READ:
        assert all(verdict.covers(hit) for hit in hits)
    else:
        assert hits == []


@pytest.mark.parametrize("seed", range(6))
def test_fuzz_token_soup(seed):
    rng = random.Random(1000 + seed)
    for _ in range(600):
        parts = [rng.choice(_TOKENS) for _ in range(rng.randint(0, 24))]
        _check_invariants("".join(p + rng.choice(("", " ", "\n")) for p in parts))


@pytest.mark.parametrize("seed", range(6))
def test_fuzz_mutated_corpus(seed):
    rng = random.Random(2000 + seed)
    alphabet = "()'\";|. \n\\abcx_-:*1"
    for expression in READ_CORPUS:
        for _ in range(6):
            chars = list(expression)
            for _ in range(rng.randint(1, 3)):
                op = rng.randrange(3)
                k = rng.randrange(len(chars) + 1)
                if op == 0 and chars:
                    del chars[min(k, len(chars) - 1)]
                elif op == 1:
                    chars.insert(k, rng.choice(alphabet))
                elif chars:
                    j = min(k, len(chars) - 1)
                    chars.insert(j, chars[j])
            _check_invariants("".join(chars))


# ── through the server ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("AUTOCAD_MCP_BACKEND", "ezdxf")
    async with Client(server.mcp) as connected:
        await connected.call_tool("drawing_new", {})
        yield connected


@pytest.mark.asyncio
async def test_a_refused_lisp_is_the_envelope_with_where_and_alternatives(client):
    result = await client.call_tool(
        "system_run_lisp", {"expression": '(command "_ERASE" "_L" "")'}, raise_on_error=False
    )
    payload = result.structured_content
    assert result.is_error and payload["kind"] == "policy_refused"
    assert payload["policy"] == "lisp_allowlist" and payload["channel"] == "lisp"
    assert (payload["symbol"], payload["line"], payload["column"]) == ("command", 1, 2)
    assert payload["tier_needed"] == "write" and payload["reason"] == "outside_read_tier"
    assert {"entity_delete", "entity_delete_many"} <= {r["tool"] for r in payload["alternatives"]}
    assert payload["category"] == "policy" and payload["ok"] is False


@pytest.mark.asyncio
async def test_a_refused_symbol_names_its_typed_tool(client):
    deleted = await client.call_tool(
        "system_run_lisp", {"expression": '(entdel (handent "2A"))'}, raise_on_error=False
    )
    # entdel is write tier but not denylisted: 1.7 ran it, so it still passes
    # the policy - and the headless engine answers that it has no LISP.
    assert not deleted.is_error
    assert deleted.structured_content["ok"] is False
    assert "not supported" in deleted.structured_content["error"]
    picked = (
        await client.call_tool(
            "system_run_lisp", {"expression": "(getpoint)"}, raise_on_error=False
        )
    ).structured_content
    assert picked["kind"] == "policy_refused" and picked["tier_needed"] == "none"
    assert "user_pick_point" in {r["tool"] for r in picked["alternatives"]}


@pytest.mark.asyncio
async def test_a_read_form_reaches_the_engine(monkeypatch):
    """A stand-in for the live engine: the read form is handed over unchanged."""
    from backends.ezdxf_backend import EzdxfBackend

    backend = EzdxfBackend()
    await backend.connect()
    await backend.drawing_new()
    sent: list[str] = []

    async def run_lisp(expression):
        sent.append(expression)
        return {"ok": True, "expression": expression, "result": "78.5398"}

    monkeypatch.setattr(backend, "system_run_lisp", run_lisp)

    async def make():
        return backend

    monkeypatch.setattr(server, "_make_backend", make)
    form = '(vla-get-Area (vlax-ename->vla-object (handent "2A")))'
    async with Client(server.mcp) as connected:
        result = (
            await connected.call_tool("system_run_lisp", {"expression": form})
        ).structured_content
    assert sent == [form] and result["result"] == "78.5398"


@pytest.mark.asyncio
async def test_the_headless_engine_keeps_its_own_refusal(client):
    result = await client.call_tool(
        "system_run_lisp", {"expression": '(getvar "CLAYER")'}, raise_on_error=False
    )
    assert not result.is_error, "the policy let the read form through"
    assert result.structured_content["ok"] is False
    assert "not supported in ezdxf backend" in result.structured_content["error"]


@pytest.mark.asyncio
async def test_cad_batch_still_denies_the_escape_hatch_with_the_lisp_fields(client):
    result = await client.call_tool(
        "cad_batch",
        {"steps": [{"tool": "system_run_lisp", "args": {"expression": "(getpoint)"}}]},
        raise_on_error=False,
    )
    payload = result.structured_content
    assert result.is_error and payload["ok"] is False
    assert "nothing was executed" in payload["message"]
    assert "'getpoint' at line 1, column 2" in payload["message"]


async def _tool_description(name: str) -> tuple[str, dict]:
    tool = await server.mcp.get_tool(name)
    return tool.description or "", tool.parameters


@pytest.mark.asyncio
async def test_the_docstring_example_is_a_form_we_run():
    description, parameters = await _tool_description("system_run_lisp")
    text = description + repr(parameters)
    assert '(command "ZOOM" "E")' not in text
    example = '(vla-get-Area (vlax-ename->vla-object (handent "2A")))'
    assert example in description
    assert assess(example).tier == READ


# ── the COM path is unchanged (fake COM) ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_com_engine_sends_the_read_form_inside_its_wrapper(monkeypatch):
    import backends.com_backend as cb
    from backends.com_backend import ComBackend

    class _Doc:
        def __init__(self):
            self.vars = {"CMDACTIVE": 0, "USERS1": ""}
            self.sent: list[str] = []

        def GetVariable(self, name):  # noqa: N802 - ActiveX member
            return self.vars.get(name, 0)

        def SetVariable(self, name, value):  # noqa: N802 - ActiveX member
            self.vars[name] = value

        def SendCommand(self, text):  # noqa: N802 - ActiveX member
            self.sent.append(text)
            self.vars["USERS1"] = "78.5398"

    doc = _Doc()
    monkeypatch.setattr(cb, "_acad_doc", lambda: doc)
    backend = ComBackend.__new__(ComBackend)

    async def run(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    backend._run = run
    backend._safe_send_command = lambda d, text: d.SendCommand(text + "\n")
    form = '(vla-get-Area (vlax-ename->vla-object (handent "2A")))'
    assert sanitize_lisp(form) == form
    out = await backend.system_run_lisp(form)
    assert out["result"] == "78.5398"
    (sent,) = doc.sent
    assert f"(progn {form})" in sent
    read_forms(sent.rstrip("\n"))  # the wrapped text still reads (balanced)


# ── the live smoke (written, run on a seat) and the v9 task ─────────────────


def _smoke():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_lisp_com.py"
    spec = importlib.util.spec_from_file_location("smoke_lisp_com", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_smoke_corpus_is_read_tier():
    smoke = _smoke()
    handles = {"circle": "2A", "pline": "2B", "line": "2C"}
    forms = [smoke._fill(f, handles) for f in (*smoke.CORPUS, *smoke.CROSS.values())]
    assert len(forms) >= 30
    for form in forms:
        assert "{" not in form, form
        assert assess(form).tier == READ, form
        assert sanitize_lisp(form) == form
    assert _refusal(smoke.TWIN).symbol == "command"


def _smoke_report(**overrides):
    report = {
        "corpus": [
            {"form": "(+ 1 2)", "policy": "read", "ok": True, "result": "3", "cmdactive_after": 0}
        ],
        "cross": {
            "circle_area": {"lisp": "78.5398", "tool": 78.53981633974483},
            "pline_area": {"lisp": "1200.0", "tool": 1200.0},
            "clayer": {"lisp": "0", "tool": "0"},
            "insunits": {"lisp": "4", "tool": 4},
            "ltscale": {"lisp": "1.0", "tool": 1.0},
        },
        "twin": {
            "refused": True,
            "symbol": "command",
            "line": 1,
            "column": 2,
            "tier_needed": "write",
        },
        "cmdactive_end": 0,
    }
    report.update(overrides)
    return report


@pytest.mark.parametrize(
    "overrides",
    [
        {"cmdactive_end": 1},
        {"corpus": [{"form": "(getpoint)", "policy": "none", "ok": True, "cmdactive_after": 0}]},
        {"corpus": [{"form": "(+ 1 2)", "policy": "read", "ok": False, "cmdactive_after": 0}]},
        {"corpus": [{"form": "(+ 1 2)", "policy": "read", "ok": True, "cmdactive_after": 1}]},
        {"cross": {"circle_area": {"lisp": "78.6", "tool": 78.53981633974483}}},
        {"cross": {"clayer": {"lisp": "WALLS", "tool": "0"}}},
        {
            "twin": {
                "refused": False,
                "symbol": None,
                "line": None,
                "column": None,
                "tier_needed": None,
            }
        },
    ],
)
def test_the_smoke_fails_on_each_broken_report(overrides):
    from scripts.live.harness import LiveHarness

    smoke = _smoke()
    clean = LiveHarness("lisp")
    smoke.assess(clean, _smoke_report())
    assert clean.checks and all(row["status"] == "pass" for row in clean.checks), clean.checks
    broken = LiveHarness("lisp")
    smoke.assess(broken, _smoke_report(**overrides))
    assert any(row["status"] == "fail" for row in broken.checks)


def test_v9_carries_lisp_read_tier_and_is_the_default():
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import DEFAULT_MATRIX, NEW_TASKS_V9, task_by_id

    assert task_by_id("lisp_read_tier", "v9").category == "security"
    # Appended right after 1.8-17's task, in the order the items landed; later
    # items (1.8-12/13, 1.8-08..10) append theirs after it.
    ids = [task.task_id for task in NEW_TASKS_V9]
    assert ids.index("lisp_read_tier") == ids.index("policy_read_only") + 1
    assert DEFAULT_MATRIX == "v9"  # 1.8-25
    assert hasattr(AutoCADMCPProAdapter, "_task_lisp_read_tier")


@pytest.mark.asyncio
async def test_the_v9_task_is_environment_blocked_headlessly(tmp_path):
    from benchmarks.adapters.autocad_mcp_pro import AutoCADMCPProAdapter
    from benchmarks.tasks_v9 import task_by_id

    adapter = AutoCADMCPProAdapter(backend="ezdxf")
    await adapter.setup(tmp_path)
    try:
        result = await adapter.run_task(task_by_id("lisp_read_tier", "v9"))
    finally:
        await adapter.cleanup()
    assert result.status == "environment_blocked", result
    assert result.metrics["policy_passed"] == {"area": True, "clayer": True, "mapcar": True}
    assert result.metrics["twin_ok"] is True

"""The AutoLISP read-tier allowlist (plan 1.8-18, F08).

Up to 1.7.0 ``system_run_lisp`` had one guard: a symbol denylist matched with a
regular expression (``security.sanitize_lisp``). It refused every ``vla-get-*``
and the standard ``(mapcar (function (lambda ...)))`` - one rival run had its
LISP refused sixteen times - while letting ``(getpoint)`` through, which waits
for the operator and holds AutoCAD's single COM thread until somebody clicks.

This module reads the expression instead of scanning it:

* **A real reader** (:func:`read_forms`): escaped strings, ``;`` and ``;| |;``
  comments, ``'`` quote, numbers, symbols, dotted pairs -> an AST of
  :class:`Node`. Unbalanced input, an unterminated string or block comment, a
  misplaced ``.``, a trailing ``;`` comment (which would swallow the
  parentheses ``ComBackend.system_run_lisp`` wraps the expression in), a
  comment holding a parenthesis or a double quote (a reader that does not skip
  comments would balance it differently) and a control character (SendCommand
  types it: ESC ends the LISP input and hands the rest to the command line)
  are refused: nothing unreadable reaches AutoCAD.
* **A total walk** (:func:`assess`): every node is one of four kinds and every
  call head is classified; a node the walk does not know is refused, never
  passed.
* **The read tier** - arithmetic / trig / geometry, list and control, strings,
  and the database reads (``entget``, ``handent``, ``tblsearch``, ``getvar``,
  ``ssget`` with a literal ``_X/_W/_C/_CP`` mode, ``vlax-ename->vla-object``,
  ``vla-get-*``, ``vlax-get-property``, ``vlax-curve-get*`` ...). ``setq`` /
  ``function`` / ``quote`` pass when their arguments pass the same check;
  ``apply`` / ``eval`` / ``mapcar`` and the other higher-order functions only
  with a literal function (a quoted read-tier symbol or a ``lambda``). No loop
  (``while`` / ``repeat``) and no ``defun`` are in it, so a read-tier form
  always terminates.
* **Always refused** (tier ``none``): the interactive ``get*`` / ``entsel`` /
  ``grread`` / mode-less or interactive-mode ``ssget`` (they lock the STA),
  ``load`` / ``autoload`` / ``arxload``, ``startapp``, ``dos_*``, ``vl-file-*``,
  ``read``, a non-literal ``eval`` / ``apply``, ``vlax-create-object``,
  ``vla-SendCommand``, ``vlr-*``, ``defun``, ``c:*``.
* **Everything else** (the write tier - ``command``, ``entmod``, ``setvar``,
  ``vla-put-*`` ... - which is 1.8.1, and anything unclassified) falls through
  to the 1.7 denylist unchanged: what it refused stays refused, what it passed
  still runs.
* **The denylist as a second pass** over a read-tier verdict: every match of
  the 1.7 pattern must lie inside a symbol the walk approved. A reader bug that
  hid code inside what it took for a string or a comment is caught by the
  older, independent scanner (a denylisted word inside a comment is refused for
  the same reason).

``LISP_POLICY=read|denylist|off`` (``config.settings.lisp_policy``, default
``read``): ``denylist`` is 1.7's behaviour exactly (the migration route),
``off`` runs every expression unchecked (as ``DANGEROUS_COMMANDS_ENABLED=true``
does for LISP). A refusal is :class:`LispPolicyRefusedError` - kind
``policy_refused`` with ``policy`` (``lisp_allowlist`` / ``lisp_denylist``),
``symbol``, ``line``, ``column`` (1-based), ``tier_needed`` (``write`` /
``none`` / ``unclassified``), ``reason`` and, filled by the server,
``alternatives``.

A guardrail, like the denylist before it, not a sandbox: the tier names what is
known to read; the escape hatch stays ``cost="escape"``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from config import LISP_POLICIES, parse_lisp_policy
from security import (
    _DANGEROUS_LISP_PATTERN,
    CommandRefusedError,
    DenylistHit,
    lisp_command_verbs,
    lisp_denylist_matches,
)

log = logging.getLogger("autocad_mcp.security_lisp")

__all__ = [
    "DEFAULT_POLICY",
    "NEVER",
    "POLICIES",
    "READ",
    "UNCLASSIFIED",
    "WRITE",
    "Finding",
    "LispPolicyRefusedError",
    "LispReadError",
    "Node",
    "Verdict",
    "assess",
    "check_expression",
    "parse_policy",
    "position",
    "read_forms",
    "symbol_tier",
]

#: ``LISP_POLICY`` values; the first is the default (parsed in ``config``,
#: which imports nothing of this module's).
POLICIES: tuple[str, ...] = LISP_POLICIES
DEFAULT_POLICY = LISP_POLICIES[0]

#: The ``policy`` a refusal names, per ``LISP_POLICY``.
POLICY_NAMES = {"read": "lisp_allowlist", "denylist": "lisp_denylist"}

#: Tiers. ``none``: no tier runs it, ever.
READ = "read"
WRITE = "write"
NEVER = "none"
UNCLASSIFIED = "unclassified"

#: The walk refuses anything nested deeper than this (a recursion guard, not a
#: style rule: no real read form comes near it).
MAX_DEPTH = 200


def parse_policy(raw: str | None) -> str:
    """A ``LISP_POLICY`` value; an unknown one falls back to ``read`` with a warning."""
    return parse_lisp_policy(raw)


# ── the vocabulary ──────────────────────────────────────────────────────────

#: Plain read-tier functions: every argument is walked as code.
READ_FUNCTIONS: frozenset[str] = frozenset(
    {
        # arithmetic, trig, geometry
        "+",
        "-",
        "*",
        "/",
        "~",
        "1+",
        "1-",
        "abs",
        "atan",
        "cos",
        "sin",
        "exp",
        "expt",
        "fix",
        "float",
        "gcd",
        "log",
        "logand",
        "logior",
        "lsh",
        "max",
        "min",
        "minusp",
        "rem",
        "sqrt",
        "zerop",
        "numberp",
        "angle",
        "distance",
        "polar",
        "inters",
        "trans",
        # comparison and logic
        "=",
        "/=",
        "<",
        "<=",
        ">",
        ">=",
        "eq",
        "equal",
        "not",
        "null",
        "atom",
        "listp",
        "vl-consp",
        "type",
        "and",
        "or",
        "if",
        "progn",
        # lists
        "list",
        "cons",
        "append",
        "assoc",
        "member",
        "nth",
        "last",
        "length",
        "reverse",
        "subst",
        "vl-position",
        "vl-remove",
        "vl-list*",
        "vl-list-length",
        # strings
        "strcat",
        "substr",
        "strlen",
        "strcase",
        "rtos",
        "itoa",
        "atoi",
        "atof",
        "angtos",
        "distof",
        "angtof",
        "chr",
        "ascii",
        "wcmatch",
        "vl-princ-to-string",
        "vl-prin1-to-string",
        # database reads
        "entget",
        "entnext",
        "entlast",
        "handent",
        "tblsearch",
        "tblnext",
        "tblobjname",
        "getvar",
        "dictsearch",
        "dictnext",
        "namedobjdict",
        "sslength",
        "ssname",
        "vlax-ename->vla-object",
        "vlax-vla-object->ename",
        "vlax-get-property",
        "vlax-variant-value",
        "vlax-safearray->list",
        "vlax-property-available-p",
        "vl-catch-all-error-p",
        "vl-catch-all-error-message",
        "vl-load-com",
    }
)

#: Read-tier prefixes: ActiveX property getters, curve queries, string functions.
READ_PREFIXES: tuple[str, ...] = ("vla-get-", "vlax-curve-get", "vl-string-")

#: Special forms the walk handles by shape (each in :meth:`_Walk._special`).
SPECIAL_FORMS: frozenset[str] = frozenset(
    {"quote", "function", "lambda", "setq", "foreach", "cond", "eval", "ssget"}
)

#: Higher-order functions -> the index of their function argument, which must
#: be a literal function (``'sym``, ``(function sym)``, a ``lambda``).
HIGHER_ORDER: dict[str, int] = {
    "mapcar": 0,
    "apply": 0,
    "vl-catch-all-apply": 0,
    "vl-some": 0,
    "vl-every": 0,
    "vl-member-if": 0,
    "vl-member-if-not": 0,
    "vl-remove-if": 0,
    "vl-remove-if-not": 0,
    "vl-sort": 1,
}

#: ``ssget`` modes that read without the operator.
READ_SSGET_MODES: frozenset[str] = frozenset({"X", "W", "C", "CP"})
#: ``ssget`` modes that do not prompt either but are outside the read tier.
OTHER_SSGET_MODES: frozenset[str] = frozenset({"WP", "F", "L", "P", "I", "A"})

#: Always refused, by exact name -> why.
NEVER_FUNCTIONS: dict[str, str] = {
    "entsel": "waits for the operator to pick, which holds AutoCAD's only COM thread",
    "nentsel": "waits for the operator to pick, which holds AutoCAD's only COM thread",
    "nentselp": "waits for the operator to pick, which holds AutoCAD's only COM thread",
    "grread": "waits for the operator's input, which holds AutoCAD's only COM thread",
    "load": "loads code from a file",
    "autoload": "loads code from a file",
    "arxload": "loads an ObjectARX module",
    "vl-load-all": "loads code into every document",
    "startapp": "starts a program",
    "read": "turns text into code",
    "defun": "defines a function",
    "defun-q": "defines a function",
    "vlax-create-object": "creates a COM server",
    "vlax-get-or-create-object": "creates a COM server",
    "vla-sendcommand": "sends text to the command line",
}

#: Always refused, by prefix -> why.
NEVER_PREFIXES: dict[str, str] = {
    "dos_": "runs DOSLib's shell and file functions",
    "vl-file-": "touches the file system",
    "vlr-": "attaches a reactor that runs code later",
    "c:": "runs a command defined in LISP",
}

#: ``get*`` functions that do not prompt (every other ``get*`` does).
GET_NOT_INTERACTIVE: frozenset[str] = frozenset({"getvar", "getenv", "getcfg", "getpropertyvalue"})

#: The write tier (1.8.1), by exact name.
WRITE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "command",
        "command-s",
        "vl-cmdf",
        "entmod",
        "entmake",
        "entmakex",
        "entdel",
        "entupd",
        "setvar",
        "setenv",
        "setcfg",
        "ssadd",
        "ssdel",
        "regapp",
        "dictadd",
        "dictremove",
        "dictrename",
        "setpropertyvalue",
        "vlax-put-property",
        "vlax-put",
        "vlax-invoke",
        "vlax-invoke-method",
    }
)

#: Symbols that are values, never variables to rebind.
CONSTANTS: frozenset[str] = frozenset({"nil", "t", "pi"})

#: AutoCAD command verbs that name the typed tools for a LISP symbol (the
#: refusal's ``alternatives`` go through ``discovery.aliases.tools_for_command``).
_ALTERNATIVE_VERBS: dict[str, tuple[str, ...]] = {
    "entdel": ("ERASE",),
    "vla-delete": ("ERASE",),
    "entmod": ("PROPERTIES",),
    "vlax-put-property": ("PROPERTIES",),
    "setvar": ("SETVAR",),
    "getpoint": ("ID",),
    "getcorner": ("ID",),
    "entsel": ("SELECT",),
    "nentsel": ("SELECT",),
    "nentselp": ("SELECT",),
    "ssget": ("SELECT",),
}

_CXR = re.compile(r"c[ad]{1,4}r")
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)")
_INTERACTIVE = "waits for the operator's input, which holds AutoCAD's only COM thread"


def symbol_tier(name: str) -> str:
    """The tier of a (lower-cased) symbol name: ``read``, ``none``, ``write``
    or ``unclassified``."""
    if name in NEVER_FUNCTIONS:
        return NEVER
    if (
        name in READ_FUNCTIONS
        or name in SPECIAL_FORMS
        or name in HIGHER_ORDER
        or _CXR.fullmatch(name)
        or name.startswith(READ_PREFIXES)
    ):
        return READ
    if name.startswith(tuple(NEVER_PREFIXES)):
        return NEVER
    if name.startswith("get") and name not in GET_NOT_INTERACTIVE:
        return NEVER
    if name in WRITE_FUNCTIONS or name.startswith("vla-"):
        return WRITE
    return UNCLASSIFIED


def _never_reason(name: str) -> str:
    if name in NEVER_FUNCTIONS:
        return NEVER_FUNCTIONS[name]
    for prefix, why in NEVER_PREFIXES.items():
        if name.startswith(prefix):
            return why
    return _INTERACTIVE


def alternative_verbs_for(symbol: str) -> tuple[str, ...]:
    if symbol.startswith("vla-put-"):
        return ("PROPERTIES",)
    return _ALTERNATIVE_VERBS.get(symbol, ())


# ── the reader ──────────────────────────────────────────────────────────────


class LispReadError(ValueError):
    """The text is not one AutoLISP reading: ``offset`` and ``token`` say where."""

    def __init__(self, reason: str, offset: int, token: str):
        super().__init__(reason)
        self.reason = reason
        self.offset = offset
        self.token = token


@dataclass(frozen=True)
class Node:
    """One datum. ``kind`` is ``list`` / ``symbol`` / ``number`` / ``string``.

    ``text`` is a symbol's lower-cased name, a number's spelling or a string's
    raw contents (escapes kept); ``tail`` is a dotted list's last datum;
    ``synthetic`` marks the ``quote`` symbol a ``'`` expands to.
    """

    kind: str
    offset: int
    end: int
    text: str = ""
    items: tuple[Node, ...] = ()
    tail: Node | None = None
    synthetic: bool = False


@dataclass
class _Frame:
    offset: int | None
    items: list = field(default_factory=list)
    quotes: list = field(default_factory=list)
    dot: int | None = None
    tail: Node | None = None


_DELIMITERS = frozenset(" \t\r\n()\"';")
#: Control characters other than tab, newline and carriage return.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")
_COMMENT_ODD = re.compile(r"[()\"]")


def read_forms(text: str) -> list[Node]:
    """Read every top-level datum of ``text``; raises :class:`LispReadError`.

    Iterative (no recursion), so depth never raises ``RecursionError``.
    """
    stack: list[_Frame] = [_Frame(None)]
    i, n = 0, len(text)
    control = _CONTROL.search(text)
    if control is not None:
        # The expression reaches AutoCAD as keystrokes (SendCommand): ESC
        # cancels the LISP input mid-expression and hands the rest of the
        # text to the command line, wherever it stands - a string included.
        raise LispReadError(
            "a control character (sent as a keystroke to AutoCAD's command line)",
            control.start(),
            control.group(0),
        )

    def emit(node: Node) -> None:
        frame = stack[-1]
        while frame.quotes:
            q = frame.quotes.pop()
            quote = Node("symbol", q, q + 1, "quote", synthetic=True)
            node = Node("list", q, node.end, items=(quote, node))
        if frame.dot is not None:
            if frame.tail is not None:
                raise LispReadError("more than one datum after '.'", node.offset, ".")
            frame.tail = node
        else:
            frame.items.append(node)

    while i < n:
        c = text[i]
        if c in " \t\r\n\f\v":
            i += 1
        elif c == ";":
            if i + 1 < n and text[i + 1] == "|":
                close = text.find("|;", i + 2)
                if close < 0:
                    raise LispReadError("unterminated ';|' comment", i, ";|")
                end = close + 2
            else:
                newline = text.find("\n", i)
                if newline < 0:
                    raise LispReadError(
                        "a ';' comment with no newline after it would swallow the "
                        "parentheses the server closes the expression with",
                        i,
                        ";",
                    )
                end = newline + 1
            # A reader that does not know comments (the command line's own
            # parenthesis count is not measured to) must find the same
            # balance: no parenthesis or string quote inside a comment.
            odd = _COMMENT_ODD.search(text, i, end)
            if odd is not None:
                raise LispReadError(
                    "a comment holding a parenthesis or a double quote reads differently "
                    "to a reader that does not skip comments",
                    odd.start(),
                    odd.group(0),
                )
            i = end
        elif c == "(":
            stack.append(_Frame(i))
            i += 1
        elif c == ")":
            frame = stack[-1]
            if frame.offset is None:
                raise LispReadError("unbalanced ')'", i, ")")
            if frame.quotes:
                raise LispReadError("a quote with nothing after it", frame.quotes[-1], "'")
            if frame.dot is not None and frame.tail is None:
                raise LispReadError("'.' with nothing after it", frame.dot, ".")
            stack.pop()
            emit(Node("list", frame.offset, i + 1, items=tuple(frame.items), tail=frame.tail))
            i += 1
        elif c == "'":
            frame = stack[-1]
            if frame.dot is not None and frame.tail is not None:
                raise LispReadError("more than one datum after '.'", i, "'")
            frame.quotes.append(i)
            i += 1
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            if j >= n:
                raise LispReadError("unterminated string", i, '"')
            emit(Node("string", i, j + 1, text[i + 1 : j]))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in _DELIMITERS:
                j += 1
            token = text[i:j]
            if token == ".":
                frame = stack[-1]
                if frame.offset is None:
                    raise LispReadError("'.' outside a list", i, ".")
                if not frame.items or frame.dot is not None or frame.quotes:
                    raise LispReadError("misplaced '.'", i, ".")
                frame.dot = i
            elif _NUMBER.fullmatch(token):
                emit(Node("number", i, j, token))
            else:
                emit(Node("symbol", i, j, token.lower()))
            i = j
    top = stack[-1]
    if len(stack) > 1:
        raise LispReadError("unclosed '('", top.offset, "(")
    if top.quotes:
        raise LispReadError("a quote with nothing after it", top.quotes[-1], "'")
    return list(top.items)


def position(text: str, offset: int) -> tuple[int, int]:
    """``(line, column)`` of ``offset``, both 1-based."""
    offset = max(0, min(offset, len(text)))
    line = text.count("\n", 0, offset) + 1
    column = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, column


# ── the walk ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """A symbol (or node) outside the read tier: ``tier`` is the one it needs."""

    symbol: str
    offset: int
    tier: str
    reason: str


@dataclass
class Verdict:
    """What the walk concluded. ``tier`` is ``read`` (every node approved),
    ``none`` (something no tier runs) or ``other`` (outside the read tier)."""

    tier: str
    findings: list[Finding]
    approved: list[tuple[int, int]]

    def first(self, tier: str | None = None) -> Finding | None:
        rows = [f for f in self.findings if tier is None or f.tier == tier]
        return min(rows, key=lambda f: f.offset) if rows else None

    def covers(self, hit: DenylistHit) -> bool:
        return any(start <= hit.start and hit.end <= end for start, end in self.approved)


class _Walk:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.approved: list[tuple[int, int]] = []

    # -- outcomes --

    def approve(self, node: Node) -> None:
        if node.kind == "symbol" and not node.synthetic:
            self.approved.append((node.offset, node.end))

    def never(self, node: Node, reason: str, symbol: str | None = None) -> None:
        name = symbol if symbol is not None else _label(node)
        self.findings.append(Finding(name, node.offset, NEVER, reason))

    def other(self, node: Node, tier: str, reason: str) -> None:
        self.findings.append(Finding(_label(node), node.offset, tier, reason))

    # -- positions --

    def code(self, node: Node, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.never(node, f"nested deeper than {MAX_DEPTH} levels")
            return
        if node.kind in ("number", "string"):
            return
        if node.kind == "symbol":
            self.value(node)
            return
        if node.kind != "list":  # pragma: no cover - the reader makes four kinds
            self.never(node, "a node the reader does not know")
            return
        if node.tail is not None:
            self.never(node, "a dotted pair evaluated as code")
            return
        if not node.items:
            return  # () is nil
        head, args = node.items[0], node.items[1:]
        if head.kind == "list":
            if _is_lambda(head):
                self.lambda_(head, depth + 1)
                self.walk_all(args, depth + 1)
            else:
                self.never(head, "a call whose head is a list other than a lambda")
            return
        if head.kind != "symbol":
            self.never(head, "a call whose head is not a symbol")
            return
        name = head.text
        tier = symbol_tier(name)
        if tier == NEVER:
            self.never(head, _never_reason(name))
            self.walk_all(args, depth + 1)
            return
        if tier != READ:
            reason = (
                "needs the write tier (1.8.1)"
                if tier == WRITE
                else "is not in the read tier (unclassified)"
            )
            self.other(head, tier, reason)
            self.walk_all(args, depth + 1)
            return
        if name in SPECIAL_FORMS:
            self.special(name, node, head, args, depth)
            return
        if name in HIGHER_ORDER:
            index = HIGHER_ORDER[name]
            if len(args) <= index:
                self.never(head, f"{name} without its function argument")
                return
            self.approve(head)
            for k, arg in enumerate(args):
                if k == index:
                    self.function_value(arg, head, depth + 1)
                else:
                    self.code(arg, depth + 1)
            return
        self.approve(head)
        self.walk_all(args, depth + 1)

    def walk_all(self, nodes, depth: int) -> None:
        for item in nodes:
            self.code(item, depth)

    def value(self, node: Node) -> None:
        """A symbol evaluated for its value (a variable, a constant, or a
        function named as data - aliasing ``(setq f command)``)."""
        name = node.text
        tier = symbol_tier(name)
        if tier == NEVER:
            self.never(node, _never_reason(name))
        elif tier == WRITE:
            self.other(node, WRITE, "names a write-tier function as a value")
        elif tier == UNCLASSIFIED and _DANGEROUS_LISP_PATTERN.fullmatch(name):
            self.other(node, UNCLASSIFIED, "names a restricted function as a value")
        else:
            self.approve(node)

    def data(self, node: Node, depth: int) -> None:
        """Quoted data: inert unless handed to code, so the same symbol rule."""
        if depth > MAX_DEPTH:
            self.never(node, f"nested deeper than {MAX_DEPTH} levels")
            return
        if node.kind == "symbol":
            if node.synthetic:
                return
            self.value(node)
        elif node.kind == "list":
            for item in node.items:
                self.data(item, depth + 1)
            if node.tail is not None:
                self.data(node.tail, depth + 1)

    def target(self, node: Node) -> None:
        """A symbol ``setq`` / ``foreach`` / a ``lambda`` binds."""
        if node.kind != "symbol" or node.synthetic:
            self.never(node, "binds something that is not a symbol")
            return
        name = node.text
        tier = symbol_tier(name)
        if tier == NEVER:
            self.never(node, _never_reason(name))
        elif (
            tier != UNCLASSIFIED
            or name in CONSTANTS
            or name == "*error*"
            or name.startswith("*push-error")
        ):
            self.other(node, WRITE, "rebinds a function or a protected symbol")
        elif _DANGEROUS_LISP_PATTERN.fullmatch(name):
            self.other(node, UNCLASSIFIED, "rebinds a restricted name")
        else:
            self.approve(node)

    def function_value(self, node: Node, owner: Node, depth: int) -> None:
        """The function argument of a higher-order function: a literal only."""
        if _is_quote_form(node) or _is_function_form(node):
            self.approve(node.items[0])
            self.function_ref(node.items[1], depth + 1)
        elif _is_lambda(node):
            self.lambda_(node, depth + 1)
        else:
            self.never(
                owner,
                f"{owner.text} with a function argument that is not a literal "
                "(quote a read-tier function, or pass a lambda)",
            )

    def function_ref(self, node: Node, depth: int) -> None:
        if node.kind == "symbol" and not node.synthetic:
            name = node.text
            tier = symbol_tier(name)
            if tier == READ and name not in SPECIAL_FORMS and name not in HIGHER_ORDER:
                self.approve(node)
            elif tier == READ:
                self.never(node, "a special form or a higher-order function passed as a value")
            elif tier == NEVER:
                self.never(node, _never_reason(name))
            else:
                reason = "needs the write tier (1.8.1)" if tier == WRITE else "is unclassified"
                self.other(node, tier, reason)
        elif _is_lambda(node):
            self.lambda_(node, depth)
        else:
            self.never(node, "not a function")

    def lambda_(self, node: Node, depth: int) -> None:
        items = node.items
        if node.tail is not None or len(items) < 2:
            self.never(node, "a malformed lambda")
            return
        self.approve(items[0])
        params = items[1]
        if params.kind == "symbol" and params.text == "nil":
            self.approve(params)
        elif params.kind == "list" and params.tail is None:
            for param in params.items:
                if param.kind == "symbol" and param.text == "/":
                    continue
                self.target(param)
        else:
            self.never(params, "a lambda parameter list that is not a list of symbols")
        self.walk_all(items[2:], depth + 1)

    def special(self, name: str, node: Node, head: Node, args, depth: int) -> None:
        if name == "quote":
            if len(args) != 1:
                self.never(head, "quote takes exactly one argument")
                return
            self.approve(head)
            self.data(args[0], depth + 1)
        elif name == "function":
            if len(args) != 1:
                self.never(head, "function takes exactly one argument")
                return
            self.approve(head)
            self.function_ref(args[0], depth + 1)
        elif name == "lambda":
            self.lambda_(node, depth)
        elif name == "setq":
            if not args or len(args) % 2:
                self.never(head, "setq takes symbol / value pairs")
                return
            self.approve(head)
            for k in range(0, len(args), 2):
                self.target(args[k])
                self.code(args[k + 1], depth + 1)
        elif name == "foreach":
            if len(args) < 2:
                self.never(head, "foreach takes a symbol, a list and a body")
                return
            self.approve(head)
            self.target(args[0])
            self.walk_all(args[1:], depth + 1)
        elif name == "cond":
            self.approve(head)
            for clause in args:
                if clause.kind != "list" or clause.tail is not None or not clause.items:
                    self.never(clause, "a cond clause that is not a list")
                    continue
                self.walk_all(clause.items, depth + 1)
        elif name == "eval":
            if len(args) == 1 and _is_quote_form(args[0]):
                self.approve(head)
                self.code(args[0].items[1], depth + 1)
            else:
                self.never(head, "eval of anything but a quoted literal turns data into code")
        elif name == "ssget":
            self.ssget(head, args, depth)
        else:  # pragma: no cover - SPECIAL_FORMS and this dispatch are one list
            self.never(head, "a special form the walk does not know")

    def ssget(self, head: Node, args, depth: int) -> None:
        if not args or args[0].kind != "string":
            self.never(head, "ssget without a literal mode string prompts the operator")
            self.walk_all(args, depth + 1)
            return
        mode = args[0].text.strip().upper().lstrip("_")
        if ":" in mode:
            self.never(head, "an interactive ssget mode prompts the operator")
        elif mode in READ_SSGET_MODES:
            self.approve(head)
        elif mode in OTHER_SSGET_MODES:
            self.other(head, UNCLASSIFIED, f"ssget mode {args[0].text!r} is not in the read tier")
        else:
            self.never(head, f"ssget mode {args[0].text!r} may prompt the operator")
        self.walk_all(args[1:], depth + 1)


def _label(node: Node) -> str:
    if node.kind == "symbol":
        return node.text
    if node.kind == "list" and node.items and node.items[0].kind == "symbol":
        return "'" if node.items[0].synthetic else node.items[0].text
    if node.kind == "list":
        return "("
    if node.kind == "string":
        return '"'
    return node.text or node.kind


def _is_quote_form(node: Node) -> bool:
    return (
        node.kind == "list"
        and node.tail is None
        and len(node.items) == 2
        and node.items[0].kind == "symbol"
        and node.items[0].text == "quote"
    )


def _is_function_form(node: Node) -> bool:
    return (
        node.kind == "list"
        and node.tail is None
        and len(node.items) == 2
        and node.items[0].kind == "symbol"
        and node.items[0].text == "function"
    )


def _is_lambda(node: Node) -> bool:
    return (
        node.kind == "list"
        and bool(node.items)
        and node.items[0].kind == "symbol"
        and node.items[0].text == "lambda"
    )


def assess(text: str) -> Verdict:
    """Walk ``text``'s forms (raises :class:`LispReadError` if it does not read)."""
    walk = _Walk()
    for form in read_forms(text):
        walk.code(form, 0)
    if any(f.tier == NEVER for f in walk.findings):
        tier = NEVER
    elif walk.findings:
        tier = "other"
    else:
        tier = READ
    return Verdict(tier, walk.findings, walk.approved)


# ── the refusal ─────────────────────────────────────────────────────────────


class LispPolicyRefusedError(CommandRefusedError):
    """``system_run_lisp`` refused an expression under ``LISP_POLICY`` (1.8-18).

    A :class:`security.CommandRefusedError` (kind ``policy_refused``, channel
    ``lisp``, ``verbs``, ``symbols``, ``alternatives``) that also says where:
    ``symbol``, ``line``, ``column``, the ``tier_needed`` and a ``reason`` code
    (``never_runs``, ``unreadable``, ``outside_read_tier``,
    ``denylist_second_pass``, ``denylist``).
    """

    def __init__(
        self,
        message: str,
        *,
        policy: str,
        symbol: str,
        line: int,
        column: int,
        tier_needed: str,
        reason: str,
        verbs=(),
        symbols=(),
        alternative_verbs=(),
    ):
        super().__init__(message, verbs=verbs, symbols=symbols, channel="lisp")
        self.policy = policy
        self.symbol = symbol
        self.line = int(line)
        self.column = int(column)
        self.tier_needed = tier_needed
        self.reason = reason
        merged: list[str] = []
        for verb in [*self.verbs, *alternative_verbs]:
            if verb not in merged:
                merged.append(verb)
        self._alternative_verbs = merged

    @property
    def alternative_verbs(self) -> list[str]:
        return list(self._alternative_verbs)

    def fields(self) -> dict:
        return {
            "policy": self.policy,
            "symbol": self.symbol,
            "line": self.line,
            "column": self.column,
            "tier_needed": self.tier_needed,
            "reason": self.reason,
        }


#: The placeholder a refusal sentence carries for "'symbol' at line L, column C".
WHERE = "{where}"

_DENYLIST_SENTENCE = (
    "LISP expression rejected: contains a restricted function. "
    "Restricted functions include startapp, command, load, eval, file I/O, etc. "
    "Set DANGEROUS_COMMANDS_ENABLED=true to allow all LISP expressions."
)


def _refuse(
    text: str,
    *,
    policy: str,
    symbol: str,
    offset: int,
    tier: str,
    reason: str,
    message: str,
    symbols,
    names=(),
) -> LispPolicyRefusedError:
    line, column = position(text, offset)
    alternatives: list[str] = []
    for name in (symbol, *names):
        alternatives.extend(alternative_verbs_for(name))
    return LispPolicyRefusedError(
        message.replace("{where}", f"{symbol!r} at line {line}, column {column}"),
        policy=policy,
        symbol=symbol,
        line=line,
        column=column,
        tier_needed=tier,
        reason=reason,
        verbs=lisp_command_verbs(text),
        symbols=sorted(set(symbols)),
        alternative_verbs=alternatives,
    )


def _hit_tier(hit: DenylistHit) -> str:
    return symbol_tier(hit.symbol)


def check_expression(expression: str, policy: str = DEFAULT_POLICY) -> str:
    """Return ``expression`` if ``policy`` lets it run; raise
    :class:`LispPolicyRefusedError` otherwise. The caller handles the empty
    string and ``DANGEROUS_COMMANDS_ENABLED``."""
    if policy == "off":
        return expression
    hits = lisp_denylist_matches(expression)
    if policy == "denylist":
        if hits:
            log.warning("Blocked dangerous LISP expression: %s", expression[:120])
            first = hits[0]
            raise _refuse(
                expression,
                policy=POLICY_NAMES["denylist"],
                symbol=first.symbol,
                offset=first.start,
                tier=_hit_tier(first),
                reason="denylist",
                message=_DENYLIST_SENTENCE,
                symbols=[hit.raw for hit in hits],
                names=[hit.symbol for hit in hits],
            )
        return expression

    name = POLICY_NAMES["read"]
    try:
        verdict = assess(expression)
    except LispReadError as exc:
        raise _refuse(
            expression,
            policy=name,
            symbol=exc.token,
            offset=exc.offset,
            tier=NEVER,
            reason="unreadable",
            message=f"LISP expression rejected: unreadable - {exc.reason} ({WHERE}). "
            "Nothing was sent to AutoCAD.",
            symbols=[hit.raw for hit in hits],
        ) from None

    if verdict.tier == NEVER:
        first = verdict.first(NEVER)
        log.warning("Refused LISP outside every tier (%s): %s", first.symbol, expression[:120])
        raise _refuse(
            expression,
            policy=name,
            symbol=first.symbol,
            offset=first.offset,
            tier=NEVER,
            reason="never_runs",
            message="LISP expression rejected: contains a restricted function. "
            f"{WHERE} {first.reason}; no LISP_POLICY tier runs it. Prefer a typed tool "
            "from `alternatives`.",
            symbols=[f.symbol for f in verdict.findings if f.tier == NEVER]
            + [hit.raw for hit in hits],
            names=[f.symbol for f in verdict.findings],
        )

    if verdict.tier == READ:
        stray = [hit for hit in hits if not verdict.covers(hit)]
        if not stray:
            return expression
        first = stray[0]
        log.warning("LISP denylist second pass refused %s: %s", first.symbol, expression[:120])
        raise _refuse(
            expression,
            policy=name,
            symbol=first.symbol,
            offset=first.start,
            tier=_hit_tier(first),
            reason="denylist_second_pass",
            message="LISP expression rejected: contains a restricted function. {where} "
            "is matched by the 1.7 denylist outside any symbol the reader approved "
            "(inside a comment, or a reading the two disagree on), so the read tier "
            "does not vouch for it.",
            symbols=[hit.raw for hit in hits],
        )

    # Outside the read tier: the 1.7 denylist decides, unchanged.
    if not hits:
        return expression
    first = verdict.first()
    log.warning("Blocked dangerous LISP expression: %s", expression[:120])
    raise _refuse(
        expression,
        policy=name,
        symbol=first.symbol,
        offset=first.offset,
        tier=first.tier,
        reason="outside_read_tier",
        message="LISP expression rejected: contains a restricted function. {where} "
        f"{first.reason}, so the expression went to the 1.7 denylist, which refuses "
        f"{', '.join(sorted({hit.symbol for hit in hits}))}. The read tier "
        "(LISP_POLICY=read) runs reads such as vla-get-*, getvar and entget; set "
        "DANGEROUS_COMMANDS_ENABLED=true to allow all LISP expressions.",
        symbols=[hit.raw for hit in hits],
        names=[f.symbol for f in verdict.findings] + [hit.symbol for hit in hits],
    )

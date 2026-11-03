"""What a QA run checked, and what it did not (1.8-16).

``drawing_critique`` answered with a list of issues, so "nothing found" and
"nothing looked" read the same: the live engine's dimension-legibility lane
returned a silent ``[]``, a paper layout no check could judge said nothing,
and a P&ID focus on a mechanical sheet was as quiet as a P&ID focus on a
clean P&ID. A silently skipped lane plus "0 issues" is a false success.

Every critique check now writes one or more rows into the run's
:class:`CoverageLedger` (kept in the run's ``shared`` dict under
:data:`LEDGER_KEY`):

* ``checked(focus, layout, n)`` - the check ran on ``layout`` and examined
  ``n`` items (entities, layers or settings; 0 is a real answer);
* ``not_checked(focus, layout, reason)`` - it did not, and ``reason`` is one
  of :data:`NOT_CHECKED_REASONS`, never free text.

``layout`` is a layout name ("Model", a paper tab) or :data:`DOCUMENT` (``*``)
for a check of the whole document (the layer table, a domain index).

``run_critique`` backs this with two rules: a check that raises writes
``check_failed``, and a check that returns without writing a row for its
focus gets ``check_unreported`` - a server bug made visible, never silence.

Consumers:

* ``drawing_critique(report=True)`` returns ``{issues, coverage: {layouts,
  complete}}`` (:meth:`CoverageLedger.report`);
* ``drawing_finalize`` / ``drawing_deliver``, their gate failure and the
  delivery manifest carry ``qa_coverage`` (:func:`qa_coverage_of`) - only the
  not-checked rows, the ``domain_absent`` ones folded into a list of names;
* ``strict_critique=True`` fails the gate on every not-checked row whose
  reason is not ``domain_absent`` (:func:`blocking_rows`).

:func:`summarize` is the compact form ``drawing_critique``,
``validation_check`` and ``drawing_topology_check`` return with
``summary=True`` or a ``limit``: ``{total, by_focus, by_severity,
issues[:limit]}``. There is no cursor, and ``total`` is always the full
count; the finalize gate never reads a summary, it holds on the full set.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "DOCUMENT",
    "GATE_EXEMPT_REASONS",
    "LEDGER_KEY",
    "NOT_CHECKED_REASONS",
    "SUMMARY_MESSAGE_CHARS",
    "SUMMARY_ROWS",
    "CoverageLedger",
    "CoverageRow",
    "blocking_rows",
    "domain_row",
    "ledger_of",
    "qa_coverage_of",
    "summarize",
]

#: The key a run's ledger is kept under in its ``shared`` dict.
LEDGER_KEY = "qa_coverage_ledger"

#: The ``layout`` of a row about the whole document rather than one layout.
DOCUMENT = "*"

#: Every reason a check may give for not checking. A closed vocabulary: an
#: agent can branch on it, and a reason nobody declared is refused.
NOT_CHECKED_REASONS: dict[str, str] = {
    "domain_absent": (
        "the drawing holds nothing this focus reads (no P&ID symbol, no ACADMCP_MECH "
        "or ACADMCP_ARCH record, no network layer, no feature control frame)"
    ),
    "engine_live_document_template": (
        "a live AutoCAD document carries the operator's own template; this lane reads "
        "the headless document's dimension settings only"
    ),
    "no_viewport_scale": (
        "a paper layout with content but no model viewport: there is no viewport scale "
        "to judge the model's lettering on that sheet at"
    ),
    "viewport_scale_deferred": (
        "lettering at a viewport's plot scale (text under 2.5 mm, arrow size) is checked "
        "from 1.8.1; until then the sheet's plotted text heights are not checked"
    ),
    "font_unresolved": (
        "the text style's font file could not be found, so its glyphs cannot be checked "
        "(reserved: the missing-glyph lane ships in 1.8.1)"
    ),
    "scan_incomplete": "the drawing was not read in full (MAX_QA_ENTITIES, or a live export "
    "whose per-layout counts differ from AutoCAD's)",
    "check_failed": "the check raised and checked nothing (its error issue says why)",
    "check_unreported": "the check wrote no coverage row - a server bug, reported, never silence",
}

#: Not-checked reasons ``strict_critique`` lets through: there was nothing to check.
GATE_EXEMPT_REASONS = frozenset({"domain_absent"})

#: Rows ``summary=True`` keeps when no ``limit`` is given.
SUMMARY_ROWS = 5
#: A summary row's message is cut to this many characters.
SUMMARY_MESSAGE_CHARS = 160
#: Handles a summary row keeps (``handle_count`` says how many there were).
SUMMARY_HANDLES = 5


@dataclass(frozen=True)
class CoverageRow:
    """One ledger row: ``status`` is ``checked`` (with ``n``) or ``not_checked``
    (with ``reason``); ``lane`` names a sub-check of a focus when it has several."""

    focus: str
    layout: str
    status: str
    n: int | None = None
    reason: str | None = None
    lane: str | None = None

    def to_dict(self) -> dict:
        row = {"focus": self.focus, "layout": self.layout, "status": self.status}
        if self.n is not None:
            row["n"] = self.n
        if self.reason is not None:
            row["reason"] = self.reason
        if self.lane is not None:
            row["lane"] = self.lane
        return row

    def compact(self) -> dict:
        """``{focus, layout, reason}`` (+ ``lane``): the not-checked row as the
        finalize payload and the gate carry it."""
        row = {"focus": self.focus, "layout": self.layout, "reason": self.reason}
        if self.lane is not None:
            row["lane"] = self.lane
        return row


class CoverageLedger:
    """The rows one QA run wrote, in the order they were written."""

    def __init__(self) -> None:
        self._rows: list[CoverageRow] = []

    def checked(self, focus: str, layout: str, n: int, *, lane: str | None = None) -> None:
        if int(n) < 0:
            raise ValueError(f"n must be >= 0, got {n!r}")
        self._rows.append(CoverageRow(str(focus), str(layout), "checked", n=int(n), lane=lane))

    def not_checked(self, focus: str, layout: str, reason: str, *, lane: str | None = None) -> None:
        if reason not in NOT_CHECKED_REASONS:
            raise ValueError(
                f"not_checked reason {reason!r} is not declared; one of "
                f"{sorted(NOT_CHECKED_REASONS)}"
            )
        self._rows.append(
            CoverageRow(str(focus), str(layout), "not_checked", reason=reason, lane=lane)
        )

    @property
    def rows(self) -> tuple[CoverageRow, ...]:
        return tuple(self._rows)

    def count(self, focus: str) -> int:
        """How many rows ``focus`` has written so far."""
        return sum(1 for row in self._rows if row.focus == focus)

    def focuses(self) -> set[str]:
        return {row.focus for row in self._rows}

    def not_checked_rows(self) -> list[CoverageRow]:
        return [row for row in self._rows if row.status == "not_checked"]

    def report(self, layouts: Sequence[str] = ()) -> dict:
        """``{layouts: [...], complete}`` - one entry per layout (``layouts``
        first, in that order, then any other layout a row names, then ``*``).

        Each entry: ``layout``, ``status`` (``checked`` when nothing blocking
        is missing, ``not_checked`` when no check ran on it, else ``partial``),
        ``checked`` (focus names), ``not_checked`` (``{focus, reason}`` rows
        that are not ``domain_absent``) and ``domain_absent`` (focus names).
        ``complete`` is true only when no row anywhere is a blocking
        not-checked row.
        """
        order = list(
            dict.fromkeys([*layouts, *(r.layout for r in self._rows if r.layout != DOCUMENT)])
        )
        if any(r.layout == DOCUMENT for r in self._rows):
            order.append(DOCUMENT)
        entries = []
        for name in order:
            rows = [r for r in self._rows if r.layout == name]
            checked = list(dict.fromkeys(r.focus for r in rows if r.status == "checked"))
            missing = [
                {"focus": r.focus, "reason": r.reason, **({"lane": r.lane} if r.lane else {})}
                for r in rows
                if r.status == "not_checked" and r.reason not in GATE_EXEMPT_REASONS
            ]
            absent = list(
                dict.fromkeys(
                    r.focus
                    for r in rows
                    if r.status == "not_checked" and r.reason == "domain_absent"
                )
            )
            if not missing:
                status = "checked"
            elif not checked:
                status = "not_checked"
            else:
                status = "partial"
            entries.append(
                {
                    "layout": name,
                    "status": status,
                    "checked": checked,
                    "not_checked": missing,
                    "domain_absent": absent,
                }
            )
        return {"layouts": entries, "complete": not blocking_rows(self)}


def ledger_of(shared: dict) -> CoverageLedger:
    """The run's ledger, created in ``shared`` on first use."""
    ledger = shared.get(LEDGER_KEY)
    if not isinstance(ledger, CoverageLedger):
        ledger = CoverageLedger()
        shared[LEDGER_KEY] = ledger
    return ledger


def domain_row(shared: dict, focus: str, *, present: bool, n: int) -> None:
    """The row of a domain focus (P&ID, mechanical, architectural, topology,
    GD&T): ``checked(focus, *, n)`` when the drawing holds its content, else
    ``not_checked(focus, *, domain_absent)``."""
    ledger = ledger_of(shared)
    if present:
        ledger.checked(focus, DOCUMENT, n)
    else:
        ledger.not_checked(focus, DOCUMENT, "domain_absent")


def blocking_rows(ledger: CoverageLedger | None) -> list[CoverageRow]:
    """The not-checked rows ``strict_critique`` fails on: every one but
    ``domain_absent``."""
    if ledger is None:
        return []
    return [row for row in ledger.not_checked_rows() if row.reason not in GATE_EXEMPT_REASONS]


def qa_coverage_of(shared: dict) -> dict:
    """The ``qa_coverage`` a finalize / delivery payload, its gate failure and
    the delivery manifest carry: the scan (``records``, ``spaces``, ``reason``
    of the run's one read; None when no check read the drawing), every
    blocking not-checked row as ``{focus, layout, reason}``, the
    ``domain_absent`` focus names, and ``complete`` - the scan covered the
    drawing and no lane is missing. Checked rows are left out: the payload
    says what was *not* looked at, which is the short list."""
    from engineering.qa_scan import QA_SNAPSHOT_KEY

    ledger = shared.get(LEDGER_KEY)
    ledger = ledger if isinstance(ledger, CoverageLedger) else None
    scan = (shared.get(QA_SNAPSHOT_KEY) or (None, None))[1]
    blocking = blocking_rows(ledger)
    absent = (
        sorted({r.focus for r in ledger.not_checked_rows() if r.reason in GATE_EXEMPT_REASONS})
        if ledger is not None
        else []
    )
    scan_complete = True if scan is None else bool(scan.complete)
    return {
        "complete": scan_complete and not blocking,
        "records": None if scan is None else scan.records,
        "spaces": None if scan is None else list(scan.spaces),
        "reason": None if scan is None else scan.reason,
        "not_checked": [row.compact() for row in blocking],
        "domain_absent": absent,
    }


def _compact_issue(row: dict, key: str) -> dict:
    out = {key: row.get(key), "severity": row.get("severity")}
    message = row.get("message")
    if message is not None:
        text = str(message)
        out["message"] = (
            text
            if len(text) <= SUMMARY_MESSAGE_CHARS
            else text[: SUMMARY_MESSAGE_CHARS - 3] + "..."
        )
    handles = row.get("handles")
    if handles is None and row.get("handle") is not None:
        handles = [row["handle"]]
    if handles:
        handles = [str(h) for h in handles]
        out["handles"] = handles[:SUMMARY_HANDLES]
        if len(handles) > SUMMARY_HANDLES:
            out["handle_count"] = len(handles)
    code = (row.get("detail") or {}).get("code") if isinstance(row.get("detail"), dict) else None
    if code:
        out["code"] = code
    return out


def summarize(
    rows: Iterable[dict],
    *,
    summary: bool,
    limit: int | None,
    key: str = "focus",
) -> dict:
    """``{total, by_focus, by_severity, issues}`` over every row of ``rows``.

    ``total``, ``by_focus`` (counted on ``key``: a critique's focus, a
    validation check's name, a topology finding kind) and ``by_severity``
    always count the full set; ``issues`` is the first ``limit`` rows. With
    ``summary`` each row is compacted (no ``detail``, the message cut to
    :data:`SUMMARY_MESSAGE_CHARS`, at most :data:`SUMMARY_HANDLES` handles)
    and ``limit`` defaults to :data:`SUMMARY_ROWS`; without it the rows are
    kept whole and ``limit`` None keeps all of them.
    """
    rows = list(rows)
    if limit is not None and int(limit) < 0:
        raise ValueError(f"limit must be >= 0, got {limit!r}")
    cap = SUMMARY_ROWS if (summary and limit is None) else limit
    kept = rows if cap is None else rows[: int(cap)]
    return {
        "total": len(rows),
        "by_focus": dict(Counter(str(row.get(key)) for row in rows)),
        "by_severity": dict(Counter(str(row.get("severity")) for row in rows)),
        "issues": [_compact_issue(row, key) for row in kept] if summary else kept,
    }

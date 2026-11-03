"""Exercise plan item 1.8-18 (the AutoLISP read tier) against live AutoCAD.

Runs on the live harness (scripts/live/harness.py): attach only, a NEW
document of its own (never the operator's drawings), closed on every path.
Every expression goes the way ``system_run_lisp`` sends it: the server's policy
check (``security.sanitize_lisp`` under ``LISP_POLICY=read``) first, then
``ComBackend.system_run_lisp``:

    entity_create_circle(r=5) / entity_create_polyline(40 x 30, closed) /
    entity_create_line(3-4-5 x 10)
    the CORPUS below, each form: policy -> AutoCAD -> CMDACTIVE read back
    five values cross-checked against the typed tools:
        vla-get-Area (circle)    vs entity_measure          (analysis_measure_entity)
        vla-get-Area (polyline)  vs entity_measure
        getvar CLAYER / INSUNITS / LTSCALE vs system_get_variable
    the broken-input twin (command "_ERASE" "_L" "") - refused before AutoCAD

Checks: every corpus form passes the read tier and runs in AutoCAD without an
error; no call leaves CMDACTIVE set; the five values agree (numbers to the six
significant digits ``vl-princ-to-string`` prints); the twin is refused with
symbol ``command``, line 1, column 2.

Exit codes are the harness's: 0 every check passed, 1 one failed or raised,
2 no running AutoCAD to attach to or the active document changed under it.

    uv run --frozen python scripts/smoke_lisp_com.py [--keep] [--json PATH]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.live.harness import LiveHarness, main_cli  # noqa: E402

#: Read forms an agent sends, run in AutoCAD. ``{circle}``, ``{pline}`` and
#: ``{line}`` are replaced with the handles the smoke drew.
CORPUS: tuple[str, ...] = (
    "(+ 1 2 3)",
    "(sqrt (+ (expt 3 2) (expt 4 2)))",
    "(angle (list 0 0) (list 10 10))",
    "(distance (list 0 0 0) (list 3 4 0))",
    "(polar (list 0 0) (/ pi 4) 10.0)",
    "(inters '(0 0) '(10 10) '(0 10) '(10 0))",
    "(trans '(1 2 0) 0 1)",
    "(mapcar (function (lambda (x) (* x 2))) (list 1 2 3))",
    "(mapcar '+ '(1 2) '(10 20))",
    "(apply '+ '(1 2 3))",
    "(eval '(+ 1 2))",
    "(foreach x '(1 2 3) (setq acadmcp-smoke-total (+ (if acadmcp-smoke-total "
    "acadmcp-smoke-total 0) x)))",
    '(cond ((= 1 2) "a") (t "b"))',
    '(strcat "A" "-" "101")',
    '(vl-string-subst "B" "A" "AAA")',
    "(rtos 12.3456 2 2)",
    '(getvar "CLAYER")',
    '(tblsearch "LAYER" "0")',
    '(cdr (assoc 0 (entget (handent "{circle}"))))',
    "(entget (entlast))",
    '(dictsearch (namedobjdict) "ACAD_LAYOUT")',
    '(sslength (ssget "_X" (list (cons 0 "CIRCLE"))))',
    "(ssget \"_W\" '(-10 -10) '(10 10))",
    "(ssget \"_C\" '(-1 -1) '(1 1))",
    '(ssget "_CP" \'((-10 -10) (60 -10) (60 60) (-10 60)))',
    '(vla-get-Area (vlax-ename->vla-object (handent "{circle}")))',
    '(vla-get-Layer (vlax-ename->vla-object (handent "{line}")))',
    '(vlax-get-property (vlax-ename->vla-object (handent "{line}")) \'Length)',
    '(vlax-curve-getArea (handent "{pline}"))',
    '(vlax-curve-getDistAtParam (handent "{line}") (vlax-curve-getEndParam (handent "{line}")))',
    "(vlax-safearray->list (vlax-variant-value "
    '(vla-get-Coordinates (vlax-ename->vla-object (handent "{pline}")))))',
    '(vl-catch-all-apply \'vla-get-Area (list (vlax-ename->vla-object (handent "{circle}"))))',
    "(progn\n  ; the area of the polyline\n  (vla-get-Area (vlax-ename->vla-object "
    '(handent "{pline}")))\n)',
)

#: The five values cross-checked against the typed tools.
CROSS: dict[str, str] = {
    "circle_area": '(vla-get-Area (vlax-ename->vla-object (handent "{circle}")))',
    "pline_area": '(vla-get-Area (vlax-ename->vla-object (handent "{pline}")))',
    "clayer": '(getvar "CLAYER")',
    "insunits": '(getvar "INSUNITS")',
    "ltscale": '(getvar "LTSCALE")',
}

TWIN = '(command "_ERASE" "_L" "")'

#: vl-princ-to-string prints six significant digits.
REL_TOL = 1e-5


def _fill(form: str, handles: dict) -> str:
    for key, handle in handles.items():
        form = form.replace("{" + key + "}", handle)
    return form


def _same(lisp_text, tool_value) -> bool:
    if isinstance(tool_value, (int, float)) and not isinstance(tool_value, bool):
        try:
            got = float(lisp_text)
        except (TypeError, ValueError):
            return False
        return abs(got - float(tool_value)) <= REL_TOL * max(1.0, abs(float(tool_value)))
    return str(lisp_text) == str(tool_value)


def assess(h: LiveHarness, report: dict) -> None:
    rows = report["corpus"]
    h.check(
        "every corpus form passes the read tier",
        bool(rows) and all(row["policy"] == "read" for row in rows),
        observed=[row for row in rows if row["policy"] != "read"],
    )
    h.check(
        "every corpus form ran in AutoCAD without an error",
        bool(rows) and all(row["ok"] for row in rows),
        observed=[row for row in rows if not row["ok"]],
    )
    h.check(
        "no call left CMDACTIVE set",
        all(int(row["cmdactive_after"] or 0) == 0 for row in rows)
        and int(report["cmdactive_end"] or 0) == 0,
        observed={
            "rows": [row["form"] for row in rows if int(row["cmdactive_after"] or 0)],
            "end": report["cmdactive_end"],
        },
    )
    for name, pair in report["cross"].items():
        h.check(
            f"{name}: the LISP read equals the typed tool's",
            _same(pair["lisp"], pair["tool"]),
            expected=pair["tool"],
            observed=pair["lisp"],
        )
    twin = report["twin"]
    h.check(
        "the LISP erase is refused before AutoCAD, with symbol, line and column",
        twin["refused"]
        and (twin["symbol"], twin["line"], twin["column"]) == ("command", 1, 2)
        and twin["tier_needed"] == "write",
        observed=twin,
    )


async def _run_one(backend, form: str) -> dict:
    from security import sanitize_lisp
    from security_lisp import assess as verdict_of

    row = {"form": form, "policy": verdict_of(form).tier, "ok": False, "result": None}
    try:
        sanitize_lisp(form)
        answer = await backend.system_run_lisp(form)
        row["ok"] = bool(answer.get("ok"))
        row["result"] = answer.get("result")
    except Exception as exc:  # recorded per form; the check names it
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["cmdactive_after"] = await backend.system_get_variable("CMDACTIVE")
    return row


async def body(h: LiveHarness) -> dict:
    import config
    from security_lisp import LispPolicyRefusedError

    config.settings.lisp_policy = "read"
    config.settings.dangerous_commands_enabled = False

    h.stage("drawing")
    await h.new_document()
    backend = h.backend
    circle = await backend.entity_create_circle(0.0, 0.0, 5.0)
    pline = await backend.entity_create_polyline(
        [[0.0, 0.0], [40.0, 0.0], [40.0, 30.0], [0.0, 30.0]], True
    )
    line = await backend.entity_create_line(0.0, 0.0, 30.0, 40.0)
    handles = {"circle": circle.handle, "pline": pline.handle, "line": line.handle}

    h.stage("corpus")
    report: dict = {"handles": handles, "corpus": []}
    for form in CORPUS:
        report["corpus"].append(await _run_one(backend, _fill(form, handles)))

    h.stage("cross")
    tools = {
        "circle_area": (await backend.entity_measure(circle.handle)).get("area"),
        "pline_area": (await backend.entity_measure(pline.handle)).get("area"),
        "clayer": await backend.system_get_variable("CLAYER"),
        "insunits": await backend.system_get_variable("INSUNITS"),
        "ltscale": await backend.system_get_variable("LTSCALE"),
    }
    report["cross"] = {}
    for name, form in CROSS.items():
        row = await _run_one(backend, _fill(form, handles))
        report["cross"][name] = {"lisp": row["result"], "tool": tools[name], "ok": row["ok"]}

    h.stage("twin")
    twin = {"refused": False, "symbol": None, "line": None, "column": None, "tier_needed": None}
    try:
        from security import sanitize_lisp

        sanitize_lisp(TWIN)
    except LispPolicyRefusedError as exc:
        twin = {
            "refused": True,
            "symbol": exc.symbol,
            "line": exc.line,
            "column": exc.column,
            "tier_needed": exc.tier_needed,
        }
    report["twin"] = twin
    report["cmdactive_end"] = await backend.system_get_variable("CMDACTIVE")

    h.stage("checks")
    assess(h, report)
    return report


if __name__ == "__main__":
    raise SystemExit(main_cli("lisp", body, description=__doc__.split("\n\n")[0]))

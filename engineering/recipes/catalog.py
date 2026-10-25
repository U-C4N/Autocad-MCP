"""Task recipes: versioned, checkable workflows over the registered tools (v1.7.0, B51).

A recipe is a JSON record under ``engineering/recipes/data/`` (the wheel ships
the whole ``engineering`` package, data included):

* ``id``, ``version`` (``x.y.z``, bumped whenever a step, an argument or a
  postcondition changes), ``title``, ``summary``;
* ``preconditions`` - the ``backends`` it is written for, the backend
  ``capabilities`` (``system_capabilities`` feature keys) it needs, the
  ``TOOL_PACKS`` packs and the ``TOOL_PROFILE`` profiles under which every tool
  it names is advertised, and ``notes`` a reader should know before running it;
* ``inputs`` - ``{name: {type, description, required?, default?, nullable?}}``;
  a step reads one as ``$inputs.<name>``;
* ``steps`` - ``cad_batch``'s own ``{tool, args, bind}`` shape plus ``id``,
  ``purpose`` and ``evidence`` (what the step leaves behind: a report, a file,
  a field to quote). ``$name`` / ``$name.path.0`` reference an earlier step's
  bound result exactly as in ``cad_batch``; ``$$`` is a literal dollar sign;
* ``postconditions`` - ``{id, ref, op, value?, tol?, says}``, each a
  comparison a script can check over the bound results (``value`` may itself
  be a reference);
* ``outputs`` - derived values (``op``: ``sub`` of two references, or ``ref``).

A bound result is the tool's structured content as an MCP client receives it:
a tool that returns a list is read as ``{"result": [...]}``.

Running one: :func:`instantiate` substitutes the inputs and returns steps a
client can hand to ``cad_batch`` (``verbose=True`` keeps whole results), or
:func:`run_recipe` drives any ``call(tool, args)`` step by step and checks the
postconditions. An input value is inserted as given, so a string inside it
that starts with ``$`` is a reference too - which is how a requirement names
the part a later step draws (``"part": "$part.part_id"``); write ``$$`` for a
literal dollar sign.

Nothing here imports the server: the catalog is plain data, and the tests
check it against the live registry (tool names, packs, profiles, JSON Schemas).
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "BACKENDS",
    "CATALOG_VERSION",
    "OPERATORS",
    "PACKS",
    "PROFILES",
    "RECIPE_IDS",
    "SCHEMA_VERSION",
    "RecipeError",
    "check_postconditions",
    "compute_outputs",
    "get_recipe",
    "instantiate",
    "list_recipes",
    "load_catalog",
    "recipe_index",
    "run_recipe",
    "validate_recipe",
]

#: The record layout this module reads. A change to a field's meaning bumps it.
SCHEMA_VERSION = 1
#: The catalog as a whole: bumped when a recipe is added, removed or re-versioned.
CATALOG_VERSION = "1.7.0"

#: The order the index lists them in; one file per id under ``data/``.
RECIPE_IDS: tuple[str, ...] = (
    "dimensioned_part_delivery",
    "foreign_pid_lists",
    "revision_takeoff_delta",
    "a3_sheet_delivery",
    "foreign_drawing_audit",
)

BACKENDS = ("ezdxf", "com")
#: ``server.TOOL_PACK_NAMES`` - restated so the catalog never imports the server;
#: tests/test_recipes.py holds the two equal.
PACKS = ("core", "pid", "settings", "mech", "arch", "plant")
PROFILES = ("full", "lean")
OPERATORS = (
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "approx",
    "exists",
    "len_eq",
    "len_ge",
    "file_exists",
)
OUTPUT_OPERATORS = ("sub", "ref")
INPUT_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}

_DATA = Path(__file__).resolve().parent / "data"
_SIGIL = "$"
_INPUTS = "inputs"
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_RESERVED_IDS = frozenset({"index"})

_TOP_KEYS = {
    "schema_version",
    "id",
    "version",
    "title",
    "summary",
    "preconditions",
    "inputs",
    "steps",
    "postconditions",
    "outputs",
}
_PRE_KEYS = {"backends", "capabilities", "packs", "profiles", "notes"}
_INPUT_KEYS = {"type", "description", "required", "default", "nullable"}
_STEP_KEYS = {"id", "tool", "args", "bind", "purpose", "evidence"}
_POST_KEYS = {"id", "ref", "op", "value", "tol", "says"}
_OUTPUT_KEYS = {"name", "op", "of", "says"}

_MISSING = object()


class RecipeError(ValueError):
    """A recipe that is malformed, unknown, or given inputs it does not declare."""


# ── references (cad_batch's syntax) ─────────────────────────────────────────


def _ref_name(value: Any) -> str | None:
    """The binding a whole-string reference names, or None (``$$`` escapes)."""
    if not isinstance(value, str) or not value.startswith(_SIGIL):
        return None
    if value.startswith(_SIGIL * 2) or len(value) == 1:
        return None
    return value[1:].split(".", 1)[0]


def _refs_in(value: Any, path: str, found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Every ``(path, reference)`` inside an argument tree."""
    if _ref_name(value) is not None:
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            _refs_in(item, f"{path}.{key}", found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refs_in(item, f"{path}[{index}]", found)
    return found


def _resolve(ref: str, bindings: dict[str, Any]) -> Any:
    """One reference against the bindings; ``_MISSING`` when it does not resolve.

    A bare ``$name`` is the bound result's ``handle`` when it is an object with
    one (``cad_batch``'s rule), else the whole result.
    """
    name, _, path = ref[1:].partition(".")
    if name not in bindings:
        return _MISSING
    current = bindings[name]
    if not path:
        if isinstance(current, dict) and "handle" in current:
            return current["handle"]
        return current
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.lstrip("-").isdigit():
            index = int(segment)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _substitute(value: Any, bindings: dict[str, Any], only: str | None = None) -> Any:
    """Replace whole-string references; with ``only``, just those to that binding.

    ``only`` keeps every other reference (and every ``$$`` escape) as written,
    so the instantiated steps are still valid ``cad_batch`` input.
    """
    if isinstance(value, str):
        name = _ref_name(value)
        if name is None or (only is not None and name != only):
            if only is None and value.startswith(_SIGIL * 2):
                return value[1:]
            return value
        resolved = _resolve(value, bindings)
        if resolved is _MISSING:
            raise RecipeError(f"{value!r} does not resolve against {sorted(bindings)}")
        return copy.deepcopy(resolved)
    if isinstance(value, dict):
        return {key: _substitute(item, bindings, only) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, bindings, only) for item in value]
    return value


# ── validation ──────────────────────────────────────────────────────────────


def _keys(obj: Any, where: str, required: set[str], allowed: set[str]) -> None:
    if not isinstance(obj, dict):
        raise RecipeError(f"{where}: expected an object, got {type(obj).__name__}")
    for key in sorted(required - set(obj)):
        raise RecipeError(f"{where}: missing {key!r}")
    for key in sorted(set(obj) - allowed):
        raise RecipeError(f"{where}: unknown key {key!r}")


def _text(obj: dict, key: str, where: str) -> None:
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        raise RecipeError(f"{where}.{key}: a non-empty string is required")


def _names(values: Any, allowed: tuple[str, ...], where: str) -> None:
    if not isinstance(values, list) or not values:
        raise RecipeError(f"{where}: a non-empty list is required")
    for value in values:
        if value not in allowed:
            raise RecipeError(f"{where}: {value!r} is not one of {list(allowed)}")


def _check_input_value(name: str, spec: dict, value: Any) -> None:
    if value is None and spec.get("nullable", False):
        return
    expected = INPUT_TYPES[spec["type"]]
    ok = isinstance(value, expected) and not (
        spec["type"] in ("number", "integer") and isinstance(value, bool)
    )
    if ok and spec["type"] == "number":
        ok = math.isfinite(float(value))
    if not ok:
        raise RecipeError(
            f"input {name!r}: expected a {spec['type']}, got {type(value).__name__} {value!r}"
        )


def _check_ref(ref: str, where: str, bound: set[str], inputs: dict) -> None:
    name = _ref_name(ref)
    if name == _INPUTS:
        field = ref[1:].split(".")[1] if "." in ref else ""
        if field not in inputs:
            raise RecipeError(f"{where}: {ref!r} names no declared input")
        return
    if name not in bound:
        raise RecipeError(
            f"{where}: {ref!r} references {name!r}, which no earlier step binds "
            f"(bound so far: {sorted(bound) or 'nothing'})"
        )


def validate_recipe(recipe: Any) -> None:
    """Refuse a malformed recipe, naming the path of the first problem."""
    _keys(recipe, "recipe", _TOP_KEYS - {"outputs"}, _TOP_KEYS)
    if recipe["schema_version"] != SCHEMA_VERSION:
        raise RecipeError(
            f"schema_version: {recipe['schema_version']!r} is not {SCHEMA_VERSION} "
            "(this module reads only its own layout)"
        )
    rid = recipe["id"]
    if not isinstance(rid, str) or not _NAME.match(rid):
        raise RecipeError(f"id: {rid!r} is not a lower-case identifier")
    if rid in _RESERVED_IDS:
        raise RecipeError(f"id: {rid!r} is reserved (autocad://recipes/{rid} is the index)")
    if not isinstance(recipe["version"], str) or not _VERSION.match(recipe["version"]):
        raise RecipeError(f"version: {recipe['version']!r} is not x.y.z")
    for key in ("title", "summary"):
        _text(recipe, key, "recipe")

    pre = recipe["preconditions"]
    _keys(pre, "preconditions", _PRE_KEYS - {"notes"}, _PRE_KEYS)
    _names(pre["backends"], BACKENDS, "preconditions.backends")
    _names(pre["packs"], PACKS, "preconditions.packs")
    if "core" not in pre["packs"]:
        raise RecipeError("preconditions.packs: 'core' is always on and must be listed")
    _names(pre["profiles"], PROFILES, "preconditions.profiles")
    caps = pre["capabilities"]
    if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
        raise RecipeError("preconditions.capabilities: a list of capability keys is required")
    notes = pre.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) and n.strip() for n in notes):
        raise RecipeError("preconditions.notes: a list of non-empty strings is required")

    inputs = recipe["inputs"]
    if not isinstance(inputs, dict):
        raise RecipeError("inputs: expected an object")
    for name, spec in inputs.items():
        where = f"inputs.{name}"
        if not _NAME.match(name):
            raise RecipeError(f"{where}: not a lower-case identifier")
        _keys(spec, where, {"type", "description"}, _INPUT_KEYS)
        if spec["type"] not in INPUT_TYPES:
            raise RecipeError(f"{where}.type: {spec['type']!r} is not one of {list(INPUT_TYPES)}")
        _text(spec, "description", where)
        for flag in ("required", "nullable"):
            if flag in spec and not isinstance(spec[flag], bool):
                raise RecipeError(f"{where}.{flag}: a boolean is required")
        if "default" in spec:
            _check_input_value(name, spec, spec["default"])

    steps = recipe["steps"]
    if not isinstance(steps, list) or not steps:
        raise RecipeError("steps: a non-empty list is required")
    bound: set[str] = set()
    used_inputs: set[str] = set()
    step_ids: set[str] = set()
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        _keys(step, where, _STEP_KEYS, _STEP_KEYS)
        for key in ("id", "tool", "purpose", "evidence"):
            _text(step, key, where)
        if step["id"] in step_ids:
            raise RecipeError(f"{where}.id: {step['id']!r} is used twice")
        step_ids.add(step["id"])
        if not isinstance(step["args"], dict):
            raise RecipeError(f"{where}.args: expected an object")
        bind = step["bind"]
        if not isinstance(bind, str) or not _NAME.match(bind):
            raise RecipeError(f"{where}.bind: {bind!r} is not a lower-case identifier")
        if bind == _INPUTS:
            raise RecipeError(f"{where}.bind: {bind!r} is reserved for the recipe's inputs")
        for path, ref in _refs_in(step["args"], f"{where}.args", []):
            _check_ref(ref, path, bound, inputs)
            if _ref_name(ref) == _INPUTS:
                used_inputs.add(ref[1:].split(".")[1])
        if bind in bound:
            raise RecipeError(f"{where}.bind: {bind!r} is bound twice")
        bound.add(bind)

    posts = recipe["postconditions"]
    if not isinstance(posts, list) or not posts:
        raise RecipeError("postconditions: a non-empty list is required")
    post_ids: set[str] = set()
    for index, post in enumerate(posts):
        where = f"postconditions[{index}]"
        _keys(post, where, {"id", "ref", "op", "says"}, _POST_KEYS)
        _text(post, "id", where)
        _text(post, "says", where)
        if post["id"] in post_ids:
            raise RecipeError(f"{where}.id: {post['id']!r} is used twice")
        post_ids.add(post["id"])
        if post["op"] not in OPERATORS:
            raise RecipeError(f"{where}.op: {post['op']!r} is not one of {list(OPERATORS)}")
        if post["op"] not in ("exists", "file_exists") and "value" not in post:
            raise RecipeError(f"{where}: op {post['op']!r} needs a 'value'")
        if "tol" in post and post["op"] != "approx":
            raise RecipeError(f"{where}.tol: only 'approx' reads a tolerance")
        if _ref_name(post["ref"]) is None:
            raise RecipeError(f"{where}.ref: {post['ref']!r} is not a reference")
        value = post.get("value")
        if _ref_name(value) is None and _refs_in(value, f"{where}.value", []):
            raise RecipeError(
                f"{where}.value: only a whole value may be a reference, not a part of one"
            )
        for path, ref in _refs_in({"ref": post["ref"], "value": post.get("value")}, where, []):
            _check_ref(ref, path, bound, inputs)

    for index, output in enumerate(recipe.get("outputs", [])):
        where = f"outputs[{index}]"
        _keys(output, where, _OUTPUT_KEYS, _OUTPUT_KEYS)
        _text(output, "says", where)
        if not isinstance(output["name"], str) or not _NAME.match(output["name"]):
            raise RecipeError(f"{where}.name: not a lower-case identifier")
        if output["op"] not in OUTPUT_OPERATORS:
            raise RecipeError(f"{where}.op: {output['op']!r} is not one of {OUTPUT_OPERATORS}")
        arity = {"sub": 2, "ref": 1}[output["op"]]
        if not isinstance(output["of"], list) or len(output["of"]) != arity:
            raise RecipeError(f"{where}.of: op {output['op']!r} takes {arity} reference(s)")
        for path, ref in _refs_in(output["of"], f"{where}.of", []):
            _check_ref(ref, path, bound, inputs)

    unused = sorted(set(inputs) - used_inputs)
    if unused:
        raise RecipeError(f"inputs: {unused} declared but no step reads them")


# ── the catalog ─────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict]:
    found = sorted(p.stem for p in _DATA.glob("*.json"))
    if found != sorted(RECIPE_IDS):
        raise RecipeError(f"recipe files {found} are not the catalog {list(RECIPE_IDS)}")
    catalog: dict[str, dict] = {}
    for rid in RECIPE_IDS:
        recipe = json.loads((_DATA / f"{rid}.json").read_text(encoding="utf-8"))
        validate_recipe(recipe)
        if recipe["id"] != rid:
            raise RecipeError(f"{rid}.json holds recipe {recipe['id']!r}")
        catalog[rid] = recipe
    return catalog


def load_catalog() -> dict[str, dict]:
    """Every recipe by id, validated, in index order (a deep copy)."""
    return copy.deepcopy(_catalog())


def list_recipes() -> list[dict]:
    return list(load_catalog().values())


def get_recipe(recipe_id: str) -> dict:
    """One recipe (a deep copy), or a refusal naming the known ids."""
    catalog = _catalog()
    if recipe_id not in catalog:
        raise RecipeError(f"unknown recipe {recipe_id!r}; known: {', '.join(RECIPE_IDS)}")
    return copy.deepcopy(catalog[recipe_id])


def recipe_index() -> dict:
    """What ``autocad://recipes/index`` serves: one row per recipe, and how to run one."""
    rows = []
    for recipe in _catalog().values():
        rows.append(
            {
                "id": recipe["id"],
                "version": recipe["version"],
                "title": recipe["title"],
                "summary": recipe["summary"],
                "uri": f"autocad://recipes/{recipe['id']}",
                "preconditions": copy.deepcopy(recipe["preconditions"]),
                "inputs": sorted(recipe["inputs"]),
                "tools": [step["tool"] for step in recipe["steps"]],
                "postconditions": len(recipe["postconditions"]),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "catalog_version": CATALOG_VERSION,
        "how_to_run": (
            "Read autocad://recipes/<id>. Replace every '$inputs.<name>' with your value, "
            "then either pass the steps' {tool, args, bind} to cad_batch (verbose=true keeps "
            "whole results) or call the tools in order. '$name.path' is an earlier step's "
            "bound result, cad_batch's syntax; '$$' is a literal dollar sign. Check each "
            "postcondition (ref op value) against the bound results before claiming the "
            "recipe's outcome."
        ),
        "recipes": rows,
    }


# ── inputs, instantiation, the run ──────────────────────────────────────────


def _resolve_inputs(recipe: dict, inputs: dict | None) -> dict:
    given = dict(inputs or {})
    declared = recipe["inputs"]
    for name in sorted(set(given) - set(declared)):
        raise RecipeError(f"unknown input {name!r}; {recipe['id']} declares {sorted(declared)}")
    resolved = {}
    for name, spec in declared.items():
        if name in given:
            value = given[name]
        elif "default" in spec:
            value = copy.deepcopy(spec["default"])
        elif spec.get("required", True):
            raise RecipeError(f"missing input {name!r}: {spec['description']}")
        else:
            continue
        _check_input_value(name, spec, value)
        resolved[name] = value
    return resolved


def instantiate(recipe: dict, inputs: dict | None = None) -> list[dict]:
    """The recipe's steps with its inputs in place: ``cad_batch``-ready ``{tool, args, bind}``."""
    validate_recipe(recipe)
    bindings = {_INPUTS: _resolve_inputs(recipe, inputs)}
    return [
        {
            "tool": step["tool"],
            "args": _substitute(step["args"], bindings, only=_INPUTS),
            "bind": step["bind"],
        }
        for step in recipe["steps"]
    ]


def _numbers_close(actual: Any, expected: Any, tol: float) -> bool:
    if isinstance(actual, (list, tuple)) and isinstance(expected, (list, tuple)):
        return len(actual) == len(expected) and all(
            _numbers_close(a, e, tol) for a, e in zip(actual, expected, strict=True)
        )
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) <= tol
    return False


def _compare(op: str, actual: Any, expected: Any, tol: float) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "gt":
            return actual > expected
        if op == "ge":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "le":
            return actual <= expected
        if op == "approx":
            return _numbers_close(actual, expected, tol)
        if op == "exists":
            return actual is not None
        if op == "len_eq":
            return len(actual) == expected
        if op == "len_ge":
            return len(actual) >= expected
        if op == "file_exists":
            return isinstance(actual, str) and os.path.isfile(actual)
    except TypeError:
        return False
    raise RecipeError(f"unknown operator {op!r}")


def check_postconditions(recipe: dict, bindings: dict[str, Any]) -> list[dict]:
    """Every postcondition over the bound results: ``{id, ok, actual, expected, says}``."""
    rows = []
    for post in recipe["postconditions"]:
        row = {"id": post["id"], "op": post["op"], "says": post["says"]}
        actual = _resolve(post["ref"], bindings)
        expected = post.get("value")
        if _ref_name(expected) is not None:
            expected = _resolve(expected, bindings)
        if actual is _MISSING:
            row.update(ok=False, actual=None, reason=f"{post['ref'][1:]} is not in the results")
        elif expected is _MISSING:
            row.update(ok=False, actual=actual, reason=f"{post['value']} is not in the results")
        else:
            ok = _compare(post["op"], actual, expected, float(post.get("tol", 0.0)))
            row.update(ok=bool(ok), actual=actual)
            if not ok:
                row["reason"] = f"{post['ref'][1:]} {post['op']} {expected!r} does not hold"
        if "value" in post:
            row["expected"] = None if expected is _MISSING else expected
        rows.append(row)
    return rows


def compute_outputs(recipe: dict, bindings: dict[str, Any]) -> dict[str, Any]:
    """The recipe's derived values; ``None`` for one whose references do not resolve."""
    outputs: dict[str, Any] = {}
    for output in recipe.get("outputs", []):
        values = [_resolve(ref, bindings) for ref in output["of"]]
        if any(value is _MISSING for value in values):
            outputs[output["name"]] = None
        elif output["op"] == "sub":
            left, right = values
            numeric = all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right)
            )
            outputs[output["name"]] = (left - right) if numeric else None
        else:
            outputs[output["name"]] = values[0]
    return outputs


async def run_recipe(
    recipe: dict,
    inputs: dict | None,
    call: Callable[[str, dict], Awaitable[Any]],
) -> dict:
    """Run every step through ``call(tool, args)``, then check the postconditions.

    ``call`` returns the tool's structured content (what ``cad_batch`` binds).
    The first step that raises stops the run; its error is reported and the
    postconditions are still checked over what was bound, so a failed run says
    which promises it could not keep.
    """
    steps = instantiate(recipe, inputs)
    # The steps already carry their inputs; only the step results are bound while
    # they run, so a reference left inside an input value resolves exactly as
    # cad_batch would resolve it. Postconditions may also read the inputs.
    bindings: dict[str, Any] = {}
    rows: list[dict] = []
    for template, step in zip(recipe["steps"], steps, strict=True):
        row = {"id": template["id"], "tool": step["tool"], "bind": step["bind"]}
        try:
            args = _substitute(step["args"], bindings)
            bindings[step["bind"]] = await call(step["tool"], args)
        except Exception as exc:  # the run reports a failure, it never hides one
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            break
        row["ok"] = True
        rows.append(row)
    bindings[_INPUTS] = _resolve_inputs(recipe, inputs)
    posts = check_postconditions(recipe, bindings)
    return {
        "recipe": recipe["id"],
        "version": recipe["version"],
        "ok": all(row["ok"] for row in rows)
        and len(rows) == len(steps)
        and all(post["ok"] for post in posts),
        "steps": rows,
        "postconditions": posts,
        "outputs": compute_outputs(recipe, bindings),
        "bindings": bindings,
    }

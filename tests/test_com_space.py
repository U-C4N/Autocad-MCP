"""1.8-05 on the live engine: the server's target space, an honest raw command, a verified undo.

§4.6 of the rival's report: a circle drawn through us landed in paper space.
``_msp()`` returned ``ActiveLayout.Block`` - whatever tab the operator had
clicked - so a session that never asked for a sheet drew on one. The bound
document now carries a target space (``"Model"`` by default); only
``layout_set_current`` moves it, and the operator's tab click does not.

``system_run_command`` returned ``ok: True`` without knowing whether the
command finished; it now waits on CMDACTIVE through ``_safe_send_command``,
reports what was created in which space, and a command left at a prompt is
``command_incomplete`` with AutoCAD's own ``last_prompt``.

No AutoCAD is touched: ``_run`` is the real one on a plain daemon thread (no
CoInitialize), as in tests/test_bound_document_com.py, and the seat models the
members the code reads - Layouts.Count / Item(i | name), Layout.Name / Block,
Block.Count / Item / ObjectID / AddCircle, Entity.Handle / ObjectName /
OwnerID, ActiveLayout, GetVariable (CMDACTIVE, LASTPROMPT), SendCommand.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("win32com.client", reason="pywin32 builds the point VARIANTs")

from fastmcp import Client  # noqa: E402

import config  # noqa: E402
import server  # noqa: E402
from backends import com_backend as cb  # noqa: E402
from backends.quarantine import DaemonExecutor  # noqa: E402
from engineering.errors import CommandIncompleteError, classify  # noqa: E402

# ── the fake seat ───────────────────────────────────────────────────────────


class _Entity:
    def __init__(self, block, handle: str, object_name: str, **geometry):
        self._block = block
        self.Handle = handle
        self.ObjectName = object_name
        self.Layer = "0"
        self.color = 256
        self.Linetype = "ByLayer"
        self.Visible = True
        for key, value in geometry.items():
            setattr(self, key, value)

    @property
    def OwnerID(self):
        return self._block.ObjectID

    def GetBoundingBox(self):
        return (0.0, 0.0, 0.0), (1.0, 1.0, 0.0)

    def Delete(self):
        self._block._items.remove(self)


class _Block:
    def __init__(self, doc, object_id: int):
        self._doc = doc
        self.ObjectID = object_id
        self._items: list[_Entity] = []

    @property
    def Count(self):
        return len(self._items)

    def Item(self, index):
        return self._items[index]

    def __iter__(self):
        return iter(list(self._items))

    def add(self, object_name: str, **geometry) -> _Entity:
        entity = _Entity(self, self._doc.next_handle(), object_name, **geometry)
        self._items.append(entity)
        return entity

    def AddCircle(self, center, radius):
        center = tuple(getattr(center, "value", center))
        return self.add("AcDbCircle", Center=center, Radius=float(radius))


class _Layout:
    def __init__(self, owner, doc, name: str, object_id: int):
        self._owner = owner
        self.Name = name
        self.Block = _Block(doc, object_id)

    def Delete(self):
        self._owner._layouts.remove(self)


class _Layouts:
    def __init__(self, doc, names):
        self._doc = doc
        self._layouts = [_Layout(self, doc, name, 1000 + i) for i, name in enumerate(names)]

    @property
    def Count(self):
        return len(self._layouts)

    def Item(self, key):
        if isinstance(key, int):
            return self._layouts[key]
        for layout in self._layouts:
            if layout.Name.lower() == str(key).lower():
                return layout
        raise KeyError(key)

    def by_name(self, name: str) -> _Layout:
        return self.Item(name)


class _Doc:
    def __init__(self, seat, name: str, full_name: str):
        self._seat = seat
        self.Name = name
        self.FullName = full_name
        self.Saved = True
        self._handle = 0x100
        self.vars = {
            "CMDACTIVE": 0,
            "OSMODE": 0,
            "SNAPMODE": 0,
            "CMDECHO": 1,
            "LASTPROMPT": "Command:",
        }
        self.sent: list[str] = []
        self.undo_marks: list[str] = []
        self.Layouts = _Layouts(self, ["Model", "Layout1", "A3"])
        self._active = self.Layouts.by_name("Model")
        #: ``on_command(doc, text)``: what AutoCAD does with a command string.
        self.on_command = None

    def next_handle(self) -> str:
        self._handle += 1
        return format(self._handle, "X")

    @property
    def ActiveLayout(self):
        return self._active

    @ActiveLayout.setter
    def ActiveLayout(self, layout):
        self._active = layout

    @property
    def ModelSpace(self):
        return self.Layouts.by_name("Model").Block

    @property
    def PaperSpace(self):
        if self._active.Name == "Model":
            return self.Layouts.by_name("Layout1").Block
        return self._active.Block

    def space(self, name: str) -> _Block:
        return self.Layouts.by_name(name).Block

    def GetVariable(self, name):
        return self.vars.get(name, 0)

    def SetVariable(self, name, value):
        self.vars[name] = value

    def SendCommand(self, text):
        self.sent.append(text)
        if text.startswith("\x1b"):
            self.vars["CMDACTIVE"] = 0
            self.vars["LASTPROMPT"] = "*Cancel*"
            return
        if self.on_command is not None:
            self.on_command(self, text)

    def HandleToObject(self, handle):
        for index in range(self.Layouts.Count):
            for entity in self.Layouts.Item(index).Block:
                if entity.Handle == handle:
                    return entity
        raise KeyError(handle)

    def Regen(self, _which):
        pass

    def StartUndoMark(self):
        self.undo_marks.append("start")

    def EndUndoMark(self):
        self.undo_marks.append("end")

    def Activate(self):
        self._seat.ActiveDocument = self


class _Docs:
    def __init__(self, seat):
        self._seat = seat
        self._docs: list[_Doc] = []

    @property
    def Count(self):
        return len(self._docs)

    def Item(self, index):
        return self._docs[index]

    def Open(self, path, read_only=False):
        doc = _Doc(self._seat, os.path.basename(str(path)), os.path.abspath(str(path)))
        self._docs.append(doc)
        self._seat.ActiveDocument = doc
        return doc

    def Add(self, template=None):
        doc = _Doc(self._seat, f"Drawing{len(self._docs) + 1}.dwg", "")
        self._docs.append(doc)
        self._seat.ActiveDocument = doc
        return doc


class _Seat:
    Version = "25.1s (test seat)"

    def __init__(self):
        self.Documents = _Docs(self)
        self.doc = _Doc(self, "Sheet.dwg", "C:\\work\\Sheet.dwg")
        self.Documents._docs.append(self.doc)
        self.ActiveDocument = self.doc


@pytest.fixture
def seat(monkeypatch):
    fake = _Seat()
    monkeypatch.setattr(cb, "_acad_app", lambda: fake)
    return fake


@pytest.fixture
def com(seat):
    backend = cb.ComBackend()
    backend._executor = DaemonExecutor(max_workers=1, name="test-sta")  # no CoInitialize
    backend._connected = True
    yield backend
    if backend._executor is not None:
        backend._executor.shutdown(wait=False)
        backend._executor = None


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(config.settings, "command_deadline_s", 0.3)


def _counts(doc: _Doc) -> dict[str, int]:
    return {
        doc.Layouts.Item(i).Name: doc.Layouts.Item(i).Block.Count for i in range(doc.Layouts.Count)
    }


# ── (1) the target space ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_create_lands_in_model_space_while_the_operator_has_layout1_active(com, seat):
    """§4.6: the operator's tab is Layout1; nothing asked for a sheet."""
    doc = seat.doc
    doc.ActiveLayout = doc.Layouts.by_name("Layout1")
    info = await com.entity_create_circle(0.0, 0.0, 5.0)
    assert _counts(doc) == {"Model": 1, "Layout1": 0, "A3": 0}
    assert "space" not in info.properties, "model space is the default and is not announced"
    assert doc.ActiveLayout.Name == "Layout1", "the operator's tab is left alone"


@pytest.mark.asyncio
async def test_layout_set_current_sets_the_target_and_still_activates_the_tab(com, seat):
    doc = seat.doc
    result = await com.layout_set_current("a3")
    assert result == {"ok": True, "current": "A3"}
    assert doc.ActiveLayout.Name == "A3"
    info = await com.entity_create_circle(1.0, 1.0, 2.0)
    assert _counts(doc)["A3"] == 1 and _counts(doc)["Model"] == 0
    assert info.properties["space"] == "A3", "a create outside Model names its space"

    doc.ActiveLayout = doc.Layouts.by_name("Model")  # the operator clicks the Model tab
    await com.entity_create_circle(2.0, 2.0, 2.0)
    assert _counts(doc)["A3"] == 2, "a tab click does not move where the session draws"


@pytest.mark.asyncio
async def test_an_unknown_layout_is_refused_and_keeps_the_target(com, seat):
    result = await com.layout_set_current("NoSuchSheet")
    assert result["ok"] is False and "NoSuchSheet" in result["error"]
    await com.entity_create_circle(0.0, 0.0, 1.0)
    assert _counts(seat.doc)["Model"] == 1


@pytest.mark.asyncio
async def test_document_activate_and_drawing_open_reset_the_target_to_model(com, seat, tmp_path):
    doc = seat.doc
    await com.layout_set_current("A3")
    await com.document_activate("Sheet.dwg")
    await com.entity_create_circle(0.0, 0.0, 1.0)
    assert _counts(doc) == {"Model": 1, "Layout1": 0, "A3": 0}

    await com.layout_set_current("A3")
    path = tmp_path / "Other.dwg"
    path.write_bytes(b"stand-in")
    await com.drawing_open(str(path))
    other = seat.ActiveDocument
    other.ActiveLayout = other.Layouts.by_name("Layout1")  # saved with Layout1 active
    await com.entity_create_circle(0.0, 0.0, 1.0)
    assert _counts(other) == {"Model": 1, "Layout1": 0, "A3": 0}


@pytest.mark.asyncio
async def test_drawing_new_starts_in_model_space(com, seat):
    await com.layout_set_current("A3")
    await com.drawing_new()
    fresh = seat.ActiveDocument
    fresh.ActiveLayout = fresh.Layouts.by_name("Layout1")
    await com.entity_create_circle(0.0, 0.0, 1.0)
    assert _counts(fresh)["Model"] == 1


@pytest.mark.asyncio
async def test_renaming_the_target_follows_it_and_deleting_it_falls_back_to_model(com, seat):
    doc = seat.doc
    await com.layout_set_current("A3")
    renamed = await com.layout_rename("A3", "Sheet-1")
    assert renamed["ok"] is True
    info = await com.entity_create_circle(0.0, 0.0, 1.0)
    assert info.properties["space"] == "Sheet-1"
    assert _counts(doc)["Sheet-1"] == 1

    deleted = await com.layout_delete("Sheet-1")
    assert deleted["ok"] is True
    info = await com.entity_create_circle(0.0, 0.0, 1.0)
    assert "space" not in info.properties
    assert _counts(doc)["Model"] == 1


# ── corner ops: WCS pick points ─────────────────────────────────────────────


class _Line(_Entity):
    """An AcDbLine whose points take the VARIANT ``_apoint`` builds, as ActiveX does."""

    def __init__(self, block, handle, start, end):
        super().__init__(block, handle, "AcDbLine")
        self.StartPoint, self.EndPoint = start, end

    def __setattr__(self, name, value):
        if name in ("StartPoint", "EndPoint"):
            value = tuple(float(v) for v in getattr(value, "value", value))
        object.__setattr__(self, name, value)

    @property
    def Length(self):
        (x1, y1, _), (x2, y2, _) = self.StartPoint, self.EndPoint
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5


def _line(doc, start, end) -> _Line:
    block = doc.ModelSpace
    line = _Line(block, doc.next_handle(), start, end)
    block._items.append(line)
    return line


@pytest.mark.asyncio
async def test_trim_and_extend_pick_points_carry_the_wcs_override(com, seat):
    """``*x,y`` is a WCS point whatever UCS is current: ucs_set cannot shift a pick.
    (A pair that is not LINE + LINE still goes through the command.)"""
    doc = seat.doc
    arc = doc.ModelSpace.add("AcDbArc", Center=(0, 0, 0), Radius=10.0)
    cutter = doc.ModelSpace.add("AcDbLine", StartPoint=(5, -5, 0), EndPoint=(5, 5, 0), Length=10)
    await com.entity_trim(arc.Handle, cutter.Handle, 12.0, 3.0)
    assert "\n*12.0,3.0\n" in doc.sent[-1]
    await com.entity_extend(arc.Handle, cutter.Handle, end_x=1.5, end_y=-2.0)
    assert "\n*1.5,-2.0\n" in doc.sent[-1]


@pytest.mark.asyncio
async def test_a_command_trim_runs_in_standard_mode_and_restores_the_operators(com, seat):
    """Live smoke ``space`` (AutoCAD 2026): under TRIMEXTENDMODE 1 (Quick, the
    seat's default) the script's cutter handent was taken as the object to trim
    and the cutter was erased. The command runs under 0; the operator's 1 is back
    afterwards."""
    doc = seat.doc
    doc.vars["TRIMEXTENDMODE"] = 1
    seen: list[int] = []
    doc.on_command = lambda d, text: seen.append(d.vars["TRIMEXTENDMODE"])
    arc = doc.ModelSpace.add("AcDbArc", Center=(0, 0, 0), Radius=10.0)
    cutter = doc.ModelSpace.add("AcDbLine", StartPoint=(5, -5, 0), EndPoint=(5, 5, 0), Length=10)
    await com.entity_trim(arc.Handle, cutter.Handle, 12.0, 3.0)
    await com.entity_extend(arc.Handle, cutter.Handle, end_x=1.5, end_y=-2.0)
    assert seen == [0, 0]
    assert doc.vars["TRIMEXTENDMODE"] == 1


def test_a_short_variable_is_retried_as_a_16_bit_variant():
    """MEASURED on AutoCAD 2026: SetVariable("TRIMEXTENDMODE", 0) raises
    ``Error setting system variable`` (a VT_I4); a VT_I2 VARIANT is accepted."""
    import pythoncom

    class _Strict:
        def __init__(self):
            self.value = None

        def SetVariable(self, name, value):  # noqa: N802
            if isinstance(value, int):
                raise RuntimeError("Error setting system variable")
            assert value.varianttype == pythoncom.VT_I2
            self.value = value.value

    doc = _Strict()
    cb._set_variable(doc, "TRIMEXTENDMODE", 0)
    assert doc.value == 0
    with pytest.raises(RuntimeError):
        cb._set_variable(doc, "TRIMEXTENDMODE", 70000)  # not a short: the error stands


@pytest.mark.asyncio
async def test_a_line_trim_keeps_the_side_of_the_point_without_a_command(com, seat):
    """Live smoke ``space`` (AutoCAD 2026): the ``_TRIM`` pick by screen point
    missed a line outside the view, opened a crossing window and left
    SendCommand blocked until a real ESC. Two lines are trimmed by the headless
    engine's geometry: in place, the handle kept, the segment holding the point
    kept (the contract; TRIM removes the picked side), nothing sent, the
    operator's tab untouched."""
    doc = seat.doc
    doc.ActiveLayout = doc.Layouts.by_name("Layout1")
    line = _line(doc, (0.0, 0.0, 0.0), (100.0, 0.0, 0.0))
    cutter = _line(doc, (50.0, -50.0, 0.0), (50.0, 50.0, 0.0))
    info = await com.entity_trim(line.Handle, cutter.Handle, 10.0, 0.0)
    assert info.handle == line.Handle
    assert line.StartPoint == (0.0, 0.0, 0.0) and line.EndPoint == (50.0, 0.0, 0.0)
    assert info.properties["length"] == pytest.approx(50.0)
    assert doc.sent == [] and doc.ActiveLayout.Name == "Layout1"

    other = _line(doc, (0.0, 10.0, 0.0), (100.0, 10.0, 0.0))
    await com.entity_trim(other.Handle, cutter.Handle, 90.0, 10.0)
    assert other.StartPoint == (50.0, 10.0, 0.0) and other.EndPoint == (100.0, 10.0, 0.0)

    parallel = _line(doc, (0.0, 20.0, 0.0), (10.0, 20.0, 0.0))
    with pytest.raises(RuntimeError, match="parallel"):
        await com.entity_trim(parallel.Handle, other.Handle, 0.0, 20.0)


@pytest.mark.asyncio
async def test_a_line_extend_moves_the_end_nearer_the_point_or_the_boundary_midpoint(com, seat):
    doc = seat.doc
    boundary = _line(doc, (50.0, -50.0, 0.0), (50.0, 50.0, 0.0))
    line = _line(doc, (0.0, 0.0, 0.0), (20.0, 0.0, 0.0))
    info = await com.entity_extend(line.Handle, boundary.Handle)
    assert info.handle == line.Handle
    assert line.StartPoint == (0.0, 0.0, 0.0) and line.EndPoint == (50.0, 0.0, 0.0)
    back = _line(doc, (60.0, 5.0, 0.0), (90.0, 5.0, 0.0))
    await com.entity_extend(back.Handle, boundary.Handle, end_x=61.0, end_y=5.0)
    assert back.StartPoint == (50.0, 5.0, 0.0) and back.EndPoint == (90.0, 5.0, 0.0)
    assert doc.sent == []


@pytest.mark.asyncio
async def test_a_corner_op_runs_in_the_target_tab_and_gives_the_operator_theirs_back(com, seat):
    """A command runs in the active space: with the operator on Layout1, a
    model-space FILLET would select nothing. The target's tab is active for the
    command and Layout1 is active again afterwards."""
    doc = seat.doc
    a = doc.ModelSpace.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(10, 0, 0), Length=10.0)
    b = doc.ModelSpace.add("AcDbLine", StartPoint=(0, 0, 0), EndPoint=(0, 10, 0), Length=10.0)
    doc.ActiveLayout = doc.Layouts.by_name("Layout1")
    seen: list[str] = []

    def fillet(d, text):
        seen.append(d.ActiveLayout.Name)
        d.ModelSpace.add("AcDbArc", Center=(2, 2, 0), Radius=2.0)

    doc.on_command = fillet
    info = await com.entity_fillet(a.Handle, b.Handle, 2.0)
    assert seen == ["Model"]
    assert info.type == "ARC"
    assert doc.ActiveLayout.Name == "Layout1"
    assert _counts(doc) == {"Model": 3, "Layout1": 0, "A3": 0}


# ── (2) the raw command ─────────────────────────────────────────────────────


def _draws_in(space: str, count: int = 1):
    def on_command(doc, text):
        for _ in range(count):
            doc.space(space).add("AcDbCircle", Center=(0, 0, 0), Radius=1.0)

    return on_command


@pytest.mark.asyncio
async def test_a_completed_command_reports_what_it_created_and_where(com, seat):
    doc = seat.doc
    doc.ModelSpace.add("AcDbLine")  # an older entity is never "created"
    doc.on_command = _draws_in("Model")
    result = await com.system_run_command("_CIRCLE 0,0 1")
    assert result["ok"] is True and result["completed"] is True
    assert result["command"] == "_CIRCLE 0,0 1"
    assert result["created_count"] == 1
    assert result["created"] == [
        {"handle": doc.ModelSpace.Item(1).Handle, "type": "CIRCLE", "space": "Model"}
    ]
    assert result["last_prompt"] == "Command:"
    assert "warning" not in result
    assert doc.sent == ["_CIRCLE 0,0 1\n"]


@pytest.mark.asyncio
async def test_a_paper_space_entity_while_the_target_is_model_is_a_warning(com, seat):
    doc = seat.doc
    doc.on_command = _draws_in("Layout1")
    result = await com.system_run_command("_CIRCLE 0,0 1")
    assert result["completed"] is True
    assert result["created"][0]["space"] == "Layout1"
    assert result["warning"] == "created_outside_target_space"
    assert result["target_space"] == "Model"


@pytest.mark.asyncio
async def test_the_created_list_is_capped_and_the_count_is_not(com, seat):
    seat.doc.on_command = _draws_in("Model", count=60)
    result = await com.system_run_command("_ARRAY")
    assert result["created_count"] == 60
    assert len(result["created"]) == 50


def _stops_at(prompt: str):
    def on_command(doc, text):
        doc.vars["CMDACTIVE"] = 1
        doc.vars["LASTPROMPT"] = prompt

    return on_command


@pytest.mark.asyncio
async def test_a_command_left_at_a_prompt_is_command_incomplete(com, seat, short_deadline):
    doc = seat.doc
    prompt = "Specify scale factor or [Copy/Reference]:"
    doc.on_command = _stops_at(prompt)
    with pytest.raises(CommandIncompleteError) as raised:
        await com.system_run_command("_SCALE")
    error = raised.value
    assert error.last_prompt == prompt, "the prompt it stopped at, read before the ESCs"
    assert error.command == "_SCALE"
    assert doc.sent[0] == "_SCALE\n" and doc.sent[1].startswith("\x1b\x1b\x1b")
    assert doc.vars["CMDACTIVE"] == 0, "ESC x3 left the seat idle"
    row = classify(error)
    assert row["kind"] == "command_incomplete"
    assert row["last_prompt"] == prompt and row["command"] == "_SCALE"


@pytest.mark.asyncio
async def test_command_incomplete_reaches_the_client_as_a_typed_envelope(
    com, seat, monkeypatch, short_deadline
):
    seat.doc.on_command = _stops_at("Specify base point:")

    async def make():
        return com

    monkeypatch.setattr(server, "_make_backend", make)
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "system_run_command", {"command": "_SCALE"}, raise_on_error=False
        )
    assert result.is_error is True
    payload = result.structured_content
    assert payload["kind"] == "command_incomplete"
    assert payload["last_prompt"] == "Specify base point:"
    assert payload["command"] == "_SCALE"
    assert payload["retryable"] is False, "the command was sent: something may have changed"


@pytest.mark.asyncio
async def test_transaction_begin_sets_its_mark_through_the_checked_sender(
    com, seat, short_deadline
):
    doc = seat.doc
    assert (await com.transaction_begin())["ok"] is True
    assert doc.sent == ["_.UNDO _M\n"] and doc.undo_marks == ["start"]
    await com.transaction_commit()

    doc.on_command = _stops_at("Enter the number of operations to undo:")
    with pytest.raises(CommandIncompleteError):
        await com.transaction_begin()
    assert com._transaction_active is False, "no mark exists: there is nothing to roll back to"
    assert doc.undo_marks == ["start", "end"], "no undo group was opened"


# ── undo / redo, verified ───────────────────────────────────────────────────


def _undoes_last_create(doc, text):
    if text.startswith("_UNDO"):
        doc.ModelSpace._items.pop()
    elif text.startswith("_REDO"):
        doc.ModelSpace.add("AcDbCircle", Center=(0, 0, 0), Radius=1.0)


@pytest.mark.asyncio
async def test_drawing_undo_and_redo_report_whether_the_drawing_changed(com, seat):
    doc = seat.doc
    await com.entity_create_circle(0.0, 0.0, 1.0)
    doc.on_command = _undoes_last_create
    undo = await com.drawing_undo()
    assert undo["ok"] is True and undo["verified"] is True
    assert undo["evidence"]["entities_before"] == 1 and undo["evidence"]["entities_after"] == 0
    assert doc.sent[-1] == "_UNDO 1\n"
    redo = await com.drawing_redo()
    assert redo["verified"] is True and redo["evidence"]["entities_after"] == 1

    doc.on_command = None  # AutoCAD had nothing to undo
    nothing = await com.drawing_undo()
    assert nothing["ok"] is True and nothing["verified"] is False
    assert "no change" in nothing["message"]


@pytest.mark.asyncio
async def test_undo_and_redo_touch_no_system_variable(com, seat):
    """Live smoke ``space`` (AutoCAD 2026): a SetVariable is an undo record of
    its own - ``_UNDO 1`` after the OSMODE zeroing undid that instead of the
    drawing's step, and the restore after it left ``_REDO`` "Nothing to redo"."""
    doc = seat.doc
    doc.vars["OSMODE"] = 4133
    await com.entity_create_circle(0.0, 0.0, 1.0)
    writes: list[tuple] = []
    plain = doc.SetVariable
    doc.SetVariable = lambda name, value: (writes.append((name, value)), plain(name, value))
    doc.on_command = _undoes_last_create
    assert (await com.drawing_undo())["verified"] is True
    assert (await com.drawing_redo())["verified"] is True
    assert writes == [] and doc.vars["OSMODE"] == 4133


@pytest.mark.asyncio
async def test_drawing_undo_left_at_a_prompt_is_command_incomplete(com, seat, short_deadline):
    seat.doc.on_command = _stops_at("Enter the number of operations to undo or [Auto]:")
    with pytest.raises(CommandIncompleteError) as raised:
        await com.drawing_undo()
    assert raised.value.last_prompt.startswith("Enter the number")

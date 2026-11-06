"""1.8-12 on the live engine: ``entity_modify_where``, ``layer_rename``, ``layer_merge``.

No AutoCAD is touched: ``_run`` is the real one on a plain daemon thread (no
CoInitialize), as in tests/test_com_space.py, and the seat models the members
the code reads - SelectionSets.Add / Select(acSelectionSetAll, filter) / Item /
Count / Delete, Layers.Count / Item / Add, AcadLayer.Name / Lock / Delete,
ActiveLayer, Layouts.Item(i).Name / Block.ObjectID, Blocks.Item(i).IsLayout /
IsXRef / Name / Item, and an entity's Handle / ObjectName / Layer / color /
Linetype / Lineweight / Visible / OwnerID / GetBoundingBox / HasAttributes /
GetAttributes. Which group codes AutoCAD 2026 honours in an ``ssget "X"``
filter (410 ``~Model``, 66) is not measured yet: scripts/smoke_settings_com.py
carries the live steps.

The seat behaves as AutoCAD does where it matters: a write to an entity on a
locked layer raises, assigning a missing layer raises ``Key not found``, and a
layer still referenced (or current) refuses ``Delete``.
"""

from __future__ import annotations

import importlib.util

import pytest

# pywin32 is the [com] extra (Windows only): without it every test here is
# skipped, not the module, so every CI lane collects the count the README states.
_PYWIN32 = importlib.util.find_spec("win32com") is not None

from backends import com_backend as cb  # noqa: E402
from backends.quarantine import DaemonExecutor  # noqa: E402
from engineering.changes.preview import StaleRevisionError  # noqa: E402

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _PYWIN32, reason="pywin32 builds the filter VARIANTs"),
]

_DXF = {
    "AcDbLine": "LINE",
    "AcDbCircle": "CIRCLE",
    "AcDbBlockReference": "INSERT",
    "AcDbAttribute": "ATTRIB",
}


def _wcmatch(pattern: str, value: str) -> bool:
    """AutoCAD's wcmatch for what the code sends: ``,`` alternatives, a leading
    ``~`` negation, backtick escapes, ``*`` and ``?``; case-insensitive."""
    import re

    negate = pattern.startswith("~")
    body = pattern[1:] if negate else pattern
    alternatives, current, escaped = [], "", False
    for char in body:
        if escaped:
            current += re.escape(char)
            escaped = False
        elif char == "`":
            escaped = True
        elif char == ",":
            alternatives.append(current)
            current = ""
        elif char == "*":
            current += ".*"
        elif char == "?":
            current += "."
        else:
            current += re.escape(char)
    alternatives.append(current)
    hit = any(re.fullmatch(a, str(value), re.IGNORECASE) for a in alternatives)
    return hit != negate


# ── the fake seat ───────────────────────────────────────────────────────────


class _Entity:
    def __init__(self, doc, owner, handle, object_name, layer="0", **props):
        self._doc = doc
        self._owner = owner
        self.Handle = handle
        self.ObjectName = object_name
        self._layer = layer
        self._props = {"color": 256, "Linetype": "ByLayer", "Lineweight": -1, "Visible": True}
        self._props.update(props)
        self._attributes: list[_Entity] = []
        self.box = ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))
        self.Name = props.get("Name", "")

    def _guard(self):
        if self._doc.layer(self._layer).Lock:
            raise RuntimeError("On locked layer")

    @property
    def Layer(self):
        return self._layer

    @Layer.setter
    def Layer(self, value):
        self._guard()
        self._layer = self._doc.layer(value).Name  # Key not found on a missing layer
        self._doc.writes.append((self.Handle, "Layer", self._layer))

    def __getattr__(self, name):
        props = self.__dict__.get("_props", {})
        if name in props:
            return props[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ("color", "Linetype", "Lineweight", "Visible"):
            self._guard()
            self._props[name] = value
            self._doc.writes.append((self.Handle, name, value))
            return
        object.__setattr__(self, name, value)

    @property
    def OwnerID(self):
        return self._owner.ObjectID

    @property
    def HasAttributes(self):
        return bool(self._attributes)

    def GetAttributes(self):
        return tuple(self._attributes)

    def GetBoundingBox(self):
        return self.box


class _Block:
    def __init__(self, name, object_id, *, layout=False):
        self.Name = name
        self.ObjectID = object_id
        self.IsLayout = layout
        self.IsXRef = False
        self.items: list[_Entity] = []

    @property
    def Count(self):
        return len(self.items)

    def Item(self, index):
        return self.items[index]


class _Layout:
    def __init__(self, name, block):
        self.Name = name
        self.Block = block


class _Collection:
    def __init__(self, items):
        self.items = items

    @property
    def Count(self):
        return len(self.items)

    def Item(self, key):
        if isinstance(key, int):
            return self.items[key]
        for item in self.items:
            if item.Name.lower() == str(key).lower():
                return item
        raise KeyError(f"Key not found: {key}")


class _Layer:
    def __init__(self, doc, name, lock=False):
        self._doc = doc
        self._name = name
        self.Lock = lock
        self.color = 7
        self.Linetype = "Continuous"
        self.Lineweight = -3
        self.LayerOn = True
        self.Freeze = False

    @property
    def Name(self):
        return self._name

    @Name.setter
    def Name(self, value):
        if any(o is not self and o.Name.lower() == value.lower() for o in self._doc.Layers.items):
            raise RuntimeError("Duplicate record name")
        self._doc.writes.append(("LAYER", self._name, value))
        self._name = value

    def Delete(self):
        if self._doc.ActiveLayer is self:
            raise RuntimeError("Object is the current layer")
        if any(e._layer.lower() == self._name.lower() for e in self._doc.every_entity()):
            raise RuntimeError("Object is referenced")
        self._doc.Layers.items.remove(self)
        self._doc.writes.append(("LAYER-DELETE", self._name))


class _Layers(_Collection):
    def __init__(self, doc):
        super().__init__([])
        self._doc = doc

    def Add(self, name):
        layer = _Layer(self._doc, name)
        self.items.append(layer)
        self._doc.writes.append(("LAYER-ADD", name))
        return layer


class _SelectionSet:
    def __init__(self, doc, name):
        self._doc = doc
        self.Name = name
        self._members: list[_Entity] = []

    def Select(self, mode, p1=None, p2=None, filter_types=None, filter_values=None):
        assert mode == 5, "acSelectionSetAll: every layout, never the screen"
        assert p1 is None and p2 is None
        codes = list(filter_types.value) if filter_types is not None else []
        values = list(filter_values.value) if filter_values is not None else []
        self._doc.selects.append(list(zip(codes, values, strict=True)))
        members = []
        for layout in self._doc.Layouts.items:
            for entity in layout.Block.items:
                if self._passes(entity, layout.Name, codes, values):
                    members.append(entity)
        self._members = members

    @staticmethod
    def _passes(entity, layout, codes, values):
        for code, value in zip(codes, values, strict=True):
            if code == 0 and not _wcmatch(value, _DXF[entity.ObjectName]):
                return False
            if code == 8 and not _wcmatch(value, entity.Layer):
                return False
            if code == 2 and not _wcmatch(value, entity.Name):
                return False
            if code == 62 and int(entity.color) != int(value):
                return False
            if code == 66 and not entity.HasAttributes:
                return False
            if code == 410 and not _wcmatch(value, layout):
                return False
        return True

    @property
    def Count(self):
        return len(self._members)

    def Item(self, index):
        return self._members[index]

    def Delete(self):
        self._doc.SelectionSets.entries.remove(self)


class _SelectionSets:
    def __init__(self, doc):
        self._doc = doc
        self.entries: list[_SelectionSet] = []

    def Add(self, name):
        selection = _SelectionSet(self._doc, name)
        self.entries.append(selection)
        return selection


class _Doc:
    def __init__(self, seat):
        self._seat = seat
        self.Name = "Plan.dwg"
        self.FullName = "C:\\work\\Plan.dwg"
        self.Saved = True
        self.writes: list = []
        self.selects: list = []
        self._handle = 0x100
        self.Layers = _Layers(self)
        for name in ("0", "Defpoints", "WALL_old", "Walls", "NOTES", "A-WALL"):
            self.Layers.items.append(_Layer(self, name))
        self.ActiveLayer = self.Layers.Item("0")
        model = _Block("*Model_Space", 1000, layout=True)
        paper = _Block("*Paper_Space", 1001, layout=True)
        self.door = _Block("DOOR", 1002)
        self.Layouts = _Collection([_Layout("Model", model), _Layout("Layout1", paper)])
        self.Blocks = _Collection([model, paper, self.door])
        self.SelectionSets = _SelectionSets(self)
        self.vars = {"CMDACTIVE": 0}

    def layer(self, name) -> _Layer:
        return self.Layers.Item(name)

    def add(self, block, object_name, layer="0", **props) -> _Entity:
        self._handle += 1
        entity = _Entity(self, block, format(self._handle, "X"), object_name, layer, **props)
        block.items.append(entity)
        return entity

    def every_entity(self):
        for block in self.Blocks.items:
            for entity in block.items:
                yield entity
                yield from entity._attributes

    @property
    def model(self):
        return self.Layouts.Item("Model").Block

    @property
    def paper(self):
        return self.Layouts.Item("Layout1").Block

    def GetVariable(self, name):
        return self.vars.get(name, 0)

    def Regen(self, _which):
        pass


class _Docs(_Collection):
    pass


class _Seat:
    Version = "25.1s (test seat)"

    def __init__(self):
        self.doc = _Doc(self)
        self.Documents = _Docs([self.doc])
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
def drawn(seat):
    doc = seat.doc
    walls = [doc.add(doc.model, "AcDbLine", "WALL_old", color=5, Lineweight=-3) for _ in range(3)]
    more = [doc.add(doc.model, "AcDbLine", "Walls") for _ in range(2)]
    notes = [doc.add(doc.model, "AcDbCircle", "NOTES") for _ in range(2)]
    sheet = doc.add(doc.paper, "AcDbLine", "WALL_old")
    return {"walls": walls, "more": more, "notes": notes, "sheet": sheet}


def _revision(com) -> int:
    return int(com.document_identity()["revision"] or 0)


# ── entity_modify_where ─────────────────────────────────────────────────────


async def test_one_filtered_selection_one_job_and_the_set_is_deleted(com, seat, drawn):
    doc = seat.doc
    await com.system_get_variable("CMDACTIVE")  # bind the document
    before = _revision(com)
    result = await com.entity_modify_where(
        {"layer": ["WALL*", "walls"]}, {"layer": "A-WALL", "color": 256, "lineweight": "ByLayer"}
    )
    assert doc.selects == [[(8, "WALL*,walls"), (410, "Model")]], "one ssget X: layer + space"
    assert doc.SelectionSets.entries == [], "the selection set is deleted again"
    assert result["matched"] == 5 and result["changed"] == 5
    assert result["handles"] == [e.Handle for e in drawn["walls"] + drawn["more"]]
    assert result["layers_created"] == []
    assert result["changes"]["counts"]["modified"] == 5 and result["changes"]["complete"]
    for entity in drawn["walls"] + drawn["more"]:
        assert entity.Layer == "A-WALL" and entity.color == 256 and entity.Lineweight == -1
    assert drawn["sheet"].Layer == "WALL_old"
    assert _revision(com) == before + 1, "@_mutating: one write job, one revision"


async def test_the_dry_run_reads_and_writes_nothing(com, seat, drawn):
    await com.system_get_variable("CMDACTIVE")
    before = _revision(com)
    result = await com.entity_select_where({"type": "CIRCLE"}, {"color": 1}, "all")
    assert seat.doc.selects == [[(0, "CIRCLE")]], "space=all adds no 410 filter"
    assert result["count"] == 2 and result["would_change"] == 2
    assert seat.doc.writes == [] and _revision(com) == before


async def test_bylayer_colour_is_checked_per_entity_and_aci_in_the_filter(com, seat, drawn):
    by_layer = await com.entity_select_where({"color": 256}, {"color": 1}, "model")
    assert seat.doc.selects[-1] == [(410, "Model")], "no group 62 for ByLayer"
    assert set(by_layer["handles"]) == {e.Handle for e in drawn["more"] + drawn["notes"]}
    blue = await com.entity_select_where({"color": 5}, {"color": 1}, "model")
    assert seat.doc.selects[-1] == [(62, 5), (410, "Model")]
    assert blue["handles"] == [e.Handle for e in drawn["walls"]]


async def test_type_block_lineweight_linetype_and_region_filters(com, seat, drawn):
    doc = seat.doc
    bolt = doc.add(doc.model, "AcDbBlockReference", "NOTES", Name="BOLT")
    dashed = doc.add(doc.model, "AcDbLine", "NOTES", Linetype="DASHED")
    far = doc.add(doc.model, "AcDbLine", "NOTES")
    far.box = ((500.0, 500.0, 0.0), (510.0, 510.0, 0.0))
    blocks = await com.entity_select_where({"block": "BO*"}, {"color": 1})
    assert doc.selects[-1] == [(0, "INSERT"), (2, "BO*"), (410, "Model")]
    assert blocks["handles"] == [bolt.Handle]
    lw = await com.entity_select_where({"lineweight": "Default"}, {"color": 1})
    assert lw["handles"] == [e.Handle for e in drawn["walls"]]
    lt = await com.entity_select_where({"linetype": "dashed"}, {"color": 1})
    assert lt["handles"] == [dashed.Handle]
    boxed = await com.entity_select_where({"region": [400, 400, 600, 600]}, {"color": 1})
    assert boxed["handles"] == [far.Handle]
    escaped = await com.entity_select_where({"layer": "A-WALL", "type": "LINE"}, {"color": 1})
    assert doc.selects[-1] == [(0, "LINE"), (8, "A`-WALL"), (410, "Model")]
    assert escaped["count"] == 0


async def test_paper_space_is_the_negated_model_filter(com, seat, drawn):
    result = await com.entity_select_where({"layer": "WALL_old"}, {"color": 1}, "paper")
    assert seat.doc.selects[-1] == [(8, "WALL_old"), (410, "~Model")]
    assert result["handles"] == [drawn["sheet"].Handle]


async def test_expect_count_mismatch_writes_nothing_and_creates_no_layer(com, seat, drawn):
    with pytest.raises(StaleRevisionError) as caught:
        await com.entity_modify_where(
            {"layer": "WALL_old"}, {"layer": "NEW-LAYER", "color": 1}, expect_count=9
        )
    assert caught.value.changed == [{"count": "where", "expected": 9, "actual": 3}]
    assert seat.doc.writes == [], "not even the layer"
    assert seat.doc.SelectionSets.entries == []


async def test_a_missing_set_layer_is_added_before_the_first_write(com, seat, drawn):
    result = await com.entity_modify_where({"layer": "NOTES"}, {"layer": "a-new"})
    assert result["layers_created"] == ["a-new"]
    assert seat.doc.writes[0] == ("LAYER-ADD", "a-new"), "ActiveX will not create one on assignment"
    assert all(e.Layer == "a-new" for e in drawn["notes"])


async def test_a_locked_layer_is_skipped_never_written(com, seat, drawn):
    seat.doc.layer("Walls").Lock = True
    result = await com.entity_modify_where({"layer": "W*"}, {"color": 2})
    assert result["matched"] == 3 and result["skipped_locked"] == {"count": 2, "layers": ["Walls"]}
    assert all(e.color == 256 for e in drawn["more"])


async def test_resetting_to_the_same_value_writes_nothing(com, seat, drawn):
    result = await com.entity_modify_where({"layer": "NOTES"}, {"color": 256, "layer": "notes"})
    assert result["changed"] == 0 and result["set"]["layer"] == "NOTES"
    assert seat.doc.writes == []


# ── layer_rename ────────────────────────────────────────────────────────────


async def test_rename_sets_the_layer_name_and_nothing_else(com, seat, drawn):
    result = await com.layer_rename("wall_old", "A-WALL-OLD")
    assert seat.doc.writes == [("LAYER", "WALL_old", "A-WALL-OLD")]
    assert result["old"] == "WALL_old" and result["new"] == "A-WALL-OLD"
    assert result["entities"] is None and result["changes"]["counts"]["layers_renamed"] == 1


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("0", "Z", "cannot be renamed"),
        ("defpoints", "Z", "cannot be renamed"),
        ("NOTES", "walls", "already exists"),
        ("GHOST", "Z", "no layer named 'GHOST'"),
    ],
)
async def test_rename_refusals_write_nothing(com, seat, drawn, old, new, needle):
    with pytest.raises(Exception, match=needle):
        await com.layer_rename(old, new)
    assert seat.doc.writes == []


# ── layer_merge ─────────────────────────────────────────────────────────────


async def test_merge_reaches_layouts_attributes_and_block_definitions(com, seat, drawn):
    doc = seat.doc
    member = doc.add(doc.door, "AcDbLine", "Walls")
    ref = doc.add(doc.paper, "AcDbBlockReference", "NOTES", Name="DOOR")
    attribute = _Entity(doc, doc.paper, "AA1", "AcDbAttribute", "WALL_old")
    ref._attributes.append(attribute)
    doc.layer("WALL_old").Lock = True
    doc.layer("A-WALL").Lock = True
    doc.ActiveLayer = doc.layer("Walls")

    result = await com.layer_merge(["WALL_old", "walls"], "a-wall")
    assert result["sources"] == ["WALL_old", "Walls"] and result["target"] == "A-WALL"
    assert result["reassigned"] == 8, "3 + 2 model, the sheet line, the attribute, the member"
    assert result["by_space"] == {"Model": 5, "Layout1": 2, "(block definitions)": 1}
    assert result["blocks_touched"] == ["DOOR"] and "every INSERT of DOOR" in result["note"]
    assert result["layers_deleted"] == ["WALL_old", "Walls"]
    assert result["layers_not_deleted"] == []
    assert result["current_layer"] == "A-WALL"
    assert member.Layer == attribute.Layer == drawn["sheet"].Layer == "A-WALL"
    assert doc.layer("A-WALL").Lock is True, "the target is relocked"
    assert [e.Layer for e in drawn["walls"]] == ["A-WALL"] * 3
    assert doc.selects == [[(8, "wall_old,walls")], [(0, "INSERT"), (66, 1)]]
    assert doc.SelectionSets.entries == []


async def test_a_source_autocad_will_not_delete_is_reported(com, seat, drawn, monkeypatch):
    def refuse(self):
        raise RuntimeError("Object is referenced by a viewport")

    monkeypatch.setattr(_Layer, "Delete", refuse)
    result = await com.layer_merge(["Walls"], "NOTES")
    assert result["ok"] is True and result["reassigned"] == 2
    assert result["layers_deleted"] == []
    assert result["layers_not_deleted"] == [
        {"layer": "Walls", "reason": "Object is referenced by a viewport"}
    ]


async def test_merge_refusals_write_nothing(com, seat, drawn):
    with pytest.raises(Exception, match="no layer named 'GHOST'"):
        await com.layer_merge(["GHOST"], "NOTES")
    with pytest.raises(Exception, match="cannot be merged away"):
        await com.layer_merge(["0"], "NOTES")
    with pytest.raises(Exception, match="no layer named 'NOPE'"):
        await com.layer_merge(["Walls"], "NOPE")
    assert seat.doc.writes == [] and seat.doc.selects == []


async def test_the_three_writers_are_mutating_and_the_dry_run_is_not():
    for name in ("entity_modify_where", "layer_rename", "layer_merge"):
        assert getattr(vars(cb.ComBackend)[name], "__acadmcp_mutating__", False), name
    assert not getattr(vars(cb.ComBackend)["entity_select_where"], "__acadmcp_mutating__", False)

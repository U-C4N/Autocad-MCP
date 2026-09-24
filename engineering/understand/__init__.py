"""Understanding drawings somebody else made (v1.6 track H).

One bulk read per drawing (:mod:`.snapshot`), multilingual vocabulary
(:mod:`.vocab`) and label parsers (:mod:`.labels`) are the foundation; the
readers built on them - the one-call report, the scale check, the line
networks and takeoffs, the diff and the topology checks - add their modules
here. Everything except the snapshot's live export is pure: records in,
results out, the drawing never modified.
"""

from engineering.understand.labels import (
    normalize_panel,
    parse_diameters,
    parse_electrical,
    parse_tag,
    plain,
    wiring_target,
)
from engineering.understand.snapshot import (
    EntityRecord,
    Pt,
    Snapshot,
    length_of,
    read_snapshot,
    records_from_doc,
    take_snapshot,
)
from engineering.understand.vocab import (
    DISCIPLINES,
    SERVICES,
    classify_layer,
    equipment_kind,
    room_label,
    supply_return,
)

__all__ = [
    "DISCIPLINES",
    "SERVICES",
    "EntityRecord",
    "Pt",
    "Snapshot",
    "classify_layer",
    "equipment_kind",
    "length_of",
    "normalize_panel",
    "parse_diameters",
    "parse_electrical",
    "parse_tag",
    "plain",
    "read_snapshot",
    "records_from_doc",
    "room_label",
    "supply_return",
    "take_snapshot",
    "wiring_target",
]

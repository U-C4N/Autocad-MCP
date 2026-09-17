"""Environment helpers (v1.6 track E): portable layer states, view and UCS
argument resolution. Pure data and validation — no ezdxf, no COM."""

from .layer_states import (  # noqa: F401
    DICT_NAME,
    PROPERTIES,
    decode_state,
    diff_snapshot,
    encode_state,
    snapshot_from_layers,
    validate_properties,
    validate_state_name,
)
from .names import validate_name  # noqa: F401
from .ucs import WORLD, resolve_ucs_axes  # noqa: F401
from .views import resolve_view_args  # noqa: F401

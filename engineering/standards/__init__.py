"""Authored drafting standards data.

Dimension, text and leader style presets live here (track E, group S); the
system-variable catalogue, paper sizes, plot styles and templates are sibling
modules added by their own groups and imported by path. Everything in this
package is data plus validation — no I/O, no ezdxf, no COM — so the backends,
the tools and the provenance tests read one copy.
"""

from __future__ import annotations

from engineering.standards.dimstyles import (
    ARROWHEAD_BLOCKS,
    DIM_VARIABLE_RANGES,
    DIM_VARIABLE_WHITELIST,
    PRESET_VARIABLES,
    PRESETS,
    canonical_arrowhead,
    check_dim_value,
    describe_preset,
    ezdxf_arrowhead,
    reported_arrowhead,
    resolve_dimstyle,
    validate_overrides,
)
from engineering.standards.mleaderstyles import MLEADER_PRESETS, resolve_mleaderstyle
from engineering.standards.textstyles import TEXT_PRESETS, resolve_font, validate_textstyle

__all__ = [
    "ARROWHEAD_BLOCKS",
    "DIM_VARIABLE_RANGES",
    "DIM_VARIABLE_WHITELIST",
    "MLEADER_PRESETS",
    "PRESETS",
    "PRESET_VARIABLES",
    "TEXT_PRESETS",
    "canonical_arrowhead",
    "check_dim_value",
    "describe_preset",
    "ezdxf_arrowhead",
    "reported_arrowhead",
    "resolve_dimstyle",
    "resolve_font",
    "resolve_mleaderstyle",
    "validate_overrides",
    "validate_textstyle",
]

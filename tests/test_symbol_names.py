"""SendCommand takes a whole macro, so a name interpolated into one is code.

`security.py` recorded the policy as "`sanitize_command` and `sanitize_lisp`
each have exactly ONE caller, both of them the COM-only free-text escape hatch
into SendCommand. The policy is scoped to the CHANNEL." That premise was false.

`_ensure_linetype_loaded` builds ``_-LINETYPE _LOAD {name} {lin_file}`` and
hands it to `doc.SendCommand`, and it is reached from the ``linetype=``
argument of every COM entity-creation and layer tool — plus
`ComBackend.linetype_load`, which interpolates a caller-supplied *file path*
as well. SendCommand treats a newline and ``;`` as segment separators, so a
linetype name carrying either injects further AutoCAD commands, and the 36-verb
denylist and `DANGEROUS_COMMANDS_ENABLED` never see that channel at all.

The guard here is the DXF symbol-name rule rather than a bespoke blocklist:
AutoCAD already forbids these characters in a table name, so anything this
rejects was never a loadable linetype, and the injection characters fall out of
the rule rather than having to be enumerated.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from security import sanitize_symbol_name


@pytest.mark.parametrize(
    "name",
    [
        "CENTER",
        "HIDDEN2",
        "DASHDOT",
        "ACAD_ISO02W100",
        "My-Linetype_01",
    ],
)
def test_ordinary_linetype_names_pass(name):
    assert sanitize_symbol_name(name, kind="linetype") == name


@pytest.mark.parametrize(
    "payload",
    [
        "CENTER\n_ERASE ALL\n",  # newline ends the macro segment
        "CENTER;_ERASE ALL",  # so does a semicolon
        "CENTER\r_QUIT",
        'CENTER"',
        "CENTER|X",
        "CENTER<X",
        "CENTER>X",
        "CENTER?X",
        "CENTER*X",
        "CENTER,X",
        "CENTER=X",
        "CENTER:X",
        "CENTER/X",
        "CENTER" + chr(92) + "X",
    ],
)
def test_a_name_that_could_end_the_macro_segment_is_refused(payload):
    with pytest.raises(ToolError) as excinfo:
        sanitize_symbol_name(payload, kind="linetype")

    assert "linetype" in str(excinfo.value).lower()


def test_an_empty_name_is_refused():
    with pytest.raises(ToolError):
        sanitize_symbol_name("   ", kind="linetype")


def test_the_refusal_does_not_echo_the_payload_back_verbatim():
    """An error message is a channel too."""
    with pytest.raises(ToolError) as excinfo:
        sanitize_symbol_name("CENTER\n_ERASE ALL\n", kind="linetype")

    assert "\n_ERASE" not in str(excinfo.value)


def test_the_com_linetype_loader_refuses_before_it_reaches_sendcommand():
    """The guard has to sit in front of the macro, not inside AutoCAD."""
    import backends.com_backend as cb

    with pytest.raises(ToolError):
        cb._ensure_linetype_loaded("CENTER\n_ERASE ALL\n")

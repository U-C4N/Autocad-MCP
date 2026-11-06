"""A member lookup AutoCAD refused while busy is a rejection, not a missing member.

MEASURED on AutoCAD 2026 (live smokes ``document`` / ``space``, 2026-11-04):
right after ``Documents.Close`` the first read of a member name on a proxy -
``app.Documents.Count`` - raised ``AttributeError: <unknown>.Count``. pywin32's
dynamic ``CDispatch.__getattr__`` swallows the ``RPC_E_CALL_REJECTED`` its
``GetIDsOfNames`` got, so no rejection handler saw it. ``com_backend`` wraps the
lookup: a refused one raises ``RejectedLookupError`` - an ``AttributeError``
and a ``com_error`` with the HRESULT - and a missing member stays what it was.

No AutoCAD is touched: the proxy is a real ``CDispatch`` around a fake
``IDispatch`` whose ``GetIDsOfNames`` answers what the test says.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# pywin32 is the [com] extra (Windows only): without it every test here is
# skipped, not the module, so every CI lane collects the count the README states.
try:
    import pywintypes
    from win32com.client import dynamic
except ImportError:
    pywintypes = dynamic = None

pytestmark = pytest.mark.skipif(dynamic is None, reason="the hook wraps pywin32's CDispatch")

from backends import com_backend as cb  # noqa: E402
from scripts.live.harness import RPC_E_CALL_REJECTED, hresult  # noqa: E402

DISP_E_UNKNOWNNAME = -2147352570


class _Dispatch:
    """The IDispatch under the proxy: name lookups answer ``answers`` in turn
    (an HRESULT is raised, ``None`` resolves the name to dispid 7), the last
    one repeating; ``Invoke`` of dispid 7 returns 3."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.lookups: list[str] = []

    def GetIDsOfNames(self, lcid, name):  # noqa: N802 - pywin32 passes (0, name)
        self.lookups.append(name)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if answer is None:
            return 7
        raise pywintypes.com_error(answer, "lookup refused", None, None)

    def Invoke(self, dispid, lcid, flags, result):  # noqa: N802
        assert dispid == 7
        return 3


def _proxy(*answers):
    proxy = dynamic.CDispatch.__new__(dynamic.CDispatch)
    olerepr = SimpleNamespace(mapFuncs={}, propMap={}, propMapGet={})
    for key, value in {
        "_oleobj_": _Dispatch(*answers),
        "_olerepr_": olerepr,
        "_username_": "<unknown>",
        "_lazydata_": None,
        "_mapCachedItems_": {},
        "_builtMethods_": {},
        "_enum_": None,
        "_unicode_to_string_": None,
    }.items():
        proxy.__dict__[key] = value
    return proxy


def test_the_hook_is_installed():
    assert getattr(dynamic.CDispatch.__getattr__, "_acadmcp_rejection_aware", False) is True


def test_a_refused_lookup_raises_a_rejection_the_retry_handlers_see(monkeypatch):
    """Once its patience is spent (here none) a refusal reaches the caller."""
    monkeypatch.setattr(cb, "_LOOKUP_PATIENCE_S", 0.0)
    proxy = _proxy(RPC_E_CALL_REJECTED)
    with pytest.raises(cb.RejectedLookupError) as raised:
        proxy.Count  # noqa: B018 - the read is the test
    error = raised.value
    assert isinstance(error, AttributeError) and isinstance(error, pywintypes.com_error)
    assert error.args[0] == RPC_E_CALL_REJECTED
    assert hresult(error) == RPC_E_CALL_REJECTED, "the live harness retries it"
    assert hasattr(_proxy(RPC_E_CALL_REJECTED), "Count") is False, "hasattr is unchanged"


def test_a_lookup_refused_once_then_answered_reads_the_value():
    """What the ``document`` smoke hit: the lookup after Close was refused and
    the next one answered - the read returns the value instead of
    ``AttributeError: <unknown>.Count``."""
    proxy = _proxy(RPC_E_CALL_REJECTED, None)
    assert proxy.Count == 3
    assert proxy.__dict__["_oleobj_"].lookups == ["Count", "Count", "Count"]


def test_a_lookup_refused_for_a_while_is_waited_out(monkeypatch):
    """``benchmarks/perf_com.py`` (AutoCAD 2026): the ``SendCommand`` lookup of
    one ``entity_fillet`` was refused right after the previous FILLET. A lookup
    changes nothing, so the hook repeats it while it answers busy."""
    sleeps: list[float] = []
    monkeypatch.setattr(cb.time, "sleep", sleeps.append)
    proxy = _proxy(RPC_E_CALL_REJECTED, RPC_E_CALL_REJECTED, RPC_E_CALL_REJECTED, None)
    assert proxy.SendCommand == 3
    assert sleeps == [0.05, 0.1], "two pauses for the hook's two refused lookups"


def test_a_missing_member_stays_an_attribute_error():
    proxy = _proxy(DISP_E_UNKNOWNNAME)
    with pytest.raises(AttributeError) as raised:
        proxy.NoSuchMember  # noqa: B018
    assert not isinstance(raised.value, pywintypes.com_error)


def test_a_rejected_read_is_waited_out_by_the_read_only_helper():
    """``_wait_out_rejected_call`` retries a com_error RPC_E_CALL_REJECTED; the
    lookup refusal is one now, so a read right after a document switch waits."""
    answers = [cb.RejectedLookupError(RPC_E_CALL_REJECTED, "Count"), 3]

    def read():
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    assert cb._wait_out_rejected_call(read, budget_s=2.0, first_pause_s=0.01) == 3

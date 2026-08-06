"""Read-only probe for README claim C131 (asyncio.wait_for attribution).

Interleaved A/B/C/D so warm-up and machine drift hit every variant equally.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time

os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")

import config  # noqa: E402
from backends.ezdxf_backend import EzdxfBackend  # noqa: E402

N = 2000
REPS = 5

SHIPPED = EzdxfBackend._async


async def build(backend: EzdxfBackend) -> float:
    await backend.drawing_new()
    started = time.perf_counter()
    for index in range(N):
        y = float(index % 100)
        await backend.entity_create_line(0.0, y, 100.0, y + 1.0)
    return (time.perf_counter() - started) * 1000.0


async def async_no_timeout(self, func, *args, **kwargs):
    """The shipped timeout<=0 branch: plain lock + to_thread, no wait_for."""
    async with self._lock:
        return await asyncio.to_thread(func, *args, **kwargs)


async def async_task_no_waitfor(self, func, *args, **kwargs):
    """Shipped timeout branch MINUS both wait_for wrappers.

    Same lock acquire, same loop.time() bookkeeping, same ensure_future Task
    around to_thread, same try/except/release -- only the two asyncio.wait_for
    wrappers removed.
    """
    timeout = config.settings.ezdxf_call_timeout
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    _ = max(deadline - loop.time(), 0.0)
    lock = self._lock
    await lock.acquire()
    call = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        result = await call
    except BaseException:
        lock.release()
        raise
    lock.release()
    return result


async def async_waitfor_only(self, func, *args, **kwargs):
    """Naive shape: plain `async with lock` + a single wait_for on the coroutine.

    This is what "wrap every call in asyncio.wait_for" would look like if you
    wrote it the obvious way -- wait_for does its own ensure_future internally.
    """
    timeout = config.settings.ezdxf_call_timeout
    async with self._lock:
        return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout)


VARIANTS = [
    ("A shipped, EZDXF_CALL_TIMEOUT=120", SHIPPED, 120.0),
    ("B shipped, EZDXF_CALL_TIMEOUT=0  ", SHIPPED, 0.0),
    ("C timeout branch minus wait_for  ", async_task_no_waitfor, 120.0),
    ("D plain lock + to_thread         ", async_no_timeout, 120.0),
    ("E naive single wait_for          ", async_waitfor_only, 120.0),
]


async def one(impl, timeout):
    EzdxfBackend._async = impl
    config.settings.ezdxf_call_timeout = timeout
    backend = EzdxfBackend()
    await backend.connect()
    try:
        return await build(backend)
    finally:
        EzdxfBackend._async = SHIPPED


async def main():
    print("python", os.sys.version.split()[0])
    print("default EZDXF_CALL_TIMEOUT =", config.settings.ezdxf_call_timeout)
    await one(SHIPPED, 120.0)  # warm-up, discarded
    samples = {label: [] for label, _, _ in VARIANTS}
    for _ in range(REPS):
        for label, impl, timeout in VARIANTS:
            samples[label].append(await one(impl, timeout))
    meds = {}
    for label, _, _ in VARIANTS:
        vals = samples[label]
        meds[label] = statistics.median(vals)
        print(f"{label}  median {meds[label]:8.1f} ms  runs "
              + " ".join(f"{v:.0f}" for v in vals))
    a = meds["A shipped, EZDXF_CALL_TIMEOUT=120"]
    b = meds["B shipped, EZDXF_CALL_TIMEOUT=0  "]
    c = meds["C timeout branch minus wait_for  "]
    d = meds["D plain lock + to_thread         "]
    e = meds["E naive single wait_for          "]
    print()
    print(f"naive wait_for over baseline (E-D)  : {e - d:7.1f} ms  ({(e/d-1)*100:5.1f}%)")
    print(f"total timeout-branch overhead (A-B) : {a - b:7.1f} ms  ({(a/b-1)*100:5.1f}%)")
    print(f"overhead with wait_for removed (C-D): {c - d:7.1f} ms")
    print(f"overhead attributable to wait_for   : {a - c:7.1f} ms")
    if a - b > 0:
        print(f"wait_for share of the A-B delta     : {(a - c)/(a - b)*100:5.0f}%")


asyncio.run(main())

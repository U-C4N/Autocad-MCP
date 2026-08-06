"""Micro-attribution for README claim C131: what does the timeout path cost?

Same four `_async` shapes, but the payload is a no-op so the delta is pure
async machinery. Interleaved, median of many reps.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time

os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")

import config  # noqa: E402
from backends.ezdxf_backend import EzdxfBackend  # noqa: E402

CALLS = 3000
REPS = 7
SHIPPED = EzdxfBackend._async


def noop():
    return None


async def shipped_off(self, func, *a, **k):
    async with self._lock:
        return await asyncio.to_thread(func, *a, **k)


async def task_no_waitfor(self, func, *a, **k):
    timeout = config.settings.ezdxf_call_timeout
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    _ = max(deadline - loop.time(), 0.0)
    lock = self._lock
    await lock.acquire()
    call = asyncio.ensure_future(asyncio.to_thread(func, *a, **k))
    try:
        result = await call
    except BaseException:
        lock.release()
        raise
    lock.release()
    return result


async def naive_waitfor(self, func, *a, **k):
    timeout = config.settings.ezdxf_call_timeout
    async with self._lock:
        return await asyncio.wait_for(asyncio.to_thread(func, *a, **k), timeout)


VARIANTS = [
    ("A shipped timeout path (2x wait_for + ensure_future)", SHIPPED, 120.0),
    ("B shipped no-timeout path (lock + to_thread)        ", SHIPPED, 0.0),
    ("C timeout path with both wait_for removed           ", task_no_waitfor, 120.0),
    ("D naive single wait_for on the coroutine            ", naive_waitfor, 120.0),
]


async def run(impl, timeout, backend):
    EzdxfBackend._async = impl
    config.settings.ezdxf_call_timeout = timeout
    try:
        started = time.perf_counter()
        for _ in range(CALLS):
            await backend._async(noop)
        return (time.perf_counter() - started) * 1e6 / CALLS  # us per call
    finally:
        EzdxfBackend._async = SHIPPED


async def main():
    backend = EzdxfBackend()
    await backend.connect()
    await run(SHIPPED, 120.0, backend)  # warm-up
    samples = {label: [] for label, _, _ in VARIANTS}
    for _ in range(REPS):
        for label, impl, timeout in VARIANTS:
            samples[label].append(await run(impl, timeout, backend))
    med = {}
    for label, _, _ in VARIANTS:
        med[label] = statistics.median(samples[label])
        print(f"{label} {med[label]:7.1f} us/call  runs "
              + " ".join(f"{v:.0f}" for v in samples[label]))
    a, b, c, d = (med[label] for label, _, _ in VARIANTS)
    print()
    print(f"timeout path total overhead  A-B : {a - b:6.1f} us/call")
    print(f"overhead with wait_for gone  C-B : {c - b:6.1f} us/call  "
          f"({(c - b) / (a - b) * 100:.0f}% of it)")
    print(f"cost of the wait_for wrappers A-C: {a - c:6.1f} us/call  "
          f"({(a - c) / (a - b) * 100:.0f}% of it)")
    print(f"naive one-wait_for overhead  D-B : {d - b:6.1f} us/call")


asyncio.run(main())

"""CPU-time attribution for C131 (wall clock on this box is too noisy).

process_time() counts CPU across every thread of the process, so scheduler
jitter and the thread-pool handoff latency drop out and what is left is the
actual work each _async shape performs.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time

os.environ.setdefault("AUTOCAD_MCP_BACKEND", "ezdxf")

import config  # noqa: E402
from backends.ezdxf_backend import EzdxfBackend  # noqa: E402

CALLS = 4000
REPS = 9
SHIPPED = EzdxfBackend._async


def noop():
    return None


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
    ("C timeout path, both wait_for removed               ", task_no_waitfor, 120.0),
    ("D naive single wait_for on the coroutine            ", naive_waitfor, 120.0),
]


async def run(impl, timeout, backend):
    EzdxfBackend._async = impl
    config.settings.ezdxf_call_timeout = timeout
    try:
        started = time.process_time()
        for _ in range(CALLS):
            await backend._async(noop)
        return (time.process_time() - started) * 1e6 / CALLS
    finally:
        EzdxfBackend._async = SHIPPED


async def main():
    backend = EzdxfBackend()
    await backend.connect()
    await run(SHIPPED, 120.0, backend)
    samples = {label: [] for label, _, _ in VARIANTS}
    for _ in range(REPS):
        for label, impl, timeout in VARIANTS:
            samples[label].append(await run(impl, timeout, backend))
    med = {}
    for label, _, _ in VARIANTS:
        vals = sorted(samples[label])
        med[label] = statistics.median(vals)
        print(f"{label} {med[label]:7.1f} us cpu/call  min {vals[0]:.0f} max {vals[-1]:.0f}")
    a, b, c, d = (med[label] for label, _, _ in VARIANTS)
    print()
    print(f"timeout path overhead        A-B : {a - b:6.1f} us cpu/call")
    print(f"survives removing wait_for   C-B : {c - b:6.1f} us cpu/call "
          f"({(c - b) / (a - b) * 100:.0f}% of A-B)")
    print(f"attributable to wait_for     A-C : {a - c:6.1f} us cpu/call "
          f"({(a - c) / (a - b) * 100:.0f}% of A-B)")
    print(f"naive single wait_for        D-B : {d - b:6.1f} us cpu/call")


asyncio.run(main())

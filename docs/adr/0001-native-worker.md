# ADR 0001: A native .NET worker inside AutoCAD

- **Status:** Accepted. The worker is **deferred to 1.9.0** and is built there only if
  the decision rule below says so.
- **Date:** 2026-11-03
- **Plan item:** 1.8-24 (`plan.md` §3 W3; brainstorm idea live-9). Design spec:
  `docs/superpowers/specs/2026-11-03-v1.8.0-design.md`, section "1.8-24".
- **Depends on:** 1.8-23 (COM speed: the bulk job this rule measures against).
- **Measurements in this record:** none. The rule was written before any measurement,
  and the optional spike was not run (see "The spike").

## Context

The live engine (`backends/com_backend.py`) drives AutoCAD through out-of-process
ActiveX: every call is a cross-process COM round trip, and every job runs on one
single-threaded executor to satisfy AutoCAD's STA requirement. The rival's published
AutoCAD 2027 run (plan §4.6) measured the live path about 2× slower than theirs. The
code has mechanical causes for that: a `_regen()` after each created entity, several
RPCs per entity, an O(N) walk of the owner block per corner operation, and "batch"
tools that are N separate STA jobs. Item 1.8-23 removes those causes inside COM and
adds a single-job `entity_batch_create` / `entity_delete_many` (the "COM bulk job").

The second weakness is rollback. On COM the `transactions` capability has mode
`undo_mark`, and `cad_batch` reports its rollback guarantee as `best_effort_undo`:
`transaction_begin` types `_.UNDO _M` and opens a `StartUndoMark` group,
`transaction_rollback` ends the group and types `_.UNDO _B`, both through
`_safe_send_command`. AutoCAD runs command-line input asynchronously and never confirms
it landed, so the before/after fingerprint is the only evidence. On AutoCAD 2026 the
Mark + group + `_UNDO _B` route was measured to remove the entities (47 ms, CMDACTIVE 0),
but nested `StartUndoMark` behaviour and the undo steps a generator produces (spike S4,
`docs/analysis/spikes-1.8.0.json`) are still unmeasured.

A native worker is a .NET plug-in loaded inside `acad.exe`. It would write entities in
one database `Transaction` (in-process, no per-entity RPC) and abort that transaction
for a real atomic rollback. 1.8.0 named it as a promise. Shipping nothing would quietly
break that promise; building it would consume the release.

## Decision

The decision rule is fixed here, **before** any measurement.

**The worker is built in 1.9.0 if, and only if, at least one of these holds:**

1. **Speed.** On the same seat and the same 1,000-line workload, the worker is at
   least **3× faster** than the COM bulk job of 1.8-23: the median wall time of the
   worker's `add_lines(1000)`, measured from the server process with the pipe round
   trip included, multiplied by 3, is at most the median wall time of the COM bulk job
   writing the same 1,000 lines. Both sides run in the same AutoCAD session on a fresh
   document, with the same number of timed repetitions after one untimed warm-up.
2. **Rollback.** Atomic rollback **cannot be made reliable with UNDO marks**: after the
   1.8.0 fixes, a live run on the supported AutoCAD release shows a rolled-back
   `cad_batch` or `transaction_rollback` whose after-fingerprint differs from its
   before-fingerprint, or a rollback that stops at a prompt instead of landing, and the
   cause cannot be closed inside the UNDO-mark route.

**Otherwise the worker stays deferred**, for these reasons:

- **A build and a signature per AutoCAD release.** A managed plug-in compiles against
  one release family's .NET API (AutoCAD 2025 and later on .NET 8, earlier releases on
  .NET Framework); every supported release is another build, another signed binary and
  another load test.
- **The SECURELOAD route.** AutoCAD loads a plug-in without a prompt only from a
  trusted location or as a signed assembly, depending on SECURELOAD. The server cannot
  take that route itself, by its own rules: `NETLOAD` is in the command denylist
  (`security.py`, `_DANGEROUS_COMMANDS`), and `.dll` is in `WRITE_REFUSED_SUFFIXES`, so
  no tool may write the binary either. Installing the worker would be an out-of-band
  operator step with its own security review.
- **It cannot be tested in CI.** Its code runs only inside `acad.exe`; CI has no
  AutoCAD, so the most a CI test covers is the pipe protocol against a fake.
- **A second language for a single maintainer.** A C# project, its toolchain and the
  AutoCAD .NET reference assemblies, next to a Python code base maintained by one person.

**When the rule is evaluated.** At 1.9.0 planning, from two inputs: the 1.8-23 live
result (`benchmarks/results/live/perf_com.json`) with the spike's
`benchmarks/results/live/worker_spike.json` for clause 1, and the live rollback
smokes for clause 2. If neither input exists then, the rule cannot fire and the worker
stays deferred. The result is appended to this record under a dated "Outcome" heading;
this section is not edited.

## The spike (optional, one day)

What the spike would be, exactly as planned:

- a minimal C# `net8.0-windows` plug-in for AutoCAD 2025 and later;
- installed through a trusted autoloader bundle;
- a named pipe that answers `ping` and `add_lines(n)`, the latter writing all `n`
  lines in one database `Transaction`;
- the same 1,000 lines written through the COM bulk job of 1.8-23, for comparison;
- results written to `benchmarks/results/live/worker_spike.json`: both timings (the
  worker and the COM bulk job, with the repetition count and the median) and a run
  manifest (server SHA, dirty flag, AutoCAD release, the plug-in build, date).

It is time-boxed to one day. If the SDK or trusted-path setup takes longer than that,
the spike is the first thing cut, and clause 1 is not evaluated.

**It was not run in this pass.** The repository holds no `worker_spike.json` and no
timing of any kind; this record invents none. The reasons: 1.8-24 is a documentation
item, written before 1.8-23 landed, so the COM bulk job the rule compares against does
not exist yet on the branch; there is no .NET SDK on the build machine (runtimes only)
and no AutoCAD .NET reference assemblies in the repository; and no AutoCAD seat was in
use for this item.

**What would trigger running it:** 1.8-23 merged and its live `perf_com.json` recorded;
an AutoCAD 2025-or-later seat; a .NET 8 SDK and the AutoCAD .NET reference assemblies
installed; and the maintainer choosing to spend the day before 1.9.0 planning. A
1.8-23 result that misses its own targets (bulk at least 5× faster than v1.7.0's
singles) makes the spike the obvious next measurement.

## Consequences

- 1.8.0 ships no worker. CHANGELOG's "Not in 1.8.0" names this record.
- The COM rollback guarantee stays `best_effort_undo`, reported as such in the
  `atomicity` block; nothing claims more.
- If clause 1 or clause 2 fires, 1.9.0 inherits an XL item (plugin, pipe protocol,
  per-release builds, an install route and its tests), and that item gets its own
  design spec.
- If neither fires, the deferral stands with the reasons above and the live engine
  stays pure COM.

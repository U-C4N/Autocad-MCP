"""The test-only marker that arms ``backends/fault_barrier.py`` (1.8-25).

A barrier parks a server only when ``ACADMCP_TEST_BARRIER`` names it AND this
module is importable with the value below. It is never shipped: ``tests/`` is
outside the wheel's ``only-include`` and ``scripts/check_dist_contents.py``
refuses a wheel with any ``tests/`` member; this folder is not a package and
pytest never puts it on ``sys.path``. ``benchmarks/fault_inject.py`` adds it to
the ``PYTHONPATH`` of a child server it starts with a barrier armed - nothing
else does.
"""

MARKER = "acadmcp-fault-injection-test-only"

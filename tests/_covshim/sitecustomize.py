"""Coverage for subprocesses under test (the hub, CLI invocations, hooks).

Harmless when COVERAGE_PROCESS_START is unset: process_startup() is then a no-op.
"""
try:
    import coverage
    coverage.process_startup()
except Exception:  # noqa: BLE001  pragma: no cover - never break the program under test
    pass

#!/usr/bin/env python3
"""Minimal test runner: collects ``test_*`` callables and reports failures.

Why this exists alongside pytest: the production worker image is slim and does
not carry a test framework, but the pre-flight health check runs the safety
tests (gates, sizing, exits) before the executor is allowed to load wallet keys.
A container that cannot prove its own risk logic must not trade.

Usage:  python scripts/run_tests.py [module ...]
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pathlib
import sys
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def collect(module_names: list[str]) -> list[tuple[str, object]]:
    tests = []
    for name in module_names:
        mod = importlib.import_module(name)
        for attr in sorted(dir(mod)):
            if attr.startswith("test_") and callable(getattr(mod, attr)):
                tests.append((f"{name}::{attr}", getattr(mod, attr)))
    return tests


def main(argv: list[str]) -> int:
    modules = argv[1:] or ["tests.unit.test_core", "tests.unit.test_execution",
                           "tests.unit.test_orchestration", "tests.unit.test_enrich_scoring"]
    tests = collect(modules)
    failed: list[tuple[str, str]] = []

    for name, fn in tests:
        try:
            # Calling an `async def test_x()` without awaiting it returns a
            # coroutine, prints no error, and never runs the body — a silent
            # false-pass that would hide exactly the kind of bug these tests
            # exist to catch. Every test is written as a plain sync function
            # (using asyncio.run internally where it needs the event loop), but
            # this guard makes that a project convention enforced by the
            # runner, not just a habit someone has to remember.
            if inspect.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"  PASS  {name}")
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"  FAIL  {name}")

    print("-" * 72)
    print(f"{len(tests) - len(failed)}/{len(tests)} passed")
    for name, tb in failed:
        print(f"\n=== {name} ===\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Trusted test entry point, launched with Python -I outside the generated project.

This checks unittest's result object, not text printed by generated tests. It is
not a security sandbox: generated programs still execute with the user's rights.
"""
import json
from pathlib import Path
import sys
import unittest


def main():
    workspace, evidence = sys.argv[1:]
    # -I intentionally ignores PYTHON* environment variables. Set these here
    # before importing project tests so same-tick edits never load stale .pyc.
    sys.pycache_prefix = str(Path(evidence).parent / "bytecode")
    sys.dont_write_bytecode = True
    loader = unittest.TestLoader()
    runner = unittest.TextTestRunner(verbosity=2)
    sys.path.insert(0, workspace)
    suite = loader.discover(workspace, pattern="test*.py")
    result = runner.run(suite)
    data = {"tests_run": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "successful": result.wasSuccessful()}
    Path(evidence).write_text(json.dumps(data), encoding="utf-8")
    return 0 if data["successful"] and data["tests_run"] > 0 and not data["skipped"] else 1


if __name__ == "__main__":
    sys.exit(main())

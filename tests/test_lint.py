# tests/test_lint.py
"""
Static guard against undefined names in the service code.

Why this test exists
--------------------
Several production bugs in this service were *silent* NameErrors — a
``select`` or ``json`` that was used but never imported. They only fired
inside a ``try/except Exception`` block, so the service kept running while
the MajiScope result delivery quietly failed on every single run.

They are also invisible to the local test suite when the module still
imports, and they only crash on Python 3.12 (the container) when they sit
in an annotation.

``pyflakes`` reports every undefined name statically, so this test turns a
class of silent runtime failures into a hard build failure.
"""

import pathlib
import subprocess
import sys

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"


def _pyflakes_available() -> bool:
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _pyflakes_available(), reason="pyflakes is not installed")
def test_no_undefined_names_in_app():
    """Every name used in app/ must actually be defined or imported."""
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(APP_DIR)],
        capture_output=True,
        text=True,
    )
    offenders = [
        line.strip()
        for line in proc.stdout.splitlines()
        if "undefined name" in line
    ]
    assert not offenders, (
        "Undefined names in app/ — these raise NameError at runtime (or on "
        "boot under Python 3.12):\n" + "\n".join(offenders)
    )

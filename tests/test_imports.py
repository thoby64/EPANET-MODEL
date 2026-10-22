# tests/test_imports.py
"""
Every module in ``app/`` must import cleanly and every annotation must resolve.

Why this test exists
--------------------
The deployed container runs **Python 3.12**, where function annotations are
evaluated *eagerly* at definition time. The local development environment
runs **Python 3.14**, where PEP 649 makes annotations *lazy* — the names are
only looked up if somebody inspects them.

The result is a class of bug that is invisible locally and fatal in
production::

    def _build_snapshot_heat_sources(..., leakage_json: Optional[dict]):
        ^^^^^^^ NameError raised only on Python <= 3.13

``import app.main`` succeeds on 3.14 and crashes the service on boot on 3.12.

``typing.get_type_hints()`` forces the annotations to be evaluated exactly the
way Python 3.12 does, so the difference can no longer hide.
"""

import importlib
import inspect
import pkgutil
import typing

import app


def _module_names() -> list[str]:
    """Every importable module inside the ``app`` package."""
    return [info.name for info in pkgutil.walk_packages(app.__path__, prefix="app.")]


def test_every_app_module_imports():
    """A module that cannot be imported kills the whole service on boot."""
    failed = []
    for name in _module_names():
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - the assertion reports it
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failed, "Modules that failed to import:\n" + "\n".join(failed)


def test_all_annotations_resolve():
    """Annotations must not reference names that were never imported.

    This is the check that catches a missing ``from typing import Optional``:
    on Python 3.12 the container would raise ``NameError`` while importing.
    """
    problems: list[str] = []
    for name in _module_names():
        module = importlib.import_module(name)
        for attr_name, obj in vars(module).items():
            functions = []
            if inspect.isfunction(obj):
                functions.append((attr_name, obj))
            elif inspect.isclass(obj):
                for member_name, member in vars(obj).items():
                    if inspect.isfunction(member):
                        functions.append((f"{attr_name}.{member_name}", member))
            for label, func in functions:
                if getattr(func, "__module__", None) != module.__name__:
                    continue  # imported from elsewhere - not this module's problem
                try:
                    typing.get_type_hints(func)
                except NameError as exc:
                    problems.append(f"{name}.{label}: {exc}")
                except TypeError:
                    # Unresolvable forward references are not what we test here.
                    pass
    assert not problems, (
        "Annotations reference names that are not imported — this raises "
        "NameError on Python 3.12 (the container) even though it is fine on "
        "Python 3.14:\n" + "\n".join(problems)
    )

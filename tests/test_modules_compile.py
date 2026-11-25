"""Every module compiles and the app entry point imports: a syntax error can't slip past the suite."""

import importlib
import py_compile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mctracker"


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_module_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / "out.pyc"), doraise=True)


def test_app_imports(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    assert importlib.import_module("mctracker.app").main

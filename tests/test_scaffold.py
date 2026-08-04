"""Placeholder test proving the toolchain is wired up. Real tests arrive in PR2+."""

import importlib


def test_packages_importable() -> None:
    assert importlib.import_module("engine") is not None
    assert importlib.import_module("agents") is not None

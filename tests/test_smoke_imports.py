"""Every module in the package imports cleanly, with no credentials set.

This is the cheapest possible guard against a merge that would crash the bot
on start-up (a syntax error, a missing dependency, a bad import).
"""

import importlib
import pkgutil

import pytest

import stock_agent

MODULES = sorted(
    m.name for m in pkgutil.iter_modules(stock_agent.__path__) if m.name != "__main__"
)


def test_package_has_modules():
    assert "scheduler" in MODULES and "risk_manager" in MODULES


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(f"stock_agent.{name}")


def test_entry_point_imports_without_starting_the_agent():
    module = importlib.import_module("stock_agent.__main__")
    assert callable(module.main)

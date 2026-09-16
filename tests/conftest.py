"""Shared fixtures for the dashboard builder tests.

The builders are scripts, not a package: they import `hopsworks` at module level and are meant to
be run inside a Hopsworks job. These tests load them by path with the SDK stubbed out, so they
exercise the pure parts — SQL construction, metric resolution, Superset object reconciliation —
without a cluster, a network, or an SDK install.

Everything here is offline on purpose. The defects these cover were all found by reading, and all
of them would have been caught by a fake API; none of them need a live Superset to demonstrate.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _stub(name: str) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: types.SimpleNamespace()  # type: ignore[attr-defined]
    sys.modules[name] = mod


def load(script: str, stub: tuple[str, ...] = ("hopsworks",)):
    """Import a builder script by filename, under its own module name."""
    for name in stub:
        _stub(name)
    mod_name = script.replace(".py", "").replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, REPO / script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolve their module from sys.modules.
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def superset():
    return load("superset.py")


@pytest.fixture(scope="session")
def executive():
    return load("create_executive_dashboard.py")


@pytest.fixture(scope="session")
def promotion():
    return load("create_promotion_dashboard.py")


class FakeApi:
    """The slice of the Superset API the builders use, recording what it was asked to do.

    `databases` and `datasets`/`charts` are plain dicts shaped like the real list responses,
    including the nested `database.id` the dataset list returns.
    """

    def __init__(self, databases=None, datasets=None, charts=None, fail_create=False):
        self.databases = databases if databases is not None else [
            {"id": 1, "database_name": "hopsworks_analytics__meb10000_superset", "backend": "mysql"},
            {"id": 2, "database_name": "hopsworks_analytics", "backend": "mysql"},
            {"id": 3, "database_name": "someproject_onlinefs", "backend": "mysql"},
        ]
        self.datasets = datasets or []
        self.charts = charts or []
        self.fail_create = fail_create
        self.updated_datasets: list[tuple[int, dict]] = []
        self.updated_charts: list[tuple[int, dict]] = []
        self.created: list[dict] = []
        self.deleted_charts: list[int] = []

    # -- listing -----------------------------------------------------------
    def list_databases(self):
        return {"result": self.databases}

    def _request(self, method, path, **kwargs):
        if method == "GET" and path.startswith("/api/v1/dataset/?q="):
            return {"result": self.datasets}
        if method == "GET" and path.startswith("/api/v1/chart/?q="):
            return {"result": self.charts}
        return {}

    # -- datasets ----------------------------------------------------------
    def update_dataset(self, dataset_id, **kwargs):
        self.updated_datasets.append((dataset_id, kwargs))
        return {}

    def get_dataset(self, dataset_id):
        return {"result": {"columns": []}}

    def create_dataset(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("Superset rejected the dataset")
        self.created.append(kwargs)
        return {"id": 900}

    # -- charts ------------------------------------------------------------
    def update_chart(self, chart_id, **kwargs):
        self.updated_charts.append((chart_id, kwargs))
        return {}

    def create_chart(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("Superset rejected the chart")
        self.created.append(kwargs)
        return {"id": 901}

    def delete_chart(self, chart_id):
        self.deleted_charts.append(chart_id)
        return {}


@pytest.fixture
def fake_api():
    return FakeApi

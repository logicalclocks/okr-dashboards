"""Publishing a duplicate-features scan.

Every scan is a complete statement, including one that finds nothing: yesterday's duplicates were
resolved, so yesterday's rows must go. Two earlier forms got this wrong in opposite directions:
one skipped the write on an empty result and kept reporting findings that no longer existed; the
one before it deleted the previous version first, so a failed write lost the last good result.
The rule now is publish first, retire after.
"""

import pytest
from conftest import load


@pytest.fixture(scope="module")
def duplicates():
    return load("detect_duplicate_features.py", stub=("hopsworks", "hsfs", "hsfs.feature"))


class FakeFG:
    def __init__(self, store, version, description=""):
        self.store, self.version, self.description = store, version, description

    def save(self, df):
        self.store.log.append(f"save v{self.version} rows={len(df)}")

    def insert(self, df, wait=False):
        self.store.log.append(f"insert v{self.version} rows={len(df)}")

    def delete(self):
        self.store.log.append(f"delete v{self.version}")
        self.store.versions.remove(self.version)


class FakeStore:
    def __init__(self, versions=(), fail_create=False):
        self.versions = list(versions)
        self.fail_create = fail_create
        self.log: list[str] = []
        self.descriptions: dict[int, str] = {}

    def get_feature_groups(self, name):
        return [FakeFG(self, v) for v in self.versions]

    def get_feature_group(self, name, version):
        return FakeFG(self, version)

    def create_feature_group(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("create refused")
        v = kwargs["version"]
        self.versions.append(v)
        self.descriptions[v] = kwargs["description"]
        self.log.append(f"create v{v}")
        return FakeFG(self, v, kwargs["description"])


class FakeProject:
    def __init__(self, store):
        self.store = store

    def get_feature_store(self):
        return self.store


ROW = {"feature_group": "fg", "feature_name": "f", "reason": "r"}


def test_first_scan_is_version_one(duplicates):
    store = FakeStore()
    duplicates.write_feature_group(FakeProject(store), [ROW])
    assert store.log == ["create v1", "insert v1 rows=1"]


def test_a_later_scan_publishes_a_new_version_then_retires_the_old(duplicates):
    store = FakeStore(versions=[1])
    duplicates.write_feature_group(FakeProject(store), [ROW])
    assert store.log == ["create v2", "insert v2 rows=1", "delete v1"]
    assert store.versions == [2]


def test_an_empty_scan_publishes_an_empty_result_and_retires_old_findings(duplicates):
    """Resolved duplicates must stop being reported. The empty version carries the schema."""
    store = FakeStore(versions=[3])
    duplicates.write_feature_group(FakeProject(store), [])
    assert store.log == ["create v4", "save v4 rows=0", "delete v3"]
    assert store.versions == [4]


def test_a_failed_publish_keeps_the_last_good_result(duplicates):
    store = FakeStore(versions=[2], fail_create=True)
    with pytest.raises(RuntimeError, match="create refused"):
        duplicates.write_feature_group(FakeProject(store), [ROW])
    assert store.versions == [2], "nothing may be retired before the replacement exists"


def test_the_scan_time_and_finding_count_are_recorded_on_the_version(duplicates):
    """The count is what lets a reader recognise a clean scan: an empty DELTA feature group has
    no data files and cannot be read, so the rows alone cannot say "zero"."""
    store = FakeStore()
    duplicates.write_feature_group(FakeProject(store), [])
    assert duplicates.SCANNED_AT_PREFIX in store.descriptions[1]
    assert f"{duplicates.FINDINGS_PREFIX}0" in store.descriptions[1]

    duplicates.write_feature_group(FakeProject(store), [ROW, ROW])
    assert f"{duplicates.FINDINGS_PREFIX}2" in store.descriptions[2]

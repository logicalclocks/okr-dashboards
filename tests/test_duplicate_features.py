"""Replacing the duplicate-features result must not destroy it on the way.

`insert(overwrite=True)` is unusable for DELTA feature groups here, so the replace is a delete
followed by a create. That is survivable when there is something to write. It was not survivable
when there was not: a run that found no duplicates dropped the feature group and reported it as
"left empty", leaving the executive dashboard's count reading against nothing.
"""

import pytest
from conftest import load


@pytest.fixture(scope="module")
def duplicates():
    return load("detect_duplicate_features.py")


class FakeFeatureGroup:
    def __init__(self, log):
        self.log = log

    def delete(self):
        self.log.append("deleted")

    def insert(self, df, wait=False):
        self.log.append(f"inserted {len(df)}")


class FakeFeatureStore:
    def __init__(self, log, exists=True, fail_create=False):
        self.log, self.exists, self.fail_create = log, exists, fail_create

    def get_feature_group(self, *args, **kwargs):
        if not self.exists:
            raise RuntimeError("no such feature group")
        return FakeFeatureGroup(self.log)

    def create_feature_group(self, **kwargs):
        self.log.append("created")
        if self.fail_create:
            raise RuntimeError("create refused")
        return FakeFeatureGroup(self.log)


class FakeProject:
    def __init__(self, store):
        self.store = store

    def get_feature_store(self):
        return self.store


def test_no_duplicates_leaves_the_previous_result_alone(duplicates):
    """An empty result is not evidence that the last one was wrong."""
    log: list[str] = []
    duplicates.write_feature_group(FakeProject(FakeFeatureStore(log)), [])
    assert log == []


def test_duplicates_are_written(duplicates):
    log: list[str] = []
    duplicates.write_feature_group(
        FakeProject(FakeFeatureStore(log)),
        [{"feature_group": "fg", "feature_name": "f", "reason": "r"}],
    )
    assert log == ["deleted", "created", "inserted 1"]


def test_a_failed_replacement_still_raises(duplicates):
    """It reports the loss too, but the caller must not read it as success."""
    log: list[str] = []
    store = FakeFeatureStore(log, fail_create=True)
    with pytest.raises(RuntimeError, match="create refused"):
        duplicates.write_feature_group(
            FakeProject(store),
            [{"feature_group": "fg", "feature_name": "f", "reason": "r"}],
        )

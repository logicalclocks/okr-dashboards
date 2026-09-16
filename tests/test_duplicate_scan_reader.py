"""Reading the latest duplicate-features scan into the executive dashboard.

Three states have to stay apart, because the dashboard used to render all of them as an empty
chart: a scan that found nothing, no scan ever published, and a scan that exists but could not be
read. Proven on a live cluster: an empty DELTA feature group has a schema and no data files, and
reading it fails with "no active delta files", so a clean scan read as broken until the detector
started recording its finding count on the version.
"""

import pandas as pd


class FakeFG:
    def __init__(self, version, description, rows=None, fail_read=False):
        self.version, self.description, self.rows, self.fail_read = version, description, rows, fail_read

    def select(self, cols):
        return self

    def read(self):
        if self.fail_read:
            raise RuntimeError("No active delta files found for featuregroup")
        return pd.DataFrame({"feature_name": self.rows or []})


class FakeProject:
    def __init__(self, fgs):
        self.fgs = fgs

    def get_feature_store(self):
        proj = self

        class FS:
            def get_feature_groups(self, name):
                return proj.fgs
        return FS()


def test_a_clean_scan_is_zero_findings_not_unreadable(executive):
    fg = FakeFG(3, "... scanned_at=2026-09-16T21:05:39Z findings=0", fail_read=True)
    assert executive.read_duplicate_scan(FakeProject([fg])) == ({}, "2026-09-16T21:05:39Z", "ok")


def test_findings_are_read_from_the_newest_version(executive):
    old = FakeFG(1, "findings=9", rows=["x"] * 9)
    new = FakeFG(2, "scanned_at=T findings=2", rows=["a", "a"])
    counts, scanned_at, state = executive.read_duplicate_scan(FakeProject([old, new]))
    assert (counts, scanned_at, state) == ({"a": 2}, "T", "ok")


def test_nothing_published_is_absent(executive):
    assert executive.read_duplicate_scan(FakeProject([])) == ({}, None, "absent")


def test_a_version_that_cannot_be_read_is_unreadable_not_clean(executive):
    """A read failure on a version that claims findings is a failure, and must say so."""
    fg = FakeFG(5, "scanned_at=T findings=4", fail_read=True)
    assert executive.read_duplicate_scan(FakeProject([fg])) == ({}, "T", "unreadable")


def test_a_version_without_a_count_falls_back_to_reading(executive):
    """Versions written before the count existed carry rows and no token; read them."""
    fg = FakeFG(1, "no tokens here", rows=["f"])
    assert executive.read_duplicate_scan(FakeProject([fg])) == ({"f": 1}, None, "ok")

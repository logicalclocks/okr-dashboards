"""Rebuilding a chart must not destroy the published one first.

The builders used to delete every chart with a matching title and then create a replacement. A
failure in between left the dashboard missing charts, and the scheduled tag refresh reaches that
path unattended. A success changed the chart id, dropping the chart out of any other dashboard
that reused it. And matching on title alone destroyed a chart a user happened to name the same.
"""

import pytest


def _spec(superset, name="OKR · Active Feature Count"):
    return superset.ChartSpec(name=name, viz_type="table", width=6, params={})


def _chart(name="OKR · Active Feature Count", cid=7, ds=10):
    return {"id": cid, "slice_name": name, "datasource_id": ds}


def test_an_existing_chart_is_updated_in_place(superset, fake_api):
    api = fake_api(charts=[_chart()])
    s = superset.Superset.connect(api)

    chart = s.replace_chart(_spec(superset), 10)

    assert chart.id == 7, "the id must survive so other dashboards keep the chart"
    assert api.deleted_charts == []
    assert api.updated_charts[0][0] == 7


def test_nothing_is_deleted_before_a_successful_write(superset, fake_api):
    """The regression: a create that fails after the delete strips the dashboard."""
    api = fake_api(charts=[_chart()], fail_create=True)
    s = superset.Superset.connect(api)

    s.replace_chart(_spec(superset), 10)

    assert api.deleted_charts == []


def test_a_chart_with_our_title_on_someone_elses_dataset_is_refused(superset, fake_api):
    api = fake_api(charts=[_chart(ds=99)])
    s = superset.Superset.connect(api)

    with pytest.raises(RuntimeError, match="may not be ours"):
        s.replace_chart(_spec(superset), 10)
    assert api.deleted_charts == []


def test_a_missing_chart_is_created(superset, fake_api):
    api = fake_api(charts=[])
    s = superset.Superset.connect(api)

    assert s.replace_chart(_spec(superset), 10).id == 901
    assert api.deleted_charts == []


def test_our_own_duplicates_are_removed_only_after_the_survivor_is_written(superset, fake_api):
    """Debris from the delete-and-create era, cleaned up once there is a survivor."""
    api = fake_api(charts=[_chart(cid=7), _chart(cid=8)])
    s = superset.Superset.connect(api)

    chart = s.replace_chart(_spec(superset), 10)

    assert chart.id == 7
    assert api.deleted_charts == [8]
    assert api.updated_charts[0][0] == 7

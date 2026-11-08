"""Attaching charts to a dashboard must not detach them from any other.

Superset's chart PUT takes the complete membership list, so `dashboards=[this_one]` is a
replacement. Rebuilding one dashboard removed every chart it touched from any other dashboard
that reused it, even though the chart id itself survived the in-place update.
"""


def _chart(cid, dashboards):
    return {"id": cid, "slice_name": f"chart {cid}",
            "dashboards": [{"id": d, "dashboard_title": f"d{d}"} for d in dashboards]}


def test_existing_memberships_are_preserved(superset, fake_api):
    api = fake_api(charts=[_chart(7, [4, 5])])
    superset.attach_charts(api, 4, [7])
    assert api.updated_charts == [(7, {"dashboards": [4, 5]})]


def test_a_new_membership_is_added_to_the_existing_ones(superset, fake_api):
    api = fake_api(charts=[_chart(7, [5])])
    superset.attach_charts(api, 4, [7])
    assert api.updated_charts == [(7, {"dashboards": [4, 5]})]


def test_a_chart_on_no_dashboard_gets_just_this_one(superset, fake_api):
    api = fake_api(charts=[_chart(7, [])])
    superset.attach_charts(api, 4, [7])
    assert api.updated_charts == [(7, {"dashboards": [4]})]


def test_ensure_dashboard_goes_through_the_union(superset, fake_api):
    """The end-to-end path the builders use, with a chart another dashboard also holds."""
    api = fake_api(charts=[_chart(7, [5])])
    api.create_dashboard = lambda **kw: {"id": 4}
    api.update_dashboard = lambda *a, **kw: {}
    s = superset.Superset.connect(api)
    chart = superset.Chart(id=7, spec=superset.ChartSpec(name="chart 7", viz_type="table", width=6, params={}))

    s.ensure_dashboard("Executive", [chart])

    assert api.updated_charts == [(7, {"dashboards": [4, 5]})]


def test_every_builder_uses_the_shared_attach(executive, superset):
    """The executive and jobs builders had private copies of the replacing form."""
    import inspect
    assert executive.attach_charts is superset.attach_charts
    assert "dashboards=[dash_id]" not in inspect.getsource(executive)

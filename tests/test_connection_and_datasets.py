"""The shared analytics connection, and the datasets bound to it.

A cluster upgrading from the per-user connection model carries both shapes at once:
`hopsworks_analytics` (shared, granted through a role) and
`hopsworks_analytics__<superset user>` (one per admin). Choosing between them by prefix picks
whichever the API returns first, which during an upgrade is routinely somebody's personal
connection. A dashboard built there is unqueryable by every other admin and breaks outright when
that admin's connection is retired.
"""

import pytest


def test_shared_connection_wins_over_a_personal_one(superset, fake_api):
    db_id, name = superset.resolve_analytics_database(fake_api())
    assert (db_id, name) == (2, "hopsworks_analytics")


def test_prefix_match_is_the_fallback_not_the_rule(superset, fake_api):
    """With no exact match the per-user connection is still better than failing."""
    api = fake_api(databases=[
        {"id": 1, "database_name": "hopsworks_analytics__meb10000_superset", "backend": "mysql"},
        {"id": 3, "database_name": "someproject_onlinefs", "backend": "mysql"},
    ])
    assert superset.resolve_analytics_database(api)[0] == 1


def test_a_mysql_connection_that_is_not_ours_is_never_chosen(superset, fake_api):
    """An online feature store is also a MySQL connection; matching the backend is not enough."""
    api = fake_api(databases=[{"id": 3, "database_name": "someproject_onlinefs", "backend": "mysql"}])
    with pytest.raises(RuntimeError, match="No Superset connection named"):
        superset.resolve_analytics_database(api)


def _legacy_dataset(name="okr_progress", ds_id=55, db_id=1):
    return {"id": ds_id, "table_name": name, "schema": "hopsworks",
            "database": {"id": db_id, "database_name": "hopsworks_analytics__meb10000_superset"}}


def test_a_dataset_on_a_personal_connection_is_migrated_keeping_its_id(superset, fake_api):
    """The id has to survive: every chart is bound to it."""
    api = fake_api(datasets=[_legacy_dataset()])
    s = superset.Superset.connect(api)

    assert s.ensure_dataset("okr_progress", "SELECT 1") == 55
    assert api.created == [], "an existing dataset must be moved, not duplicated"
    assert api.updated_datasets[0][1]["database_id"] == 2


def test_a_dataset_already_on_the_shared_connection_is_only_updated(superset, fake_api):
    api = fake_api(datasets=[_legacy_dataset(db_id=2)])
    s = superset.Superset.connect(api)

    s.ensure_dataset("okr_progress", "SELECT 2")
    assert all("database_id" not in kw for _, kw in api.updated_datasets)


def test_one_name_on_two_connections_is_refused_rather_than_guessed(superset, fake_api):
    """Picking one would silently orphan the charts on the other."""
    api = fake_api(datasets=[_legacy_dataset(db_id=1), _legacy_dataset(ds_id=56, db_id=4)])
    s = superset.Superset.connect(api)

    with pytest.raises(RuntimeError, match="refusing to guess"):
        s.ensure_dataset("okr_progress", "SELECT 1")


def test_caching_is_disabled_with_the_value_superset_means_by_it(superset, fake_api):
    """-1 is CACHE_DISABLED_TIMEOUT. 0 is 'never expire' in Flask-Caching, which is the opposite."""
    api = fake_api(datasets=[_legacy_dataset(db_id=2)])
    s = superset.Superset.connect(api)

    s.ensure_dataset("okr_progress", "SELECT 1")
    timeouts = [kw["cache_timeout"] for _, kw in api.updated_datasets if "cache_timeout" in kw]
    assert timeouts == [-1]

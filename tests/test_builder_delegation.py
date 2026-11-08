"""The executive and jobs builders resolve their own connection and used to carry private copies
of the dataset and chart helpers. Those copies matched datasets by name alone and never moved one
off the connection it was first built on, so the shared-connection migration happened for every
dashboard except the two that mattered most. They delegate now, and this holds them to it.
"""

from conftest import load


def test_builders_share_the_migrating_ensure_dataset(executive, superset):
    jobs = load("create_jobs_dashboard.py")
    assert executive.ensure_dataset is superset.ensure_dataset
    assert jobs.ensure_dataset is superset.ensure_dataset


def test_executive_ensure_dataset_migrates_a_personal_dataset(executive, fake_api):
    """Through the builder's own call shape, `ensure_dataset(api, db_id, name, sql)`."""
    api = fake_api(datasets=[{"id": 55, "table_name": "okr_progress", "schema": "hopsworks",
                              "database": {"id": 1, "database_name": "personal"}}])
    ds_id = executive.ensure_dataset(api, 2, "okr_progress", "SELECT 1")
    assert ds_id == 55
    assert api.created == []
    assert any(kw.get("database_id") == 2 for _, kw in api.updated_datasets)

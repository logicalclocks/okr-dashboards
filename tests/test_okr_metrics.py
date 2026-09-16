"""OKR target names and the populations behind them.

Setup writes four target names into the `okrs` feature group. Each has to resolve to a query, and
each query has to count what the setup question asked for.
"""

import pytest

SETUP_TARGET_NAMES = [
    "features",
    "feature views (models)",
    "model deployments",
    "agent deployments",
]


@pytest.mark.parametrize("name", SETUP_TARGET_NAMES)
def test_every_name_setup_writes_resolves_to_a_metric(executive, name):
    """okrs.sh wrote 'feature views (models)' while the builder looked up 'models', so a
    configured target of 10 rendered as a target of zero with NULL attainment."""
    canonical = executive.TARGET_ALIASES.get(name, name)
    assert canonical in executive.SUPPORTED_METRICS


def test_the_older_target_name_still_resolves(executive):
    """Existing feature groups hold the old name; rows are renamed on read, not rewritten."""
    assert executive.TARGET_ALIASES["models"] == "feature views (models)"


def test_every_supported_metric_has_a_query(executive):
    assert set(executive.SUPPORTED_METRICS) == set(executive.actual_sql("asset_lifecycle"))


def test_deployment_actuals_separate_models_from_agents(executive):
    """Both are rows in `serving`; the model artifact is what tells them apart. Counting the
    table alone reported every agent as a model and left agents at a literal zero."""
    sql = executive.actual_sql("asset_lifecycle")
    model, agent = sql["model deployments"], sql["agent deployments"]

    assert "serving_model_artifact" in model
    assert "NOT EXISTS" in agent and "NOT EXISTS" not in model


def test_deployment_actuals_are_filtered_to_production(executive):
    """okrs.sh asks for the target number of *production* deployments."""
    for key in ("model deployments", "agent deployments"):
        sql = executive.actual_sql("asset_lifecycle")[key]
        assert "model_registry_tag_value" in sql
        assert "'asset_lifecycle'" in sql
        assert "'prod'" in sql


def test_the_lifecycle_tag_reaches_the_query(executive):
    assert "'my_tag'" in executive.actual_sql("my_tag")["model deployments"]


def test_build_sql_emits_one_row_per_target(executive):
    sql = executive.build_sql({"features": 10, "agent deployments": 2}, "asset_lifecycle")
    assert sql.count("AS metric") == 2
    assert "'features'" in sql and "'agent deployments'" in sql


def test_an_unsupported_metric_is_not_silently_zero(executive):
    """A target with no query used to fall back to the literal 0, which reads as 'nothing built
    yet' rather than as a broken dashboard."""
    with pytest.raises(KeyError):
        executive.build_sql({"not a real metric": 1}, "asset_lifecycle")


def test_feature_and_feature_view_actuals_count_production_only(executive):
    """The KPI panels filter on the lifecycle tag's prod status; the detail row beside them must
    count the same population. With one production view and 99 in development against a target
    of 10, the KPI read 10% while this row read 1000%."""
    sql = executive.actual_sql("asset_lifecycle")
    for key in ("features", "feature views (models)"):
        assert "= 'prod'" in sql[key], key
        assert "'asset_lifecycle'" in sql[key], key

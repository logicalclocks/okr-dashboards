"""Counting assets in the promotion dashboard.

`artifact_id` is only unique within an `artifact_type`: the ids come from separate tables, so a
feature group and a feature view that both happen to be id 42 are two assets. Counting the id
alone collapsed them, and only in the project series, where different kinds share a bar.
"""


def test_assets_are_counted_on_the_composite_identity(promotion):
    specs = promotion.chart_specs(["dev", "prod"], "project_name")
    rendered = repr(specs)
    assert "COUNT(DISTINCT artifact_type, artifact_id)" in rendered
    assert "COUNT(DISTINCT artifact_id)" not in rendered

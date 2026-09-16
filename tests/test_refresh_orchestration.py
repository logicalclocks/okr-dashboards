"""The refresh has to cover the dashboards whose numbers are snapshots.

Most dashboards query the analytics connection when opened, so they are live by construction. The
executive dashboard embeds its OKR targets and duplicate-feature counts as SQL literals fixed when
its builder runs, and the Refresh Dashboard Now job rebuilt only the tag datasets.
"""

from conftest import load


def test_the_executive_builder_is_part_of_the_refresh():
    refresh = load("refresh_dashboards.py", stub=())
    scripts = [script for _, script in refresh.BUILDERS]
    assert "create_executive_dashboard.py" in scripts
    assert "create_tag_dataset.py" in scripts


def test_tag_datasets_are_rebuilt_before_the_executive_dashboard():
    """The executive charts read the tag status columns the tag datasets define."""
    refresh = load("refresh_dashboards.py", stub=())
    scripts = [script for _, script in refresh.BUILDERS]
    assert scripts.index("create_tag_dataset.py") < scripts.index("create_executive_dashboard.py")

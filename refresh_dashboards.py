"""Refresh every dashboard whose numbers are a snapshot rather than a live read.

The Hopsworks UI's "Refresh Dashboard Now" action runs the `update-tag-dataset` job, and that
job used to run `create_tag_dataset.py` alone. Most dashboards are fine with that: their charts
query the analytics connection when they are opened, so they are live by construction.

The executive dashboard is not. Its OKR targets and its duplicate-feature counts are embedded as
SQL literals when `create_executive_dashboard.py` runs, so editing the `okrs` feature group or
running the nightly duplicate detector changed nothing a viewer could see, and the refresh action
did not run that builder. The dashboard kept reporting last quarter's targets with no indication
that it was doing so.

This runs both, in order, and reports each. Point the `update-tag-dataset` job at this instead of
at `create_tag_dataset.py`.

Exit code is the first failure's, so a job that half-succeeded is not reported as success. Each
builder is idempotent, so re-running after a partial failure is safe.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone

# Ordered. The tag datasets come first because the executive dashboard's charts read the tag
# status columns they define, so a fresh executive build on stale tag data is the one combination
# worth avoiding.
BUILDERS = [
    ("tag datasets", "create_tag_dataset.py"),
    ("executive dashboard", "create_executive_dashboard.py"),
]


def main() -> int:
    args = sys.argv[1:]
    started = datetime.now(timezone.utc)
    print(f"Refreshing snapshot-backed dashboards at {started:%Y-%m-%d %H:%M UTC}\n")

    failures = []
    for label, script in BUILDERS:
        print(f"=== {label} ({script}) ===")
        # Resolved against this file, not the working directory: a Hopsworks job does not
        # necessarily run from the checkout, and a bare name would fail there with a
        # "can't open file" that reads as a missing builder rather than a wrong cwd.
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
        result = subprocess.run([sys.executable, path, *args], check=False)
        if result.returncode != 0:
            print(f"--- {label} FAILED (exit {result.returncode}) ---\n")
            failures.append((label, result.returncode))
        else:
            print(f"--- {label} ok ---\n")

    if failures:
        for label, code in failures:
            print(f"FAILED: {label} (exit {code})", file=sys.stderr)
        return failures[0][1]

    print(f"All {len(BUILDERS)} builders refreshed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

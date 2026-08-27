"""Seed a realistic dev -> qa -> uat -> prod history for the lifecycle tag. Demo clusters only.

`seed_asset_lifecycle.py` promotes assets through the stages over a couple of seconds, because it
drives the real REST API and the backend timestamps each write as it happens. That proves the
history records transitions, which is what it is for, but it leaves every duration at zero, so the
promotion-time dashboard has nothing to show: an average of nothing, a maximum of nothing, and a
distribution with one bar at the origin.

This writes the history rows directly, backdated, so those charts have something to say. It is a
demo aid and nothing else:

  - It requires a privileged MySQL account. The analytics read-only user cannot write here, by
    design.
  - It DELETES the existing history for the tag before seeding, so the result is one coherent set
    of journeys rather than backdated rows interleaved with whatever was already recorded.
  - It never runs as part of the setup flow.

The rows it writes are indistinguishable from real ones, deliberately: `event_id` is the same
sha256 over the same tuple the backend hashes, so the unique key still rejects a duplicated
transition and the interval derivation still pairs OPENED with CLOSED. Seeding rows the reader
would treat differently from real ones would make the dashboard a demo of itself.

Run:  MYSQL_HOST=... MYSQL_USER=... MYSQL_PASSWORD=... python seed_promotion_history.py --emit-sql
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

SEPARATOR = ""
TAG = "asset_lifecycle"
STATUS_KEY = "status"
OWNER_KEY = "owner"
STAGES = ["dev", "qa", "uat", "prod"]
OWNERS = ["fraud-platform", "risk-analytics", "ml-engineering"]

# How long a stage tends to last, in days: (low, high). Wide on purpose — a distribution chart
# over three near-identical values is not a distribution.
STAGE_DAYS = {"dev": (4, 45), "qa": (2, 21), "uat": (1, 14)}

# How far each asset gets. Most reach production; some are still on the way, which is what makes
# "assets completing each transition" mean anything.
JOURNEYS = [(4, 0.45), (3, 0.25), (2, 0.20), (1, 0.10)]


@dataclass(frozen=True)
class Event:
    artifact_type: str
    artifact_id: int
    tag_key: str
    event_type: str
    at: datetime
    value: str
    project_id: int | None
    project_name: str | None

    def event_id(self) -> str:
        canonical = SEPARATOR.join(
            [
                self.artifact_type,
                str(self.artifact_id),
                TAG,
                self.tag_key,
                self.event_type,
                str(int(self.at.timestamp() * 1000)),
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sql_str(value) -> str:
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def journey(rng: random.Random, asset, now: datetime) -> list[Event]:
    """One asset's history: the stages it passed through, and when."""
    artifact_type, artifact_id, project_id, project_name = asset
    reached, _ = rng.choices([j for j, _ in JOURNEYS], [w for _, w in JOURNEYS])[0], None
    stages = STAGES[:reached]
    owner = OWNERS[artifact_id % len(OWNERS)]

    # Work backwards from a point in the past so finished journeys land at different dates and the
    # "by week reached" chart has a spread rather than a spike.
    total = sum(
        rng.uniform(*STAGE_DAYS[s]) for s in stages[:-1]
    )
    start = now - timedelta(days=total + rng.uniform(1, 40))

    events: list[Event] = []
    at = start
    events.append(Event(artifact_type, artifact_id, OWNER_KEY, "OPENED", at, owner,
                        project_id, project_name))
    for i, stage in enumerate(stages):
        events.append(Event(artifact_type, artifact_id, STATUS_KEY, "OPENED", at, stage,
                            project_id, project_name))
        if i < len(stages) - 1:
            at = at + timedelta(days=rng.uniform(*STAGE_DAYS[stage]))
            events.append(Event(artifact_type, artifact_id, STATUS_KEY, "CLOSED", at, stage,
                                project_id, project_name))
    return events


def statements(assets, seed: int) -> list[str]:
    rng = random.Random(seed)
    now = datetime.now()
    out = [
        f"DELETE FROM hopsworks.tag_history WHERE tag_name = {sql_str(TAG)};",
    ]
    for asset in assets:
        for e in journey(rng, asset, now):
            out.append(
                "INSERT INTO hopsworks.tag_history "
                "(artifact_type, artifact_id, tag_name, tag_key, event_type, event_time, "
                "event_id, tag_value, project_id, project_name) VALUES ("
                f"{sql_str(e.artifact_type)}, {e.artifact_id}, {sql_str(TAG)}, "
                f"{sql_str(e.tag_key)}, {sql_str(e.event_type)}, "
                f"{sql_str(e.at.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3])}, "
                f"{sql_str(e.event_id())}, {sql_str(e.value)}, "
                f"{e.project_id if e.project_id is not None else 'NULL'}, "
                f"{sql_str(e.project_name)});"
            )
    return out


ASSETS_QUERY = f"""
SELECT DISTINCT h.artifact_type, h.artifact_id, h.project_id, h.project_name
FROM hopsworks.tag_history h
WHERE h.tag_name = {sql_str(TAG)}
ORDER BY h.artifact_type, h.artifact_id;
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument(
        "--assets-tsv",
        help="tab-separated artifact_type/id/project_id/project_name, for when this runs "
             "somewhere without a direct MySQL route (see ASSETS_QUERY)",
    )
    parser.add_argument("--emit-sql", action="store_true",
                        help="print the SQL instead of executing it")
    args = parser.parse_args()

    if args.assets_tsv:
        assets = []
        with open(args.assets_tsv) as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 4:
                    assets.append((parts[0], int(parts[1]),
                                   int(parts[2]) if parts[2] not in ("", "NULL") else None,
                                   parts[3] if parts[3] != "NULL" else None))
    else:
        import os
        import pymysql

        conn = pymysql.connect(
            host=os.environ["MYSQL_HOST"], user=os.environ["MYSQL_USER"],
            password=os.environ.get("MYSQL_PASSWORD", ""), database="hopsworks",
        )
        try:
            with conn.cursor() as cur:
                cur.execute(ASSETS_QUERY)
                assets = list(cur.fetchall())
        finally:
            conn.close()

    if not assets:
        print(f"no assets currently carrying {TAG!r}; run seed_asset_lifecycle.py first")
        return 1

    sql = statements(assets, args.seed)
    if args.emit_sql:
        print("\n".join(sql))
        return 0

    import os
    import pymysql

    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"], user=os.environ["MYSQL_USER"],
        password=os.environ.get("MYSQL_PASSWORD", ""), database="hopsworks",
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            for statement in sql:
                cur.execute(statement)
    finally:
        conn.close()
    print(f"seeded {len(sql) - 1} event(s) across {len(assets)} asset(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

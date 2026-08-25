"""Attach a dev -> qa -> prod lifecycle tag to every taggable asset in a project.

This exists to give the Asset Lifecycle dashboard something real to read. It creates the
`asset_lifecycle` tag schema with history archiving switched on, attaches it to every feature
group, feature view, training dataset, model and deployment in the project, and then promotes a
share of them through the stages so the history contains actual transitions rather than a single
opening event per asset.

Promotion is what makes the data interesting, and it is also the only thing that exercises the
part of the feature that matters: a tag write is an upsert in place, so `dev -> qa` mutates one
row and creates nothing. Only the history records that it happened.

Archiving is turned on BEFORE anything is attached. Turning it on afterwards would backfill each
attachment at its attach time and record no transitions, because the earlier states are already
gone from the live row.

Run:  python seed_asset_lifecycle.py --project debitcard_fraud
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib3

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TAG_NAME = "asset_lifecycle"
TAG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "Lifecycle stage of the asset",
            "enum": ["dev", "qa", "uat", "prod"],
        },
        "owner": {"type": "string", "description": "Team accountable for the asset"},
    },
    "required": ["status"],
    "additionalProperties": False,
}

# How far through the lifecycle each asset gets. Most things reach qa, fewer reach prod: a
# lifecycle dashboard where everything is in one stage shows nothing.
PATHS = [
    (["dev"], 0.20),
    (["dev", "qa"], 0.35),
    (["dev", "qa", "prod"], 0.35),
    (["dev", "qa", "uat", "prod"], 0.10),
]

OWNERS = ["fraud-platform", "risk-analytics", "ml-engineering"]

# Each promotion needs a distinct event time: the history is keyed per millisecond, and two
# transitions inside one millisecond collide on the event id.
PROMOTION_GAP_SECONDS = 1.2


class Hopsworks:
    """The REST calls this needs, which the Python SDK does not expose for every artifact."""

    def __init__(self, host: str, api_key: str, project: str) -> None:
        self.base = f"https://{host}/hopsworks-api/api"
        self.headers = {"Authorization": f"ApiKey {api_key}"}
        self.project_name = project
        self.project_id = self._project_id(project)

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return requests.request(
            method, f"{self.base}{path}", headers=headers, verify=False,
            timeout=60, **kwargs,
        )

    def _project_id(self, name: str) -> int:
        r = self._call("GET", "/project")
        r.raise_for_status()
        for entry in r.json():
            # The listing is wrapped in a team membership record for an API key and flat for a
            # JWT, so unwrap rather than assuming either.
            p = entry.get("project", entry)
            if p.get("name") == name:
                return p["id"]
        raise SystemExit(f"project {name!r} not found")

    # -- schema ------------------------------------------------------------- #

    def ensure_schema(self) -> None:
        """Create the schema with archiving on, or turn archiving on if it exists.

        Order matters: archiving has to be on before the first attach, or the first states are
        never recorded as transitions.
        """
        r = self._call(
            "POST",
            f"/tags?name={TAG_NAME}&archive=true",
            data=json.dumps(TAG_SCHEMA),
            headers={"Content-Type": "application/json"},
        )
        if r.status_code in (200, 201):
            print(f"created tag schema {TAG_NAME!r} with history archiving on")
            return
        # Already there: make sure archiving is on regardless of how it was created.
        r = self._call("PUT", f"/tags/{TAG_NAME}/archive?value=true")
        if r.ok:
            print(f"tag schema {TAG_NAME!r} exists; archiving enabled")
        else:
            raise SystemExit(f"could not enable archiving: {r.status_code} {r.text[:200]}")

    # -- assets ------------------------------------------------------------- #

    def _items(self, path: str) -> list[dict]:
        r = self._call("GET", path)
        if not r.ok:
            return []
        body = r.json()
        return body.get("items") or [] if isinstance(body, dict) else body

    def assets(self) -> list[tuple[str, str, str]]:
        """[(kind, label, tag_path)] for everything taggable in the project."""
        p = f"/project/{self.project_id}"
        fs = self._items(f"{p}/featurestores")
        found: list[tuple[str, str, str]] = []

        for store in fs:
            sid = store["featurestoreId"]
            for fg in self._items(f"{p}/featurestores/{sid}/featuregroups"):
                found.append(("feature group", f"{fg['name']} v{fg['version']}",
                              f"{p}/featurestores/{sid}/featuregroups/{fg['id']}/tags"))
            for fv in self._items(f"{p}/featurestores/{sid}/featureview"):
                found.append(("feature view", f"{fv['name']} v{fv['version']}",
                              f"{p}/featurestores/{sid}/featureview/{fv['name']}"
                              f"/version/{fv['version']}/tags"))

        for reg in self._items(f"{p}/modelregistries"):
            rid = reg["id"]
            for m in self._items(f"{p}/modelregistries/{rid}/models"):
                found.append(("model", m["id"],
                              f"{p}/modelregistries/{rid}/models/{m['id']}/tags"))

        for d in self._items(f"{p}/serving"):
            found.append(("deployment", d["name"],
                          f"{p}/serving/{d['id']}/tags"))

        return found

    def tag(self, tag_path: str, value: dict) -> bool:
        r = self._call(
            "PUT",
            f"{tag_path}/{TAG_NAME}",
            data=json.dumps(value),
            headers={"Content-Type": "application/json"},
        )
        if not r.ok:
            print(f"    ! {r.status_code} {r.text[:160]}")
        return r.ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--host", default=os.environ.get("HOPSWORKS_HOST", "10.115.12.130"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    api_key = os.environ.get("HOPSWORKS_API_KEY")
    if not api_key:
        raise SystemExit("HOPSWORKS_API_KEY is not set")

    rng = random.Random(args.seed)
    hw = Hopsworks(args.host, api_key, args.project)
    hw.ensure_schema()

    assets = hw.assets()
    if not assets:
        raise SystemExit("no taggable assets found in the project")

    print(f"\nfound {len(assets)} taggable asset(s):")
    for kind, label, _ in assets:
        print(f"  {kind:<15} {label}")

    paths, weights = zip(*PATHS)
    plans = rng.choices(paths, weights=weights, k=len(assets))
    longest = max(len(p) for p in plans)

    # Walk every asset one stage at a time rather than finishing each asset in turn, so the
    # transitions interleave in time and the "by week entered" chart has a spread to show.
    print("\npromoting:")
    for step in range(longest):
        for (kind, label, path), plan in zip(assets, plans):
            if step >= len(plan):
                continue
            stage = plan[step]
            owner = OWNERS[hash(str(label)) % len(OWNERS)]
            ok = hw.tag(path, {"status": stage, "owner": owner})
            print(f"  {'ok ' if ok else 'FAIL'} {kind:<15} {str(label):<32} -> {stage}")
        if step < longest - 1:
            time.sleep(PROMOTION_GAP_SECONDS)

    print("\nDone. Build the dashboard with: python create_lifecycle_dashboard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

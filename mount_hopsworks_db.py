#!/usr/bin/env python
"""Create external feature groups for a requested set of tables in the
`hopsworks` database, read through the `hopsworks_analytics` data source.

One program that does it all:

  * parses the requested table list (a separator-less concatenation) against the
    connector's real table list via tolerant longest-prefix matching,
  * creates an external (on-demand) feature group per table, and
  * verifies the final state.

Schema handling (three paths, tried in order per table):
  0. Platform intelligence (infer_metadata) — when enabled on the cluster, an
     LLM proposes per-column renames, Hopsworks types, descriptions, and a
     suggested primary key / event time. This is the primary path. If platform
     intelligence is not enabled, it is skipped (probed once on the first table
     and disabled for the rest of the run).
  1. Backend introspection — pass no features and let the backend read the MySQL
     schema server-side. Works for tables whose preview returns rows.
  2. information_schema fallback — for tables whose preview is empty (so the
     backend infers zero features), read column names/types from
     information_schema.columns and pass an explicit, typed feature list.

Statistics are disabled (the backend rejects statistics/monitoring on on-demand
feature groups).

Usage:
    python mount_hopsworks_db.py            # reconcile, create missing FGs, then verify
    python mount_hopsworks_db.py plan       # parse + reconcile only, create nothing
    python mount_hopsworks_db.py verify     # report which requested tables have FGs
    python mount_hopsworks_db.py <N>        # create only the first N tables (test mode)
"""
from __future__ import annotations

import re
import sys
import time
import warnings

import hopsworks
from hopsworks_common.client.exceptions import PlatformIntelligenceException
from hsfs.feature import Feature

warnings.filterwarnings("ignore")

SOURCE_DB = "hopsworks"
# Must match HopsworksAnalyticsController.RO_CONNECTOR_NAME in hopsworks-ee: the backend creates the read-only
# data source under this name, so anything else fails with a not-found before a single table is mounted.
CONNECTOR = "hopsworks_analytics"
VERSION = 1

# Retry policy for transient LLM inference 500s (errorCode 520013).
INFER_RETRIES = 4
INFER_BACKOFF = 3  # seconds; multiplied by attempt number (3s, 6s, 9s, ...)

# The exact list the user provided (table names concatenated, no separators).
REQUESTED_BLOB = (
    "account_auditactivityalert_receivercached_featurecached_feature_extra_constraints"
    "cached_feature_groupdata_sourcedatasetdataset_requestdataset_shared_withembedding"
    "embedding_featureenvironmentenvironment_historyenvironment_python_librariesexecutions"
    "expectationexpectation_suitefeature_descriptive_statisticsfeature_groupfeature_group_alert"
    "feature_group_commitfeature_group_descriptive_statisticsfeature_group_feature_usage"
    "feature_group_feature_usage_derived_feature_groupfeature_group_feature_usage_feature_view"
    "feature_group_feature_usage_modelfeature_group_linkfeature_group_statistics"
    "feature_group_transformation_functionsfeature_monitoring_configfeature_monitoring_result"
    "feature_storefeature_store_activityfeature_store_jobfeature_store_keyword"
    "feature_store_mandatory_tagfeature_store_metrics_datafeature_store_metrics_event_log"
    "feature_store_tagfeature_store_tag_valuefeature_viewfeature_view_alert"
    "feature_view_descriptive_statisticsfeature_view_linkfeature_view_loggingfeature_view_statistics"
    "feature_view_transformation_functiongreat_expectationjob_alertjob_schedulejobsmodel"
    "model_linkmodel_versionon_demand_featureon_demand_feature_groupon_demand_optionproject"
    "project_teamservingserving_depl_componentserving_deploymentserving_keyserving_model_artifact"
    "serving_remote_accessshared_featureshared_feature_groupshared_feature_storestream_feature_group"
    "training_datasettransformation_functiontriggered_alerttrino_queriesuserloginsvalidation_result"
)


# --------------------------------------------------------------------------- #
# Parsing the requested list
# --------------------------------------------------------------------------- #
def parse_blob(blob: str, known: set[str]) -> tuple[list[str], list[str]]:
    """Extract the real table names embedded in a separator-less concatenation,
    tolerating junk (names that don't exist) between matches. Returns
    (ordered real tables, unmatched fragments)."""
    ordered: list[str] = []
    unmatched: list[str] = []
    i, junk = 0, ""
    while i < len(blob):
        cand = None
        for name in known:
            if blob.startswith(name, i) and (cand is None or len(name) > len(cand)):
                cand = name
        if cand is None:
            junk += blob[i]
            i += 1
            continue
        if junk:
            unmatched.append(junk)
            junk = ""
        ordered.append(cand)
        i += len(cand)
    if junk:
        unmatched.append(junk)
    return ordered, unmatched


def requested_tables(canon: set[str]) -> tuple[list[str], list[str]]:
    """Return (deduped ordered requested tables, unmatched fragments)."""
    ordered, unmatched = parse_blob(REQUESTED_BLOB, canon)
    return list(dict.fromkeys(ordered)), unmatched


# --------------------------------------------------------------------------- #
# Schema fallback via information_schema
# --------------------------------------------------------------------------- #
def map_type(mysql_t: str) -> str:
    """Map a MySQL data_type to a Hopsworks offline (Hive) type."""
    t = (mysql_t or "").strip().lower()
    if t in ("tinyint", "smallint", "mediumint", "int", "integer", "year"):
        return "int"
    if t == "bigint":
        return "bigint"
    if t == "float":
        return "float"
    if t in ("double", "real", "decimal", "numeric", "dec"):
        return "double"
    if t in ("bit", "bool", "boolean"):
        return "boolean"
    if t == "date":
        return "date"
    if t in ("datetime", "timestamp"):
        return "timestamp"
    if t in ("binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"):
        return "binary"
    return "string"  # char/varchar/text/enum/set/json/time/geometry/...


def sanitize_name(name: str, taken: set[str] | None = None) -> str:
    """Lowercase, replace illegal chars with `_`, and (optionally) dedupe."""
    nm = re.sub(r"[^a-z0-9_]", "_", (name or "").lower()) or "col"
    if taken is not None:
        while nm in taken:
            nm += "_x"
        taken.add(nm)
    return nm


def features_from_information_schema(table_ds, tname: str) -> list[Feature]:
    """Build an explicit, typed feature list by reading column definitions from
    information_schema.columns (used when the backend preview is empty)."""
    table_ds.query = (
        "SELECT column_name AS cn, CAST(data_type AS CHAR) AS dt "
        "FROM information_schema.columns "
        f"WHERE table_schema='{SOURCE_DB}' AND table_name='{tname}' "
        "ORDER BY ordinal_position"
    )
    data = table_ds.get_data()
    table_ds.query = None  # reset so the FG reads the real table
    rows = data.preview.get("preview", []) if isinstance(data.preview, dict) else (data.preview or [])

    features, taken = [], set()
    for r in rows:
        vals = r.get("values", [])
        if len(vals) < 2:
            continue
        cname, dtype = vals[0].get("value1"), vals[1].get("value1")
        nm = sanitize_name(cname, taken)
        features.append(Feature(name=nm, type=map_type(dtype)))
    return features


# --------------------------------------------------------------------------- #
# Schema via platform intelligence (infer_metadata)
# --------------------------------------------------------------------------- #
def features_from_inference(table_ds, tname: str) -> tuple[list[Feature], list[str], str | None]:
    """Use platform intelligence to infer feature metadata for `table_ds`.

    Returns (features, primary_key, event_time). The inferred `new_name` becomes
    the logical feature name (the original column is kept as `column_name`), and
    the inferred type/description are carried through. The suggested primary key
    and event time are remapped onto the sanitized feature names.

    Raises:
        PlatformIntelligenceException: if platform intelligence is not enabled on
        the cluster, or the LLM call fails.
    """
    # SDK workaround: DataSourceData.from_response_json leaves `features` as a
    # list of dicts (it never deserializes them into Feature objects), so the
    # SDK's _infer_metadata raises AttributeError('dict' has no attribute 'name')
    # on the very first table. Fetch the preview ourselves and coerce the dict
    # features into Feature objects before handing them to infer_metadata.
    preview = table_ds.get_data()
    # The preview backend returns no features for an empty table (0 rows), and
    # the inference backend then deterministically 500s (errorCode 520013) with
    # nothing to sample. Skip the doomed LLM call (and its retries) and let the
    # caller fall back to schema introspection / information_schema.
    if not preview.features:
        raise PlatformIntelligenceException(
            PlatformIntelligenceException.INFERENCE_FAILED,
            "preview returned no features — cannot infer metadata from an empty table",
        )
    if isinstance(preview.features[0], dict):
        preview._features = [
            Feature(name=f["name"], type=f.get("type") or "string")
            for f in preview.features
        ]
    # The LLM backend intermittently 500s (errorCode 520013, surfaced as
    # INFERENCE_FAILED) under load — retry a few times with backoff before
    # letting the caller fall back to introspection for this table. A
    # NOT_CONFIGURED verdict is not transient, so it propagates immediately.
    for attempt in range(INFER_RETRIES):
        try:
            inferred = table_ds.infer_metadata(preview_data=preview)
            break
        except PlatformIntelligenceException as exc:
            if (exc.reason != PlatformIntelligenceException.INFERENCE_FAILED
                    or attempt == INFER_RETRIES - 1):
                raise
            time.sleep(INFER_BACKOFF * (attempt + 1))

    features, taken, name_map = [], set(), {}
    for f in inferred.features:
        original = f.original_name
        nm = sanitize_name(f.new_name or original, taken)
        if f.new_name:
            name_map[f.new_name] = nm
        # infer_metadata returns Hopsworks types directly (bigint/string/...),
        # so use them as-is; only column_name carries the physical source name.
        features.append(
            Feature(
                name=nm,
                type=f.type or "string",
                description=f.description,
                column_name=original,
            )
        )

    feature_names = {feat.name for feat in features}

    def remap(new_name: str) -> str | None:
        nm = name_map.get(new_name, sanitize_name(new_name))
        return nm if nm in feature_names else None

    primary_key = [pk for pk in (remap(p) for p in inferred.suggested_primary_key) if pk]
    event_time = remap(inferred.suggested_event_time) if inferred.suggested_event_time else None
    return features, primary_key, event_time


# --------------------------------------------------------------------------- #
# Creating one external feature group
# --------------------------------------------------------------------------- #
def create_one(fs, table_ds, tname: str, pi_state: dict) -> tuple[str, int]:
    """Create one external FG. Returns (method, n_features).

    Tries, in order: platform intelligence (infer_metadata) when enabled, then
    backend introspection, then — if the backend reports no features (empty
    preview) — an explicit information_schema schema.

    `pi_state` is a one-element mutable cache ({"enabled": None|bool}) so that a
    "platform intelligence not enabled" verdict (a cluster-wide setting) is
    probed once and then skipped for the rest of the run.
    """
    desc = f"External feature group mounted from `{SOURCE_DB}`.`{tname}` via {CONNECTOR}."

    def _save(features, primary_key=None, event_time=None):
        fg = fs.create_external_feature_group(
            name=tname,
            version=VERSION,
            data_source=table_ds,
            description=desc,
            features=features or [],
            primary_key=primary_key or [],
            event_time=event_time,
            online_enabled=False,
            statistics_config=False,  # on-demand FGs reject statistics/monitoring
        )
        fg.save()
        return fg

    # 0. Platform intelligence — primary path, skipped once we learn it is
    #    unavailable. Both the typed "not configured" verdict and a structural
    #    failure (the SDK/backend can't serve inference on this cluster) are
    #    cluster-wide, so they disable inference for the rest of the run; a
    #    transient LLM failure only skips inference for the current table.
    if pi_state["enabled"] is not False:
        inferred = None
        try:
            inferred = features_from_inference(table_ds, tname)
            pi_state["enabled"] = True
        except PlatformIntelligenceException as exc:
            if exc.reason == PlatformIntelligenceException.NOT_CONFIGURED:
                pi_state["enabled"] = False
                print("  platform intelligence not enabled -> skipping inference")
            else:
                print(f"  infer_metadata failed ({exc}); falling back for this table")
        except Exception as exc:  # noqa: BLE001 - SDK/backend can't serve inference here
            pi_state["enabled"] = False
            print(f"  infer_metadata unavailable ({type(exc).__name__}: {exc}); skipping inference")

        if inferred is not None:
            features, primary_key, event_time = inferred
            if features:
                fg = _save(features, primary_key, event_time)
                return "infer_metadata", len(fg.features or [])

    # 1. Backend introspection.
    try:
        fg = _save(None)  # backend introspects the schema server-side
        return "introspect", len(fg.features or [])
    except Exception as exc:
        if "No features were provided" not in str(exc):
            raise
        # Empty preview -> backend found no columns. Supply schema explicitly.
        features = features_from_information_schema(table_ds, tname)
        if not features:
            raise RuntimeError("information_schema returned no columns") from exc
        fg = _save(features)
        return "information_schema", len(fg.features or [])


def has_fg(fs, tname: str):
    try:
        fg = fs.get_feature_group(tname, version=VERSION)
        if fg is not None and fg.features:
            return fg
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def report_reconciliation(canon: dict, requested: list[str], unmatched: list[str]) -> None:
    not_listed = sorted(set(canon) - set(requested))
    print(f"Matched {len(requested)} real table(s) from your list (out of {len(canon)} in the DB).")
    if unmatched:
        print(f"\n{len(unmatched)} fragment(s) in your list did NOT match any real table (skipped):")
        for u in unmatched:
            print(f"  ?? {u}")
    if not_listed:
        print(f"\n{len(not_listed)} real table(s) NOT in your list (not created):")
        print("   " + ", ".join(not_listed))


def do_verify(fs, requested: list[str]) -> None:
    existing = {fg.name for fg in fs.get_feature_groups()}
    present = [t for t in requested if t in existing]
    missing = [t for t in requested if t not in existing]
    print("\n==================== VERIFY ====================")
    print(f"requested: {len(requested)}   present as FG: {len(present)}   missing: {len(missing)}")
    if missing:
        print(f"missing -> {missing}")
    print(f"total FGs in project now: {len(existing)}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "create"

    project = hopsworks.login()
    fs = project.get_feature_store()
    ds = fs.get_data_source(CONNECTOR)
    canon = {t.table: t for t in ds.get_tables(database=SOURCE_DB)}

    requested, unmatched = requested_tables(set(canon))
    report_reconciliation(canon, requested, unmatched)

    if mode == "verify":
        do_verify(fs, requested)
        return

    if mode == "plan":
        print("\nWould create external FGs for:")
        for t in requested:
            print(f"  - {t}")
        print(f"\nPLAN ONLY. {len(requested)} FG(s) would be created. Nothing was changed.")
        return

    if mode.isdigit():
        requested = requested[: int(mode)]
        print(f"\n[test mode] limiting to first {len(requested)} table(s)")

    created, skipped, failed = [], [], []
    pi_state = {"enabled": None}  # one-time platform-intelligence probe cache
    for i, tname in enumerate(requested, 1):
        prefix = f"[{i}/{len(requested)}] {tname}"

        if has_fg(fs, tname) is not None:
            print(f"{prefix}: already exists -> skip")
            skipped.append(tname)
            continue

        try:
            method, nfeat = create_one(fs, canon[tname], tname, pi_state)
            print(f"{prefix}: created ({nfeat} features, via {method})")
            created.append(tname)
        except Exception as exc:
            failed.append((tname, str(exc)))
            print(f"{prefix}: FAILED -> {str(exc)[:200]}")

    print("\n==================== SUMMARY ====================")
    print(f"created: {len(created)}  skipped: {len(skipped)}  failed: {len(failed)}")
    if failed:
        print("\nFailures:")
        for name, err in failed:
            print(f"  - {name}: {err[:200]}")

    do_verify(fs, requested)


if __name__ == "__main__":
    main()

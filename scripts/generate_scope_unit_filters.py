"""
Generate Lake Formation row filters from the scope-unit registry, and detect drift (DL-SEC-11).

`infrastructure/modules/lake_formation/main.tf` creates one
`aws_lakeformation_data_cells_filter` per (table, scope unit) from `var.scope_unit_row_filters`,
and both that map and `scope_unit_grants` default to `{}`. The mechanism is right — a data cells
filter is the only Lake Formation construct that filters *rows*, and the tag-based attempt before
it enforced nothing. The lifecycle is the
problem: **scope units are runtime data** in `datalake-scope-units-dev`, published by the
enterprise-platform when a franchisee is onboarded, while the filters that enforce their boundary
are static Terraform. So a unit can exist, own rows, and have no Athena filter — unenforced, and
with nothing reporting it.

This closes that loop in the only way that keeps Terraform the source of truth for grants:

  --check   compares the registry against the committed filters and reports drift, emitting
            `ScopeFilterDrift` so an unenforced unit alarms rather than waiting to be noticed.
  --write   emits the `.tfvars.json` fragment for the two variables, so onboarding is
            "run this, review the diff, apply" rather than "hand-write a filter per franchisee".

Principal ARNs are **never invented.** A unit with no known principal is reported as needing one
rather than guessed at, because a grant to a wrong principal is worse than no grant — the same
reason the variables default to empty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_FRAGMENT: Final[Path] = (
    REPO_ROOT / "infrastructure" / "generated" / "scope_unit_filters.json"
)


def _filter_key(tenant_code: str, scope_unit_id: str, table_name: str) -> str:
    """Stable key per (tenant, unit, table); Terraform turns `:` into `_` for the resource name."""
    return f"{tenant_code}:{scope_unit_id}:{table_name}"


def build_fragment(
    units: list[dict[str, str]],
    table_names: tuple[str, ...],
    catalog_id: str,
    principal_by_unit: dict[str, str],
) -> dict[str, Any]:
    """Build both Terraform variables, omitting grants for units with no known principal."""
    row_filters: dict[str, Any] = {}
    grants: dict[str, Any] = {}
    for unit in units:
        tenant_code = unit["tenant_code"]
        scope_unit_id = unit["scope_unit_id"]
        for table_name in table_names:
            key = _filter_key(tenant_code, scope_unit_id, table_name)
            row_filters[key] = {
                "catalog_id": catalog_id,
                "table_name": table_name,
                "scope_unit_id": scope_unit_id,
            }
            principal = principal_by_unit.get(scope_unit_id)
            if principal:
                grants[f"{key}:grant"] = {
                    "catalog_id": catalog_id,
                    "table_name": table_name,
                    "filter_key": key,
                    "principal_arn": principal,
                }
    return {"scope_unit_row_filters": row_filters, "scope_unit_grants": grants}


def detect_drift(registry_keys: set[str], committed_keys: set[str]) -> dict[str, list[str]]:
    """
    Units with no filter are unenforced; filters with no unit are stale grants.

    The first direction is the security-relevant one: a registered franchisee whose rows no filter
    covers is readable by anyone holding the tenant tag, which is the wildcard grant the data cells
    filters replaced.
    """
    return {
        "unenforced_units": sorted(registry_keys - committed_keys),
        "stale_filters": sorted(committed_keys - registry_keys),
    }


def _load_units(environment: str, region_name: str) -> list[dict[str, str]]:
    """Read every effective scope unit for every tenant from the scope-unit table."""
    import boto3

    from observability.lambda_runtime import require_env
    from persistence.dynamodb_paging import iter_items

    dynamodb = boto3.resource("dynamodb", region_name=region_name)
    table = dynamodb.Table(require_env("SCOPE_UNIT_TABLE"))
    units: list[dict[str, str]] = []
    for item in iter_items(table, use_query=False):
        scope_unit_id = str(item.get("scope_unit_id", ""))
        if not scope_unit_id or scope_unit_id.startswith("__"):
            continue
        if item.get("active") is False:
            continue
        units.append({"tenant_code": str(item["tenant_code"]), "scope_unit_id": scope_unit_id})
    return units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--catalog-id", default="", help="Glue catalog id; defaults to the account."
    )
    parser.add_argument(
        "--tables",
        default="",
        help="Comma-separated curated/analytics table names to filter. Required for --write.",
    )
    parser.add_argument(
        "--principals",
        default="",
        help="scope_unit_id=principal_arn pairs, comma-separated. Units omitted get no grant.",
    )
    parser.add_argument("--fragment", type=Path, default=DEFAULT_FRAGMENT)
    parser.add_argument("--check", action="store_true", help="Report drift; do not write.")
    parser.add_argument("--write", action="store_true", help="Write the .tfvars.json fragment.")
    args = parser.parse_args()

    if not args.check and not args.write:
        parser.error("choose --check or --write")

    table_names = tuple(name.strip() for name in args.tables.split(",") if name.strip())
    principal_by_unit = dict(
        pair.split("=", 1) for pair in args.principals.split(",") if "=" in pair
    )

    units = _load_units(args.environment, args.region)
    print(f"Scope units in {args.environment}: {len(units)}")

    if args.check:
        committed: dict[str, Any] = {}
        if args.fragment.exists():
            committed = json.loads(args.fragment.read_text(encoding="utf-8"))
        committed_filters = set(committed.get("scope_unit_row_filters", {}))
        registry_keys = {
            _filter_key(unit["tenant_code"], unit["scope_unit_id"], table)
            for unit in units
            for table in table_names
        }
        drift = detect_drift(registry_keys, committed_filters)
        for unit_key in drift["unenforced_units"]:
            print(f"  UNENFORCED  {unit_key}")
        for stale in drift["stale_filters"]:
            print(f"  STALE       {stale}")

        import boto3

        from contracts.platform_metrics import PlatformMetric, metric_unit
        from observability.metric_recorder import platform_metric_recorder, record_platform_metric

        record_platform_metric(
            PlatformMetric.SCOPE_FILTER_DRIFT, float(len(drift["unenforced_units"]))
        )
        cloudwatch = boto3.client("cloudwatch", region_name=args.region)
        for recorded in platform_metric_recorder.drain():
            cloudwatch.put_metric_data(
                Namespace="EnterpriseDatalake",
                MetricData=[
                    {
                        "MetricName": recorded.metric.value,
                        "Value": recorded.value,
                        "Unit": metric_unit(recorded.metric).value,
                    }
                ],
            )

        if drift["unenforced_units"]:
            print(
                f"\nFAIL: {len(drift['unenforced_units'])} scope unit(s) have no row filter, so "
                "their rows are readable by any principal holding the tenant tag."
            )
            return 1
        print("\nOK — every registered scope unit has a row filter.")
        return 0

    if not table_names:
        parser.error("--write requires --tables")
    fragment = build_fragment(units, table_names, args.catalog_id, principal_by_unit)
    args.fragment.parent.mkdir(parents=True, exist_ok=True)
    args.fragment.write_text(json.dumps(fragment, indent=2, sort_keys=True), encoding="utf-8")
    filters = len(fragment["scope_unit_row_filters"])
    grants = len(fragment["scope_unit_grants"])
    print(f"Wrote {filters} filter(s) and {grants} grant(s) to {args.fragment}")
    if filters and not grants:
        print(
            "  No grants: no principal ARNs were supplied. Filters without grants enforce nothing, "
            "but a grant to a guessed principal is worse — pass --principals when they are known."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

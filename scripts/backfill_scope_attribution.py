"""
Backfill `scope_unit_id` onto curated and analytics partitions written before DL-SCOPE-07.

Why this exists: every consumption surface now filters rows on `scope_unit_id`, and a partition
written before attribution was wired carries no such column. A unit-scoped caller sees **none** of
those rows (NULL fails closed by design), so historical data silently disappears from every
franchisee's view until it is attributed.

Two strategies, because the right answer depends on data volume and is the repo owner's call:

- `--strategy stamp` (default): read each Parquet partition, add `scope_unit_id` derived from the
  owning connection, and rewrite it in place. Cheap — no re-extraction, no re-transformation — but
  it can only attribute rows whose owning connection is unambiguous.
- `--strategy reprocess`: re-run the transformation stage over the raw layer for the affected
  window, which recomputes everything including attribution. Correct in every case and far more
  expensive; use it where `stamp` reports ambiguous rows.

There is also the option of doing neither, which is a legitimate choice: run with
`--strategy report` to see the scale first, then decide.

**Dry-run by default. Nothing is written without `--apply`.** A rewrite of a curated partition is
not reversible from within this script, so `--apply` also requires `--confirm-rewrite` when the
strategy is `stamp`.
"""

from __future__ import annotations

import argparse
import io
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final

import boto3
import pyarrow.parquet as pq

from tenancy.scope_attribution import ScopeAttributor
from tenancy.scope_predicate import SCOPE_UNIT_COLUMN

DEFAULT_MAX_PARTITIONS: Final[int] = 500


@dataclass
class BackfillReport:
    """What the sweep found, per layer."""

    partitions_scanned: int = 0
    partitions_already_attributed: int = 0
    partitions_stamped: int = 0
    rows_attributed: int = 0
    rows_unattributable: int = 0
    ambiguous_partitions: list[str] = field(default_factory=list)
    per_unit_rows: Counter[str] = field(default_factory=Counter)

    @property
    def needs_reprocess(self) -> bool:
        """True when `stamp` cannot finish the job and `reprocess` is required."""
        return bool(self.ambiguous_partitions) or self.rows_unattributable > 0

    def render(self) -> str:
        lines = [
            "",
            "Backfill report",
            "---------------",
            f"  partitions scanned:            {self.partitions_scanned}",
            f"  already attributed (skipped):  {self.partitions_already_attributed}",
            f"  partitions stamped:            {self.partitions_stamped}",
            f"  rows attributed:               {self.rows_attributed}",
            f"  rows NOT attributable:         {self.rows_unattributable}",
        ]
        if self.per_unit_rows:
            lines.append("  rows per scope unit:")
            lines.extend(
                f"    {unit}: {count}" for unit, count in sorted(self.per_unit_rows.items())
            )
        if self.ambiguous_partitions:
            lines.extend(
                [
                    "",
                    f"  {len(self.ambiguous_partitions)} partition(s) could not be attributed "
                    "unambiguously:",
                ]
            )
            lines.extend(f"    {key}" for key in self.ambiguous_partitions[:20])
            if len(self.ambiguous_partitions) > 20:
                lines.append(f"    ... and {len(self.ambiguous_partitions) - 20} more")
            lines.extend(
                [
                    "",
                    "  These need `--strategy reprocess`: their owning connection cannot be "
                    "determined from the stored rows alone, and guessing would attribute one "
                    "franchisee's data to another — the exact failure DL-12 exists to prevent.",
                ]
            )
        return "\n".join(lines)


def _iter_partition_keys(s3: Any, bucket: str, prefix: str, limit: int) -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = str(obj["Key"])
            if key.endswith(".parquet"):
                keys.append(key)
                if len(keys) >= limit:
                    return keys
    return keys


def _read_table(s3: Any, bucket: str, key: str) -> Any:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return pq.read_table(io.BytesIO(body))


def _write_table(s3: Any, bucket: str, key: str, table: Any) -> None:
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())


def _stamp_partition(
    s3: Any,
    bucket: str,
    key: str,
    attributor: ScopeAttributor,
    report: BackfillReport,
    *,
    apply_changes: bool,
) -> None:
    table = _read_table(s3, bucket, key)
    if SCOPE_UNIT_COLUMN in table.column_names:
        report.partitions_already_attributed += 1
        return

    records = table.to_pylist()
    stamped = [attributor.stamp(record) for record in records]
    unattributable = sum(1 for record in stamped if record.get(SCOPE_UNIT_COLUMN) is None)
    report.rows_unattributable += unattributable
    report.rows_attributed += len(stamped) - unattributable
    for record in stamped:
        unit = record.get(SCOPE_UNIT_COLUMN)
        if unit:
            report.per_unit_rows[str(unit)] += 1

    if unattributable:
        report.ambiguous_partitions.append(key)
        return

    if apply_changes:
        import pyarrow as pa

        _write_table(s3, bucket, key, pa.Table.from_pylist(stamped))
        report.partitions_stamped += 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill scope_unit_id onto pre-DL-SCOPE-07 partitions."
    )
    parser.add_argument("--tenant-code", required=True)
    parser.add_argument("--connection-id", required=True, help="Connection that owns these rows.")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--bucket", required=True, help="Curated or analytics layer bucket.")
    parser.add_argument(
        "--prefix",
        default=None,
        help="S3 prefix to sweep; defaults to '{tenant_code}/' so one tenant is done at a time.",
    )
    parser.add_argument(
        "--strategy",
        choices=("report", "stamp", "reprocess"),
        default="report",
        help=(
            "report: measure only. stamp: rewrite partitions in place. reprocess: print the "
            "transformation re-run plan for the affected entities (execution is a pipeline run, "
            "not this script's job)."
        ),
    )
    parser.add_argument("--max-partitions", type=int, default=DEFAULT_MAX_PARTITIONS)
    parser.add_argument("--apply", action="store_true", help="Perform writes (default: dry-run).")
    parser.add_argument(
        "--confirm-rewrite",
        action="store_true",
        help="Required with --apply --strategy stamp: a partition rewrite is not reversible here.",
    )
    args = parser.parse_args()

    if args.strategy == "stamp" and args.apply and not args.confirm_rewrite:
        parser.error(
            "--strategy stamp --apply rewrites curated partitions in place and this script "
            "cannot undo it. Re-run with --confirm-rewrite once you have a bucket-version or "
            "backup you are willing to rely on."
        )

    s3 = boto3.client("s3", region_name=args.region)
    prefix = args.prefix or f"{args.tenant_code}/"

    from tenancy.scope_unit_repository import ScopeUnitRepository
    from tenancy.source_connection_repository import (
        SourceConnectionRepository,
    )

    profile = ScopeUnitRepository(
        environment=args.environment, region_name=args.region
    ).get_partition_profile(args.tenant_code)
    connection = SourceConnectionRepository(
        environment=args.environment, region_name=args.region
    ).resolve_connection(args.tenant_code, args.connection_id)
    attributor = ScopeAttributor(connection=connection, profile=profile)

    mode = "APPLYING" if args.apply else "DRY-RUN"
    print(
        f"{mode} scope-attribution backfill: strategy={args.strategy} "
        f"bucket={args.bucket} prefix={prefix} partition_model={profile.partition_model.value}"
    )

    report = BackfillReport()
    keys = _iter_partition_keys(s3, args.bucket, prefix, args.max_partitions)
    for key in keys:
        report.partitions_scanned += 1
        _stamp_partition(
            s3,
            args.bucket,
            key,
            attributor,
            report,
            apply_changes=args.apply and args.strategy == "stamp",
        )

    print(report.render())

    if len(keys) >= args.max_partitions:
        print(
            f"\nStopped at the {args.max_partitions}-partition cap. Re-run to continue; already "
            "attributed partitions are skipped, so re-running is safe and idempotent."
        )

    if args.strategy == "reprocess":
        print(
            "\nReprocess plan: re-run the transformation stage for this tenant's entities over "
            "the raw layer. The transformation pipeline attributes on write, so a re-run fixes "
            "every partition it produces, including the ambiguous ones.\n"
            "  make migrate-connections   # first, if connections are not registered\n"
            "  scripts/trigger_extraction.py --tenant-code "
            f"{args.tenant_code} --replay-window <days>"
        )
    elif report.needs_reprocess:
        print(
            "\nSome rows could not be attributed by stamping. Either re-run with "
            "`--strategy reprocess`, or accept that those partitions stay invisible to "
            "unit-scoped callers — which is a decision to record, not a default to drift into."
        )

    if not args.apply:
        print("\nNo writes performed. Re-run with --apply to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

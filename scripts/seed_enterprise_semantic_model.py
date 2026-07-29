"""
Publish the authored §4 enterprise semantic model for a tenant (DL-SEM-03, DL-SEM-04).

Publishes as a **draft** version. That is not a limitation to work around: DL-SEM-04 states a KPI
is not done until its named business owner has signed the definition, and unvalidated definitions
publish to a draft model version only. Forging a signature here would defeat the control.

Once an owner signs, run with `--sign role:controller=ar_invoice.revenue …` (repeatable) and then
`--approve <approver>` plus `--activate` to promote. Approval must come from someone other than
the publisher — maker-checker is enforced by `SemanticModelGovernance`, not by this script.

Also emits the generated methodology document (DL-SEM-05), which is the artefact the transition
package ships and training material consumes.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import boto3

from semantic.enterprise_model import (
    ENTERPRISE_MODEL_VERSION,
    SOW_KPI_MAP,
    build_enterprise_model,
    sign_metric_definition,
)
from semantic.kpi_validation import KpiValidationHarness, structural_expectations
from semantic.metric_lineage import build_methodology_document, methodology_s3_key
from semantic.model_governance import SemanticModelGovernance


def _apply_signatures(model, signatures: list[str]):
    """Each `--sign` argument is `owner=entity.metric`; the owner must match the declared one."""
    signed_at = datetime.now(UTC).isoformat()
    for raw in signatures:
        try:
            owner, target = raw.split("=", 1)
            entity_name, metric_name = target.split(".", 1)
        except ValueError:
            raise SystemExit(
                f"--sign expects owner=entity.metric, got {raw!r} "
                "(e.g. role:controller=ar_invoice.revenue)"
            ) from None
        model = sign_metric_definition(
            model, entity_name, metric_name, signed_by=owner, signed_at=signed_at
        )
        print(f"  signed {entity_name}.{metric_name} by {owner}")
    return model


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish the authored enterprise semantic model (DL-SEM-03/04/05)."
    )
    parser.add_argument("--tenant-code", required=True)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--bucket", required=True, help="Curated bucket holding model bodies.")
    parser.add_argument("--model-version", default=ENTERPRISE_MODEL_VERSION)
    parser.add_argument(
        "--fiscal-year-start-month",
        type=int,
        default=1,
        help="Tenant fiscal year start month (DL-SEM-02); 1 is the Gregorian year.",
    )
    parser.add_argument("--published-by", required=True)
    parser.add_argument(
        "--sign",
        action="append",
        default=[],
        help="Repeatable: owner=entity.metric, e.g. role:controller=ar_invoice.revenue.",
    )
    parser.add_argument(
        "--approve", default=None, help="Approver; must differ from --published-by."
    )
    parser.add_argument("--activate", action="store_true", help="Repoint $latest after approval.")
    parser.add_argument("--apply", action="store_true", help="Perform writes (default: dry-run).")
    args = parser.parse_args()

    model = build_enterprise_model(
        args.tenant_code,
        model_version=args.model_version,
        fiscal_year_start_month=args.fiscal_year_start_month,
    )
    if args.sign:
        print("Applying signatures:")
        model = _apply_signatures(model, args.sign)

    unsigned = model.unsigned_metrics()
    print(
        f"\nModel {args.model_version}: {len(model.entities)} entities, "
        f"{sum(len(e.metrics) for e in model.entities)} metrics, "
        f"{len(unsigned)} unsigned."
    )
    print(f"SOW §4 KPIs covered: {len(SOW_KPI_MAP)}")

    report = KpiValidationHarness(model, structural_expectations(model)).run()
    print("\n" + report.render_summary())
    if not report.passed:
        raise SystemExit("KPI validation failed; refusing to publish.")

    if not args.apply:
        print("\nDRY-RUN: no writes performed. Re-run with --apply to publish.")
        return

    governance = SemanticModelGovernance(
        environment=args.environment, region_name=args.region, s3_bucket=args.bucket
    )
    # allow_draft mirrors DL-SEM-04: an unsigned definition publishes to draft, never active.
    record = governance.publish(model, published_by=args.published_by, allow_draft=bool(unsigned))
    print(f"\nPublished {record.model_version} as {record.status.value}.")

    methodology = build_methodology_document(model)
    key = methodology_s3_key(args.tenant_code, args.model_version)
    boto3.client("s3", region_name=args.region).put_object(
        Bucket=args.bucket,
        Key=key,
        Body=methodology.render_markdown().encode("utf-8"),
        ContentType="text/markdown",
    )
    print(f"Methodology written to s3://{args.bucket}/{key}")

    if args.approve:
        approved = governance.approve(
            args.tenant_code, args.model_version, approved_by=args.approve
        )
        print(f"Approved by {args.approve}: {approved.status.value}.")
        if args.activate:
            active = governance.activate(args.tenant_code, args.model_version)
            print(f"Activated {active.model_version}.")
    elif unsigned:
        print(
            f"\n{len(unsigned)} metric definition(s) are unsigned, so this version cannot be "
            "activated. Collect owner signatures, re-run with --sign, then --approve/--activate."
        )


if __name__ == "__main__":
    main()

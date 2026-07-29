"""
The authored enterprise entity model and KPI set (DL-SEM-03, DL-SEM-04).

Every SOW §4 named definition is expressed here with an unambiguous calculation, grain,
filters, and a named owner *role*. Definitions carry `definition_signed_by = None` on
purpose: a KPI is not done until the named business owner has signed it, and forging a
signature would defeat the control. Consequently this model publishes to a **draft** version
until `sign_metric_definition` is called with the real owner — which is exactly the
`allow_draft` path in `semantic.model_governance`.

Owner roles rather than individuals: the person changes, the accountable role does not, and
`EP-04` resolves a role to a person at authoring time.
"""

from __future__ import annotations

from typing import Final

from semantic.semantic_model import (
    Dimension,
    JoinKind,
    Metric,
    MetricKind,
    NullDenominatorBehaviour,
    SemanticEntity,
    SemanticJoin,
    SemanticModel,
    TimeDimension,
    TimeGrain,
)

ENTERPRISE_MODEL_VERSION: Final[str] = "enterprise-2026-07-v1"

# ── Access tag taxonomy (DL-SEC-09, DL-SEC-10) ──────────────────────────────
# Departments map to tags; the executive tier spans them. A Sales analyst holding
# `dept-sales` cannot see `dept-finance` metrics such as AP bills.
TAG_FINANCE: Final[str] = "dept_finance"
TAG_OPERATIONS: Final[str] = "dept_operations"
TAG_SALES_MARKETING: Final[str] = "dept_sales_marketing"
TAG_EXECUTIVE: Final[str] = "tier_executive"
TAG_PII: Final[str] = "class_pii"

DEPARTMENT_TAGS: Final[frozenset[str]] = frozenset(
    {TAG_FINANCE, TAG_OPERATIONS, TAG_SALES_MARKETING}
)

# The executive tier spans every department for the brands a caller is granted.
EXECUTIVE_TAG_EXPANSION: Final[frozenset[str]] = DEPARTMENT_TAGS | {TAG_EXECUTIVE}

# ── Owner roles ─────────────────────────────────────────────────────────────
OWNER_CFO: Final[str] = "role:cfo"
OWNER_CONTROLLER: Final[str] = "role:controller"
OWNER_VP_OPERATIONS: Final[str] = "role:vp-operations"
OWNER_VP_MARKETING: Final[str] = "role:vp-marketing"
OWNER_VP_FRANCHISE: Final[str] = "role:vp-franchise-development"
STEWARD_DATA: Final[str] = "role:data-steward"


def expand_access_tags(granted: frozenset[str]) -> frozenset[str]:
    """An executive-tier grant expands to every department tag (DL-SEC-10)."""
    if TAG_EXECUTIVE in granted:
        return granted | EXECUTIVE_TAG_EXPANSION
    return granted


def _dimension(
    name: str,
    column: str,
    owner: str,
    *,
    access_tag: str | None = None,
    classification: str = "internal",
    description: str = "",
) -> Dimension:
    return Dimension(
        name=name,
        column=column,
        access_tag=access_tag,
        business_owner=owner,
        steward=STEWARD_DATA,
        classification=classification,
        description=description,
    )


def _metric(
    name: str,
    aggregation: str,
    column: str,
    owner: str,
    definition: str,
    *,
    access_tag: str | None = None,
    unit: str = "",
) -> Metric:
    return Metric(
        name=name,
        aggregation=aggregation,  # type: ignore[arg-type]
        column=column,
        access_tag=access_tag,
        business_owner=owner,
        steward=STEWARD_DATA,
        definition=definition,
        unit=unit,
    )


def _ratio(
    name: str,
    numerator: str,
    denominator: str,
    owner: str,
    definition: str,
    *,
    access_tag: str | None = None,
    unit: str = "ratio",
) -> Metric:
    return Metric(
        name=name,
        aggregation="sum",
        column="*",
        kind=MetricKind.RATIO,
        numerator_metric=numerator,
        denominator_metric=denominator,
        null_denominator=NullDenominatorBehaviour.NULL,
        access_tag=access_tag,
        business_owner=owner,
        steward=STEWARD_DATA,
        definition=definition,
        unit=unit,
    )


_COMMON_TIME = TimeDimension(
    name="activity_date",
    column="activity_date",
    grain=TimeGrain.DAY,
    description="Business event date, truncated to the requested grain on the fiscal calendar.",
)

_BRAND_DIMENSION = _dimension(
    "brand_code",
    "brand_code",
    OWNER_VP_FRANCHISE,
    description="First-class brand dimension; drives brand-level row security (DL-SEC-11).",
)
_SCOPE_DIMENSION = _dimension(
    "scope_unit_id",
    "scope_unit_id",
    OWNER_VP_FRANCHISE,
    description="Owning franchisee or scope unit; the row-security column (DL-SCOPE-09).",
)


# ---------------------------------------------------------------------------
# Entities (DL-SEM-03)
# ---------------------------------------------------------------------------


def _company_entity() -> SemanticEntity:
    return SemanticEntity(
        name="company",
        entity_type="company",
        definition="Account or company golden record consolidated across contributing sources.",
        business_owner=OWNER_VP_OPERATIONS,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("industry", "industry", OWNER_VP_OPERATIONS),
            _dimension("company_type", "company_type", OWNER_VP_OPERATIONS),
            _dimension("country", "country", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "customer_count",
                "count_distinct",
                "golden_id",
                OWNER_VP_OPERATIONS,
                "Distinct company golden records in scope for the period.",
                unit="count",
            ),
            _metric(
                "new_customer_count",
                "count_distinct",
                "new_customer_golden_id",
                OWNER_VP_OPERATIONS,
                "Companies whose first recognised order or job falls inside the period. "
                "New-customer test: no prior order in any earlier period for the same brand.",
                unit="count",
            ),
        ),
        joins=(
            SemanticJoin(
                target_entity="franchisee",
                kind=JoinKind.LEFT,
                local_column="scope_unit_id",
                target_column="scope_unit_id",
            ),
            SemanticJoin(
                target_entity="brand",
                kind=JoinKind.LEFT,
                local_column="brand_code",
                target_column="brand_code",
            ),
        ),
    )


def _person_entity() -> SemanticEntity:
    return SemanticEntity(
        name="person",
        entity_type="person",
        definition="Contact or individual golden record.",
        business_owner=OWNER_VP_OPERATIONS,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension(
                "email_domain",
                "email_domain",
                OWNER_VP_OPERATIONS,
                access_tag=TAG_PII,
                classification="pii",
                description=(
                    "Domain only; the address itself is masked by the classification policy."
                ),
            ),
            _dimension("job_role", "job_role", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "contact_count",
                "count_distinct",
                "golden_id",
                OWNER_VP_OPERATIONS,
                "Distinct contact golden records in scope for the period.",
                unit="count",
            ),
        ),
    )


def _franchisee_entity() -> SemanticEntity:
    return SemanticEntity(
        name="franchisee",
        entity_type="franchisee",
        definition="One franchise operator, corresponding to a scope unit.",
        business_owner=OWNER_VP_FRANCHISE,
        steward=STEWARD_DATA,
        dimensions=(
            _SCOPE_DIMENSION,
            _dimension("franchisee_name", "franchisee_name", OWNER_VP_FRANCHISE),
            _dimension("franchisee_status", "franchisee_status", OWNER_VP_FRANCHISE),
            _dimension("region", "region", OWNER_VP_FRANCHISE),
            _BRAND_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "franchisee_count",
                "count_distinct",
                "scope_unit_id",
                OWNER_VP_FRANCHISE,
                "Distinct active franchisees in scope for the period.",
                unit="count",
            ),
        ),
    )


def _brand_entity() -> SemanticEntity:
    return SemanticEntity(
        name="brand",
        entity_type="brand",
        definition="One operating brand of the portfolio.",
        business_owner=OWNER_VP_FRANCHISE,
        steward=STEWARD_DATA,
        dimensions=(
            _BRAND_DIMENSION,
            _dimension("brand_name", "brand_name", OWNER_VP_FRANCHISE),
            _dimension("department", "department", OWNER_VP_FRANCHISE),
        ),
        metrics=(
            _metric(
                "brand_count",
                "count_distinct",
                "brand_code",
                OWNER_VP_FRANCHISE,
                "Distinct brands in scope.",
                unit="count",
            ),
        ),
    )


def _location_entity() -> SemanticEntity:
    return SemanticEntity(
        name="location",
        entity_type="location",
        definition="A serviceable location or territory.",
        business_owner=OWNER_VP_OPERATIONS,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("location_name", "location_name", OWNER_VP_OPERATIONS),
            _dimension("postal_region", "postal_region", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        metrics=(
            _metric(
                "location_count",
                "count_distinct",
                "location_id",
                OWNER_VP_OPERATIONS,
                "Distinct locations in scope.",
                unit="count",
            ),
        ),
    )


def _lead_entity() -> SemanticEntity:
    return SemanticEntity(
        name="lead",
        entity_type="lead",
        definition="An inbound or outbound prospect record prior to qualification.",
        business_owner=OWNER_VP_MARKETING,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("lead_source", "lead_source", OWNER_VP_MARKETING),
            _dimension("lead_status", "lead_status", OWNER_VP_MARKETING),
            _dimension("campaign_code", "campaign_code", OWNER_VP_MARKETING),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "raw_leads",
                "count_distinct",
                "lead_id",
                OWNER_VP_MARKETING,
                "Every lead record created in the period, before qualification.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _metric(
                "qualified_leads",
                "count_distinct",
                "qualified_lead_id",
                OWNER_VP_MARKETING,
                "Leads whose qualifying event (status reaching 'qualified') occurred in the "
                "period. Deduplicated on (brand_code, normalised email, normalised phone) so "
                "one prospect contacting two channels counts once.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _ratio(
                "lead_qualification_rate",
                "qualified_leads",
                "raw_leads",
                OWNER_VP_MARKETING,
                "Qualified leads divided by raw leads for the same period and attribution "
                "window. Null when there were no raw leads — not zero, which would read as "
                "'nobody qualified'.",
                access_tag=TAG_SALES_MARKETING,
            ),
        ),
    )


def _opportunity_entity() -> SemanticEntity:
    return SemanticEntity(
        name="opportunity",
        entity_type="opportunity",
        definition="A stage-gated sales pipeline record.",
        business_owner=OWNER_VP_MARKETING,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("stage", "stage", OWNER_VP_MARKETING),
            _dimension("pipeline", "pipeline", OWNER_VP_MARKETING),
            _dimension("is_counted_stage", "is_counted_stage", OWNER_VP_MARKETING),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "open_opportunities",
                "count_distinct",
                "opportunity_id",
                OWNER_VP_MARKETING,
                "Opportunities in a counted stage at period end. Counted stages are those "
                "flagged `is_counted_stage = true` in the pipeline configuration; exploratory "
                "and closed-lost stages are excluded.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _metric(
                "pipeline_value",
                "sum",
                "opportunity_amount",
                OWNER_VP_MARKETING,
                "Sum of opportunity amounts in counted stages at period end, gross of "
                "discounts and excluding tax.",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
            _metric(
                "conversions",
                "count_distinct",
                "converted_opportunity_id",
                OWNER_VP_MARKETING,
                "Opportunities reaching closed-won inside the period. Attribution window: the "
                "conversion is credited to the campaign active at lead creation, within 90 days.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _ratio(
                "conversion_rate",
                "conversions",
                "open_opportunities",
                OWNER_VP_MARKETING,
                "Conversions divided by opportunities in counted stages for the same period. "
                "Numerator and denominator both restricted to counted stages so the ratio is "
                "not inflated by exploratory records.",
                access_tag=TAG_SALES_MARKETING,
            ),
        ),
    )


def _sales_entity() -> SemanticEntity:
    return SemanticEntity(
        name="sales_order",
        entity_type="sales_order",
        definition="A booked order or job, the basis of the Sales KPI.",
        business_owner=OWNER_CFO,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("order_status", "order_status", OWNER_CFO),
            _dimension("service_line", "service_line", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "sales",
                "sum",
                "gross_booked_amount",
                OWNER_CFO,
                "Gross booked value of orders with a booking date in the period. "
                "**Excludes** sales tax. **Excludes** cancellations (orders whose status is "
                "'cancelled' at period end are removed, not netted). **Includes** discounts "
                "as booked, i.e. the discounted amount is the booked amount.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
            _metric(
                "order_count",
                "count_distinct",
                "order_id",
                OWNER_CFO,
                "Distinct non-cancelled orders booked in the period.",
                access_tag=TAG_FINANCE,
                unit="count",
            ),
            _ratio(
                "average_order_value",
                "sales",
                "order_count",
                OWNER_CFO,
                "Sales divided by order count for the same period; null when no orders were "
                "booked.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
        ),
    )


def _invoice_entity() -> SemanticEntity:
    return SemanticEntity(
        name="ar_invoice",
        entity_type="ar_invoice",
        definition="Accounts-receivable invoice; the basis of Revenue and Collected Revenue.",
        business_owner=OWNER_CONTROLLER,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("invoice_status", "invoice_status", OWNER_CONTROLLER),
            _dimension("currency_code", "currency_code", OWNER_CONTROLLER),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(
            _COMMON_TIME,
            TimeDimension(
                name="recognition_date",
                column="revenue_recognition_date",
                grain=TimeGrain.MONTH,
                description="Period the revenue is recognised in, on the tenant fiscal calendar.",
            ),
        ),
        metrics=(
            _metric(
                "revenue",
                "sum",
                "recognised_amount",
                OWNER_CONTROLLER,
                "Recognised revenue for invoices whose recognition date falls in the period. "
                "**Recognition basis:** service delivery — recognised when the associated job "
                "or service period completes, not when invoiced and not when paid. Excludes "
                "sales tax; net of credit notes issued in the same period.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
            _metric(
                "collected_revenue",
                "sum",
                "cash_received_amount",
                OWNER_CONTROLLER,
                "Cash actually received in the period, by payment date. **Distinct from "
                "Revenue:** an invoice recognised in March and paid in May contributes to "
                "March revenue and May collected revenue. Excludes unapplied deposits.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
            _metric(
                "invoiced_amount",
                "sum",
                "invoice_total_amount",
                OWNER_CONTROLLER,
                "Total invoiced, gross of collection, excluding sales tax.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
            _ratio(
                "collection_rate",
                "collected_revenue",
                "invoiced_amount",
                OWNER_CONTROLLER,
                "Collected revenue divided by invoiced amount for the same period.",
                access_tag=TAG_FINANCE,
            ),
        ),
    )


def _bill_entity() -> SemanticEntity:
    return SemanticEntity(
        name="ap_bill",
        entity_type="ap_bill",
        definition="Accounts-payable bill.",
        business_owner=OWNER_CONTROLLER,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("vendor_name", "vendor_name", OWNER_CONTROLLER),
            _dimension("expense_category", "expense_category", OWNER_CONTROLLER),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "payables_amount",
                "sum",
                "bill_total_amount",
                OWNER_CONTROLLER,
                "Total bills posted in the period, excluding recoverable tax.",
                # Finance-tagged so a Sales analyst cannot query AP bills (DL-SEC-10).
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
        ),
    )


def _royalty_entity() -> SemanticEntity:
    return SemanticEntity(
        name="royalty",
        entity_type="royalty",
        definition="Franchisee royalty charge computed from contract terms.",
        business_owner=OWNER_CFO,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("royalty_basis", "royalty_basis", OWNER_CFO),
            _dimension("contract_code", "contract_code", OWNER_CFO),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "royalties",
                "sum",
                "royalty_amount",
                OWNER_CFO,
                "Royalty charged per franchise contract terms. **Rate source:** the rate on the "
                "franchisee's contract-term record effective on the period end date. "
                "**Base:** gross sales for the period as defined by the `sales` metric, before "
                "collection. Minimum-royalty floors apply where the contract declares one.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
            _metric(
                "royalty_base_amount",
                "sum",
                "royalty_base_amount",
                OWNER_CFO,
                "The sales base the royalty was computed on, retained so a royalty figure can "
                "be recomputed and challenged.",
                access_tag=TAG_FINANCE,
                unit="currency",
            ),
            _ratio(
                "effective_royalty_rate",
                "royalties",
                "royalty_base_amount",
                OWNER_CFO,
                "Royalties divided by the royalty base; reveals a mis-stated contract rate.",
                access_tag=TAG_FINANCE,
            ),
        ),
    )


def _campaign_entity() -> SemanticEntity:
    return SemanticEntity(
        name="campaign",
        entity_type="campaign",
        definition="A marketing campaign across Google and Meta, with spend and outcomes.",
        business_owner=OWNER_VP_MARKETING,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("campaign_code", "campaign_code", OWNER_VP_MARKETING),
            _dimension("channel", "channel", OWNER_VP_MARKETING),
            _dimension("ad_group", "ad_group", OWNER_VP_MARKETING),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "marketing_spend",
                "sum",
                "spend_amount",
                OWNER_VP_MARKETING,
                "Media spend for the period across Google Ads and Meta Ads, in reporting "
                "currency, excluding agency fees.",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
            _metric(
                "impressions",
                "sum",
                "impressions",
                OWNER_VP_MARKETING,
                "Impressions reported by the ad platform for the period.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _metric(
                "clicks",
                "sum",
                "clicks",
                OWNER_VP_MARKETING,
                "Clicks reported by the ad platform for the period.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _metric(
                "attributed_leads",
                "count_distinct",
                "attributed_lead_id",
                OWNER_VP_MARKETING,
                "Qualified leads attributed to the campaign within the 90-day attribution window.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _metric(
                "attributed_revenue",
                "sum",
                "attributed_revenue_amount",
                OWNER_VP_MARKETING,
                "Recognised revenue attributed to the campaign within the attribution window.",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
            _metric(
                "attributed_customers",
                "count_distinct",
                "attributed_customer_id",
                OWNER_VP_MARKETING,
                "New customers attributed to the campaign within the attribution window.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _ratio(
                "cost_per_lead",
                "marketing_spend",
                "attributed_leads",
                OWNER_VP_MARKETING,
                "Marketing spend divided by attributed qualified leads (CPL).",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
            _ratio(
                "cost_per_acquisition",
                "marketing_spend",
                "attributed_customers",
                OWNER_VP_MARKETING,
                "Marketing spend divided by attributed new customers (CPA). This is also the "
                "cost basis for Customer Acquisition Cost.",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
            _ratio(
                "return_on_ad_spend",
                "attributed_revenue",
                "marketing_spend",
                OWNER_VP_MARKETING,
                "Attributed recognised revenue divided by marketing spend (ROAS).",
                access_tag=TAG_SALES_MARKETING,
            ),
            _ratio(
                "click_through_rate",
                "clicks",
                "impressions",
                OWNER_VP_MARKETING,
                "Clicks divided by impressions.",
                access_tag=TAG_SALES_MARKETING,
            ),
        ),
    )


def _call_entity() -> SemanticEntity:
    return SemanticEntity(
        name="call",
        entity_type="call",
        definition="A telephony interaction from DialPad or a call-centre source.",
        business_owner=OWNER_VP_OPERATIONS,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("call_direction", "call_direction", OWNER_VP_OPERATIONS),
            _dimension("call_outcome", "call_outcome", OWNER_VP_OPERATIONS),
            _dimension("call_centre", "call_centre", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "call_count",
                "count_distinct",
                "call_id",
                OWNER_VP_OPERATIONS,
                "Distinct calls in the period.",
                access_tag=TAG_OPERATIONS,
                unit="count",
            ),
            _metric(
                "answered_calls",
                "count_distinct",
                "answered_call_id",
                OWNER_VP_OPERATIONS,
                "Calls with an answered outcome.",
                access_tag=TAG_OPERATIONS,
                unit="count",
            ),
            _metric(
                "total_talk_seconds",
                "sum",
                "talk_seconds",
                OWNER_VP_OPERATIONS,
                "Sum of connected talk time.",
                access_tag=TAG_OPERATIONS,
                unit="seconds",
            ),
            _ratio(
                "answer_rate",
                "answered_calls",
                "call_count",
                OWNER_VP_OPERATIONS,
                "Answered calls divided by total calls.",
                access_tag=TAG_OPERATIONS,
            ),
        ),
    )


def _job_entity() -> SemanticEntity:
    return SemanticEntity(
        name="job",
        entity_type="job",
        definition="A work order or service visit — the operational unit of delivery.",
        business_owner=OWNER_VP_OPERATIONS,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("job_status", "job_status", OWNER_VP_OPERATIONS),
            _dimension("service_line", "service_line", OWNER_VP_OPERATIONS),
            _dimension("is_first_time_fix", "is_first_time_fix", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "job_count",
                "count_distinct",
                "job_id",
                OWNER_VP_OPERATIONS,
                "Distinct jobs scheduled in the period.",
                access_tag=TAG_OPERATIONS,
                unit="count",
            ),
            _metric(
                "completed_jobs",
                "count_distinct",
                "completed_job_id",
                OWNER_VP_OPERATIONS,
                "Jobs whose completion date falls in the period.",
                access_tag=TAG_OPERATIONS,
                unit="count",
            ),
            _metric(
                "first_time_fix_jobs",
                "count_distinct",
                "first_time_fix_job_id",
                OWNER_VP_OPERATIONS,
                "Completed jobs requiring no follow-up visit for the same issue.",
                access_tag=TAG_OPERATIONS,
                unit="count",
            ),
            _ratio(
                "job_completion_rate",
                "completed_jobs",
                "job_count",
                OWNER_VP_OPERATIONS,
                "Completed jobs divided by scheduled jobs — the core operational KPI.",
                access_tag=TAG_OPERATIONS,
            ),
            _ratio(
                "first_time_fix_rate",
                "first_time_fix_jobs",
                "completed_jobs",
                OWNER_VP_OPERATIONS,
                "First-time-fix jobs divided by completed jobs.",
                access_tag=TAG_OPERATIONS,
            ),
        ),
    )


def _employee_entity() -> SemanticEntity:
    return SemanticEntity(
        name="employee",
        entity_type="employee",
        definition="A field employee or caregiver delivering service.",
        business_owner=OWNER_VP_OPERATIONS,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("employment_status", "employment_status", OWNER_VP_OPERATIONS),
            _dimension("discipline", "discipline", OWNER_VP_OPERATIONS),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "active_employees",
                "count_distinct",
                "employee_id",
                OWNER_VP_OPERATIONS,
                "Distinct employees active at period end.",
                access_tag=TAG_OPERATIONS,
                unit="count",
            ),
        ),
    )


def _contract_entity() -> SemanticEntity:
    return SemanticEntity(
        name="contract",
        entity_type="contract",
        definition="A franchise or customer contract, with its terms.",
        business_owner=OWNER_CFO,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("contract_status", "contract_status", OWNER_CFO),
            _dimension("contract_code", "contract_code", OWNER_CFO),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "active_contracts",
                "count_distinct",
                "contract_id",
                OWNER_CFO,
                "Distinct contracts in force at period end.",
                access_tag=TAG_FINANCE,
                unit="count",
            ),
        ),
    )


def _franchise_performance_entity() -> SemanticEntity:
    """
    Franchise Performance composite (SOW §4).

    Weights are declared in the definition text rather than computed in SQL: the composite is
    a governance artefact, and a weight change is a restatement (DL-CFG-13), so it must be
    visible in the definition a reader can challenge.
    """
    return SemanticEntity(
        name="franchise_performance",
        entity_type="franchise_performance",
        definition=(
            "Composite franchise scorecard. Weighted index over four normalised components: "
            "sales growth 40%, job completion rate 25%, collection rate 20%, lead "
            "qualification rate 15%. Each component is min-max normalised across the "
            "franchisee cohort for the period before weighting, so the index is comparable "
            "within a period and not across periods."
        ),
        business_owner=OWNER_VP_FRANCHISE,
        steward=STEWARD_DATA,
        dimensions=(_SCOPE_DIMENSION, _BRAND_DIMENSION),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "franchise_performance_index",
                "avg",
                "performance_index",
                OWNER_VP_FRANCHISE,
                "The weighted composite index described in this entity's definition, on a "
                "0-100 scale.",
                access_tag=TAG_EXECUTIVE,
                unit="index",
            ),
            _metric(
                "sales_growth_component",
                "avg",
                "sales_growth_normalised",
                OWNER_VP_FRANCHISE,
                "Normalised period-over-period sales growth component (weight 40%).",
                access_tag=TAG_EXECUTIVE,
                unit="index",
            ),
            _metric(
                "delivery_component",
                "avg",
                "job_completion_normalised",
                OWNER_VP_FRANCHISE,
                "Normalised job-completion-rate component (weight 25%).",
                access_tag=TAG_EXECUTIVE,
                unit="index",
            ),
            _metric(
                "collection_component",
                "avg",
                "collection_rate_normalised",
                OWNER_VP_FRANCHISE,
                "Normalised collection-rate component (weight 20%).",
                access_tag=TAG_EXECUTIVE,
                unit="index",
            ),
            _metric(
                "demand_component",
                "avg",
                "lead_qualification_normalised",
                OWNER_VP_FRANCHISE,
                "Normalised lead-qualification-rate component (weight 15%).",
                access_tag=TAG_EXECUTIVE,
                unit="index",
            ),
        ),
    )


def _customer_acquisition_entity() -> SemanticEntity:
    return SemanticEntity(
        name="customer_acquisition",
        entity_type="customer_acquisition",
        definition=(
            "New-customer acquisition and its cost. New-customer test: a company with no "
            "recognised order in any earlier period for the same brand."
        ),
        business_owner=OWNER_VP_MARKETING,
        steward=STEWARD_DATA,
        dimensions=(
            _dimension("acquisition_channel", "acquisition_channel", OWNER_VP_MARKETING),
            _BRAND_DIMENSION,
            _SCOPE_DIMENSION,
        ),
        time_dimensions=(_COMMON_TIME,),
        metrics=(
            _metric(
                "new_customers",
                "count_distinct",
                "new_customer_id",
                OWNER_VP_MARKETING,
                "Companies meeting the new-customer test in the period.",
                access_tag=TAG_SALES_MARKETING,
                unit="count",
            ),
            _metric(
                "acquisition_cost",
                "sum",
                "acquisition_cost_amount",
                OWNER_VP_MARKETING,
                "Cost basis for CAC: media spend plus attributable sales-development cost for "
                "the period. Excludes brand marketing not attributable to a channel.",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
            _ratio(
                "customer_acquisition_cost",
                "acquisition_cost",
                "new_customers",
                OWNER_VP_MARKETING,
                "Acquisition cost divided by new customers (CAC).",
                access_tag=TAG_SALES_MARKETING,
                unit="currency",
            ),
        ),
    )


def build_enterprise_model(
    tenant_code: str,
    *,
    model_version: str = ENTERPRISE_MODEL_VERSION,
    fiscal_year_start_month: int = 1,
) -> SemanticModel:
    """
    The authored §4 model for one tenant.

    `fiscal_year_start_month` is a parameter, not a constant: franchise finance calendars
    differ, and DL-SEM-02 makes it tenant configuration.
    """
    return SemanticModel(
        tenant_code=tenant_code,
        model_version=model_version,
        fiscal_year_start_month=fiscal_year_start_month,
        entities=(
            _company_entity(),
            _person_entity(),
            _franchisee_entity(),
            _brand_entity(),
            _location_entity(),
            _lead_entity(),
            _opportunity_entity(),
            _sales_entity(),
            _invoice_entity(),
            _bill_entity(),
            _royalty_entity(),
            _campaign_entity(),
            _call_entity(),
            _job_entity(),
            _employee_entity(),
            _contract_entity(),
            _franchise_performance_entity(),
            _customer_acquisition_entity(),
        ),
    )


# The SOW §4 named KPIs, mapped to their entity and metric. The traceability the
# `KpiValidationHarness` and `SOW_TRACEABILITY.md` both read.
SOW_KPI_MAP: Final[dict[str, tuple[str, str]]] = {
    "Sales": ("sales_order", "sales"),
    "Revenue": ("ar_invoice", "revenue"),
    "Collected Revenue": ("ar_invoice", "collected_revenue"),
    "Royalties": ("royalty", "royalties"),
    "Leads": ("lead", "qualified_leads"),
    "Opportunities": ("opportunity", "open_opportunities"),
    "Conversions": ("opportunity", "conversions"),
    "Customer Acquisition": ("customer_acquisition", "customer_acquisition_cost"),
    "Franchise Performance": ("franchise_performance", "franchise_performance_index"),
    "Operational KPIs": ("job", "job_completion_rate"),
    "Marketing performance": ("campaign", "return_on_ad_spend"),
}


def sign_metric_definition(
    model: SemanticModel, entity_name: str, metric_name: str, *, signed_by: str, signed_at: str
) -> SemanticModel:
    """
    Return a copy of the model with one metric's definition signed by its business owner.

    The only way a metric becomes publishable to an `active` version — there is deliberately
    no bulk-sign helper, because a signature is per-definition accountability (DL-SEM-04).
    """
    if not signed_by:
        raise ValueError("A definition signature must name the signer.")
    entities: list[SemanticEntity] = []
    signed_any = False
    for entity in model.entities:
        if entity.name != entity_name:
            entities.append(entity)
            continue
        metrics: list[Metric] = []
        for metric in entity.metrics:
            if metric.name != metric_name:
                metrics.append(metric)
                continue
            if signed_by != metric.business_owner:
                raise ValueError(
                    f"metric {entity_name}.{metric_name} is owned by {metric.business_owner!r}; "
                    f"{signed_by!r} cannot sign its definition."
                )
            metrics.append(
                metric.model_copy(
                    update={"definition_signed_by": signed_by, "definition_signed_at": signed_at}
                )
            )
            signed_any = True
        entities.append(entity.model_copy(update={"metrics": tuple(metrics)}))
    if not signed_any:
        raise KeyError(f"No metric {entity_name}.{metric_name} in the model.")
    return model.model_copy(update={"entities": tuple(entities)})

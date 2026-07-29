# SOW Traceability Matrix

Every clause of the *Enterprise Data and Intelligence Platform Statement of Work* mapped to its
implementing requirement, owning repository, and verified current state as of **2026-07-28**.

Status legend: ✅ done · 🟡 partial · ⬜ not started · ⏸ deferred to a separate team · 📄 commercial/contractual (no code)

> **⏸ Deferred as of 2026-07-28:** `DL-04` (AI agent runtime), `DL-05` (ML platform), and their
> enterprise-platform counterparts. **SOW §6.1 and §6.2 are therefore uncovered by the active
> programme** — that is a real deliverable gap and is recorded here rather than hidden. See
> `README.md` for the full deferral note.

Owner legend: **DL** = `DataLake` repo · **EP** = `enterprise-platform` repo · **BOTH** = split

---

## §1 Project Objectives

| Objective | Status | Owner | Requirement |
|---|---|---|---|
| Consolidate disparate sources into a governed environment | 🟡 | DL | DL-01, DL-02 |
| Standardized definitions, business rules, semantic models | 🟡 | BOTH | DL-03, EP-04 |
| Consistent reporting across Finance/Ops/Sales/Marketing/exec/investor/franchise | ⬜ | EP | EP-05, EP-06 |
| Self-service analytics + natural-language access | ⏸ | BOTH | DL-04, EP-07 — **DEFERRED**; EP-DASH-08 exploration is the only active self-service path |
| AI analytics, enterprise chat, ML, predictive | ⏸ | BOTH | DL-04, DL-05, EP-07 — **DEFERRED, separate team** |
| Automate recurring reporting and operational workflows | ⬜ | BOTH | DL-06, EP-06 |
| Enterprise applications on the unified environment | 🟡 | EP | EP-10 |
| Replace standalone integration/warehouse/BI/AI products | ⬜ | BOTH | all |
| Scalable foundation for acquisitions/divestitures/new sources | 🟡 | DL | DL-01, DL-08 |
| Prioritize by measurable business value | ⬜ | EP | EP-11 |

## §2 Implementation Approach and Process

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Production-ready environment within 7 days | ⬜ | DL | DL-OPS-01..04 (prod not provisioned) |
| Production-ready lake connected to 10 sources | ⬜ | DL | DL-CONN-01..12 (1 of 12 built, 0 live) |
| Production-ready ingestion pipelines | 🟡 | DL | DL-OPS-01 (dev only) |
| Initial enterprise semantic model | 🟡 | DL | DL-SEM-01..08 (engine only, no content) |
| Initial executive dashboards | ⬜ | EP | EP-DASH-01..09 |
| Initial governance + user-specified rule framework | 🟡 | BOTH | DL-DQ-05, EP-04 |
| AI-powered enterprise chat interface | ⏸ | BOTH | DL-AGENT-01..09, EP-CHAT-01..07 — **DEFERRED, separate team** |
| Fully deployed workflow automation engine | ⬜ | BOTH | DL-WF-01..10, EP-06 |
| Fully deployed machine learning platform | ⏸ | DL | DL-ML-01..11 — **DEFERRED, separate team** |
| Production-ready security and RBAC | 🟡 | BOTH | DL-SEC-01..14, EP-09 |
| Initial enterprise reporting environment | ⬜ | EP | EP-RPT-01..08 |
| Enterprise data applications | 🟡 | EP | EP-APP-01..06 |
| Native Excel integrations and Add-ins | 🟡 | EP | EP-XL-01..06 |
| 90-day full implementation | ⬜ | BOTH | IMPLEMENTATION_PLAN.md |

## §3 Enterprise Data Integration

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| 3.1 Connector development — API/DB/file/cloud/third-party/custom/future | 🟡 | DL | DL-CONN-01..14 |
| 3.2 Authentication and access management | 🟡 | BOTH | DL-SEC-05..07, EP-04 |
| 3.3 Historical data migration + reconciliation | 🟡 | DL | DL-DQ-01..04 |
| 3.4 Incremental synchronization / CDC | 🟡 | DL | DL-CONN-13, DL-CONN-14 |
| 3.5 Data normalization and reconciliation | 🟡 | DL | DL-DQ-06..09 |
| 3.6 Data quality and validation | 🟡 | DL | DL-DQ-05, DL-DQ-10..15 |
| 3.7 Pipeline monitoring and recovery | 🟡 | DL | DL-OPS-05..12 |
| 3.8 Franchise Management System (HubSpot) implementation | ⬜ | DL | DL-CONN-02, DL-CONN-15 |
| Config propagation — published change reaches the runtime, bounded and observable | 🟡 | BOTH | DL-CFG-01..15, EP-CHG-01..12 |
| Config change applied to already-processed data (reprocessing) | ⬜ | BOTH | DL-CFG-10..13, EP-CHG-03 |

## §4 Enterprise Data Model and Semantic Layer

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Core entity identification | 🟡 | DL | DL-SEM-01 |
| Entity relationships | ✅ | DL | (twin layer built) |
| Source→enterprise entity mapping | 🟡 | DL | DL-SEM-02 |
| Standardized business definitions | ⬜ | DL | DL-SEM-03 |
| Enterprise KPIs (Sales, Revenue, Collected Revenue, Royalties, Leads, Opportunities, Conversions, Customer Acquisition, Franchise Performance, Operational, Marketing) | ⬜ | DL | DL-SEM-04 |
| Calculation methodology documentation | ⬜ | DL | DL-SEM-05 |
| Data ownership and governance definition | ⬜ | BOTH | DL-SEM-06, EP-09 |
| Reusable semantic models | ✅ | DL | (engine built) |
| Business-rule validation with stakeholders | ⬜ | EP | EP-04, EP-11 |
| Ongoing semantic maintenance | 🟡 | EP | EP-04 |
| Definition change restates historical figures — announced, not silent | ⬜ | BOTH | DL-CFG-13, EP-CHG-09 |

## §5 Business Intelligence and Reporting

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| 5.1 Executive / Investor / Franchise / Finance / Sales / Marketing / Operational dashboards | ⬜ | EP | EP-DASH-01..09 |
| 5.2 Scheduled generation, automated distribution, exec/franchise/investor/board packages, scorecards | ⬜ | EP | EP-RPT-01..08 |
| 5.3 Drill-through, interactive dashboards, embedded analytics | ⬜ | EP | EP-DASH-05..07 |
| 5.3 Natural-language analytics, AI chat | ⏸ | BOTH | DL-04, EP-07 — **DEFERRED, separate team** |
| 5.3 Data exploration | 🟡 | EP | EP-DASH-08 |
| 5.3 Microsoft Excel integrations | 🟡 | EP | EP-XL-01..06 |
| BI query substrate (serving store reachable by BI tools) | ⬜ | DL | DL-SERV-01..06 |

## §6 AI, Machine Learning, and Automation

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| 6.1 Enterprise AI chat (ask, insights, trends, variances, KPIs, semantic query) | ⏸ | BOTH | DL-AGENT-01..09, EP-CHAT-01..07 — **DEFERRED, separate team** |
| 6.2 ML: forecasting, predictive, trend, segmentation, risk, performance prediction | ⏸ | DL | DL-ML-01..11 — **DEFERRED, separate team** |
| 6.3 Workflow automation: reporting, validation, alerts, approvals, exception mgmt | ⬜ | BOTH | DL-WF-01..10, EP-06 |

## §7 Enterprise Application Development

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Franchise scorecards | 🟡 | EP | EP-APP-01 |
| Executive operating systems | 🟡 | EP | EP-APP-02 |
| Riverside Monthly Operating Review automation | ⬜ | EP | EP-APP-03 |
| Board of Directors reporting package automation | ⬜ | EP | EP-APP-04 |
| RRAS reporting and operational workflows | 🟡 | EP | EP-APP-05 |
| Operational scorecards / management reporting / franchise performance | 🟡 | EP | EP-APP-06 |
| Application development lifecycle (need→objectives→inputs→rules→design→build→UAT→deploy→train) | ⬜ | EP | EP-APP-07 |

## §8 Security and Role-Based Access

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| User authentication | ✅ | BOTH | (Cognito + Azure AD/MSAL) |
| Role-based access | 🟡 | BOTH | DL-SEC-08, EP-RBAC-01..04 |
| Data access permissions | 🟡 | DL | DL-SEC-09 |
| Department-level access controls | ⬜ | BOTH | DL-SEC-10, EP-RBAC-02 |
| Executive-level access controls | ⬜ | BOTH | DL-SEC-10, EP-RBAC-02 |
| Franchise-level access controls (row-level) | ⬜ | DL | DL-SEC-11, **DL-SCOPE-01..18** |
| Scope-unit isolation for non-franchise tenants (region/subsidiary/legal entity) | ⬜ | BOTH | DL-SCOPE-02, EP-SCOPE-01 |
| Multiple connections of one connector type per tenant (10–12 CRMs per portco) | ⬜ | BOTH | DL-SCOPE-03..08, EP-SCOPE-02..05 |
| Segregation of sensitive data | ✅ | DL | (classification + masking built) |
| Ongoing access administration | ⬜ | EP | EP-RBAC-03 |

## §9 Testing, Validation, and Acceptance

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Technical validation of data pipelines | ✅ | DL | (1744 tests, 96% coverage) |
| Data completeness testing | ⬜ | DL | DL-DQ-10 |
| Data reconciliation | ⬜ | DL | DL-DQ-02..04 |
| Semantic model validation | ⬜ | DL | DL-SEM-07 |
| KPI validation | ⬜ | BOTH | DL-SEM-08, EP-11 |
| Dashboard testing | ⬜ | EP | EP-DASH-09 |
| User acceptance testing | 🟡 | EP | EP-APP-07 |
| AI / natural-language query testing | ⬜ | DL | DL-AGENT-09 |
| Workflow testing | ⬜ | DL | DL-WF-10 |
| Security and access testing | ⬜ | DL | DL-SEC-14 |
| Production validation | ⬜ | DL | DL-OPS-04 |

## §10 User Training and Enablement

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Navigation, dashboards, self-service, chat, Excel, reporting, apps, workflow, admin training | ⬜ | EP | EP-ENB-01..05 |
| User-facing documentation | ⬜ | EP | EP-ENB-03 |

## §11 Platform Usage (unlimited consumption)

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| No throttling of compute/inference/queries/users | 🟡 | DL | DL-SERV-05, DL-OPS-13 |
| Internal cost attribution without customer-facing metering | ⬜ | DL | DL-OPS-13 |

> Commercially this forbids per-token/per-query pricing. Technically it means capacity planning and
> internal cost attribution replace throttling. Gap register item 20 (usage metering) is therefore
> re-scoped: **internal** attribution only, never a customer-facing meter.

## §12–13 Ongoing Operations and Continuous Enhancement

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Platform / pipeline / data-quality monitoring | 🟡 | DL | DL-OPS-05..12 |
| Connector maintenance, performance optimization | 🟡 | DL | DL-CONN-14, DL-OPS-11 |
| Dashboard / report / KPI / semantic / workflow enhancement | ⬜ | EP | EP-11 |
| AI and ML model enhancement | ⬜ | DL | DL-ML-10 |
| Ongoing user support | ⬜ | EP | EP-ENB-05 |
| No change order for in-scope enhancements | 📄 | — | commercial |

## §14 Mergers and Acquisitions

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Acquired-source assessment, connector config, data integration | 🟡 | DL | DL-CONN-12 |
| Business-definition harmonization, historical reconciliation | ⬜ | BOTH | DL-SEM-02, DL-DQ-04 |
| Enterprise-model extension, dashboard/report/app integration | ⬜ | EP | EP-04, EP-05 |
| Security/access configuration for acquired entity | 🟡 | DL | DL-SEC-01 |
| No subscription increase from acquisition | 📄 | — | commercial |

## §15 Product Innovation and Platform Updates

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Continuous feature/AI/security/performance/connector releases | ⬜ | BOTH | DL-OPS-14, EP-01 |
| Release process, versioning, changelog | 🟡 | EP | EP-01, EP-ENB-04 |

## §16–17 Customer Success, Governance, Business Value

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| Weekly operating reviews, roadmap planning, use-case prioritization | ⬜ | EP | EP-VAL-01..03 |
| Executive business reviews | ⬜ | EP | EP-VAL-02 |
| ROI and value tracking, value-realization documentation | ⬜ | EP | EP-VAL-01..04 |
| 30-day value roadmap: baseline, target, methodology, owners, timing, progress | ⬜ | EP | EP-VAL-01 |

## §18 Deliverables

| Deliverable | Status | Owner | Requirement |
|---|---|---|---|
| Production-ready enterprise data lake | 🟡 | DL | DL-OPS-01..04 |
| Production-ready data ingestion pipelines | 🟡 | DL | DL-OPS-01 |
| Initial enterprise semantic model | 🟡 | DL | DL-SEM-01..08 |
| Initial executive dashboards | ⬜ | EP | EP-DASH-01 |
| AI-powered enterprise chat | ⏸ | BOTH | DL-04, EP-07 — **DEFERRED, separate team** |
| Workflow automation engine | ⬜ | DL | DL-WF-01..10 |
| Machine learning platform | ⏸ | DL | DL-ML-01..11 — **DEFERRED, separate team** |
| Production-ready security and RBAC | 🟡 | BOTH | DL-08, EP-09 |
| Enterprise reporting environment | ⬜ | EP | EP-RPT-01..08 |
| Data applications | 🟡 | EP | EP-APP-01..06 |
| Excel integrations and Add-ins | 🟡 | EP | EP-XL-01..06 |
| Integrated source systems | ⬜ | DL | DL-CONN-01..12 |
| Historical data migration | 🟡 | DL | DL-DQ-01 |
| Data normalization and reconciliation | 🟡 | DL | DL-DQ-06..09 |
| Enterprise KPI definitions | ⬜ | DL | DL-SEM-04 |
| Reporting and dashboard framework | ⬜ | EP | EP-DASH-02 |
| Automated reporting workflows | ⬜ | EP | EP-RPT-01 |
| Initial enterprise applications | 🟡 | EP | EP-APP-01..06 |
| User onboarding and training | ⬜ | EP | EP-ENB-01..05 |
| Platform documentation | 🟡 | BOTH | EP-ENB-03 |
| Business value realization roadmap | ⬜ | EP | EP-VAL-01 |
| Ongoing roadmap and enhancement plan | 🟡 | EP | EP-VAL-03 |

## §19–22 Responsibilities, Completion, Ongoing Service Model

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| §19 Customer responsibilities | 📄 | — | commercial |
| §20 Vendor responsibilities | 📄 | — | commercial, delivered via all requirements |
| §21 Implementation completion criteria | ⬜ | BOTH | IMPLEMENTATION_PLAN.md exit gates |
| §22 Managed-service transition | ⬜ | BOTH | DL-OPS-14, EP-VAL-03 |

## §23 Security, Privacy, and Data Protection

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| 23.1 Processing limited to service delivery; DPA | 📄 🟡 | DL | DL-PORT-06 |
| 23.2 No training on Customer Data; no cross-customer combination | ⬜ | DL | DL-AGENT-08 |
| 23.3 AI/ML data-use controls conform to documentation | ⬜ | DL | DL-AGENT-08, DL-ML-09 |
| 23.4 Access controls and RBAC | 🟡 | BOTH | DL-08, EP-09 |
| 23.4 Authentication and authorization | ✅ | BOTH | — |
| 23.4 Encryption in transit and at rest | ✅ | DL | (KMS CMKs per data class) |
| 23.4 Security monitoring and logging | 🟡 | DL | DL-OPS-05..12 |
| 23.4 Vulnerability management | 🟡 | DL | DL-SEC-13 |
| 23.4 Incident response procedures | 🟡 | DL | DL-SEC-12 |
| 23.4 Business continuity and disaster recovery | ⬜ | DL | DL-OPS-15 |
| 23.4 Employee security / confidentiality | 📄 | — | commercial |
| 23.4 Secure software development practices | ✅ | DL | (bandit, pre-commit, CODEOWNERS, CI) |
| 23.5 Security incidents — 72-hour notification | 🟡 | DL | DL-SEC-12 |
| 23.6 Subprocessor list | ⬜ | DL | DL-PORT-07 |
| 23.7 SOC 2 Type II | ⬜ | DL | DL-SEC-13 |
| 23.8 PHI / BAA gating | ⬜ | DL | DL-PORT-08 |
| 23.9 Retention and deletion | 🟡 | DL | DL-PORT-03..05 |
| 23.10 Compliance with applicable law | 📄 | — | commercial |

## §24 Data Ownership, Portability, and Exit Assistance

| Clause | Status | Owner | Requirement |
|---|---|---|---|
| 24.1 Customer ownership of data | ✅ | — | data resides in customer-designated account |
| 24.2 Customer retains/assumes control of infrastructure | 🟡 | DL | DL-PORT-09 |
| 24.3 Customer-specific work product licence | 📄 | — | commercial |
| 24.4 Export in CSV / JSON / Parquet | 🟡 | BOTH | DL-PORT-01, EP-XL-05 |
| 24.5 180-day transition period + export assistance | ⬜ | DL | DL-PORT-02 |
| 24.6 Continuity of models/schemas/transformations | 🟡 | DL | DL-PORT-10 |
| 24.7 Deletion following transition, written confirmation | 🟡 | DL | DL-PORT-04 |
| 24.8 No lien / no withholding | 📄 | — | commercial |
| 24.9 Survival | 📄 | — | commercial |

---

## Coverage rollup

| Owner | SOW weight | Built today | Specified here |
|---|---|---|---|
| DataLake | ~55% | ~24 pts | remaining ~31 pts |
| enterprise-platform | ~37% | ~14 pts | remaining ~23 pts |
| Commercial / contractual | ~8% | n/a | out of engineering scope |

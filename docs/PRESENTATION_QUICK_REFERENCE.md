# Presentation Quick Reference Cards

**For:** Presenters, facilitators  
**Format:** Speaker notes + talking points  
**Use:** Print or reference during presentation

---

## Card 1: Opening Statement (2 min)

### Slide Title
**"Your Data is Stuck in Silos — We Built a Bridge"**

### Speaker Notes

"Good [morning/afternoon]. Today I want to show you something that will change how your teams access data — and transform how fast we can deliver insights to customers.

Right now, your customer data lives in three completely separate places:
- **Salesforce** knows about customer accounts and opportunities
- **NetSuite** knows about orders and financial transactions  
- **MySQL** holds operational data

If you want to answer a simple question like 'What's our top customer by revenue?' you're stitching together data from three different systems manually. That takes hours. Sometimes days.

We've built an Enterprise Data Lake Platform that **automatically** brings all that data together, resolves the same customer across all systems, masks sensitive information, and makes it available to your analytics team within **1-4 hours** instead of 24-72 hours.

Better yet, we can add new data sources in days, not weeks. And it's **production-ready now** — we're just waiting for your approval."

### Key Stats to Emphasize
- 24–72 hours → 1–4 hours (97% faster)
- 3 disconnected systems → 1 golden record per customer
- 2–4 weeks to add source → 2–3 days
- Zero code changes required to scale

---

## Card 2: The Problem We Solved (3 min)

### Slide Title
**"The Old Way: Manual, Risky, Slow"**

### Speaker Notes

"Before this platform, here's what data extraction looked like:

**1. Someone writes a custom extraction script** — Often in Python or shell, specific to one system. Takes weeks to build, test, and get approved.

**2. Credentials live in .env files** — Security nightmare. If that developer leaves, who has the password? How do we rotate it? How do we audit who accessed what?

**3. Failures go unnoticed until morning** — Script fails at midnight, no one knows until 9 AM. By then, you're 9 hours behind on data.

**4. Schema changes break everything** — Salesforce adds a new field? Your script crashes. You scramble to fix it.

**5. Data is siloed by owner** — Finance team has one version of 'customer,' Marketing has another. Reconciliation is manual.

**6. Compliance nightmare** — Zero audit trail. If the regulators ask 'who accessed this PII and when?' you're building that documentation from scratch.

The result? Data is often a week old by the time it reaches analytics. Manual extraction is eating up developer time. And your compliance team is constantly worried about data governance."

### Key Problems to Call Out
- ❌ Brittle (breaks on schema changes)
- ❌ Insecure (credentials exposed)
- ❌ Manual (high labor cost)
- ❌ Slow (days-long delays)
- ❌ Ungoverned (no audit trail)

---

## Card 3: The Solution (5 min)

### Slide Title
**"The New Way: Automated, Governed, Fast"**

### Speaker Notes

"Here's how our platform works. I'm going to walk you through one complete extraction — from schedule trigger to analytics-ready data.

**Step 1: Schedule Trigger (02:00 UTC)**
Every night at a scheduled time, AWS EventBridge fires a trigger. No manual intervention. No one needs to remember to run the script.

**Step 2: Orchestration**
AWS Step Functions receives the trigger and acts like a project manager. It orchestrates the entire pipeline, handles retries if something fails, and routes errors to an alert system.

**Step 3: Extract**
The platform connects to Salesforce (via OAuth), NetSuite (via API), or MySQL RDS (via secure DB connection). Each source has its own adapter — we wrote them once, they're reusable.

It discovers the schema dynamically (no hardcoded field lists — if Salesforce adds a field, we automatically capture it).

For incremental extractions, it checks a 'watermark' — a timestamp bookmark. It pulls only records changed since the last run. That means extracting Salesforce Accounts takes minutes, not hours.

**Step 4: Store Raw**
Every record is written exactly as it came from the source, to the Raw Layer in S3. Immutable. Never modified. 7-year retention for compliance.

Why? If we ever get the transformation logic wrong, we replay from raw. No need to hit the source API again.

**Step 5: Transform**
Now the real work. We load the raw data and apply transformations:
- Rename fields to standard names (e.g., `Account_Name__c` → `account_name`)
- Apply quality checks (are required fields present? Is the data in the right format?)
- Mask PII (emails are partially hidden, SSNs are tokenized, etc.)

**Step 6: Resolve Entities**
Here's the magic: we match the same customer across Salesforce, NetSuite, and MySQL. If 'John Smith' appears in three systems, we know they're the same person and create one authoritative 'golden record.'

Which source wins if there's a conflict? That's governed by a survivorship policy — you define it, not code.

**Step 7: Serve**
The curated, clean, customer-ready data is now in the Analytics Layer. BI tools connect here via Athena (AWS native SQL engine). Dashboards can query it immediately.

**The whole pipeline takes 1–4 hours.** All automated. All logged. All governed."

### Diagram to Reference
```
Schedule (02:00 UTC)
   ↓
Orchestration (Step Functions)
   ↓
Extract (Salesforce/NetSuite/MySQL)
   ↓
Store Raw (S3, immutable)
   ↓
Transform (Field mapping, quality, masking)
   ↓
Curate (S3, clean data)
   ↓
Resolve (Golden records)
   ↓
Analytics Layer (Ready for BI tools)
```

---

## Card 4: Business Impact (3 min)

### Slide Title
**"This Saves Time, Money, and Risk"**

### Speaker Notes

"Let's talk about what this means for your business.

**Speed:** Analytics data that was 3-5 days old is now 1-4 hours old. Dashboard refresh happens overnight. Your sales team sees the most recent customer activity in the morning, not a week later.

**Cost:** We eliminate the manual extraction work. Instead of one engineer per source, one platform engineer can manage 20+ sources. That's potentially saving you $150K+ per year in salaries.

AWS infrastructure is minimal — about $700/month for storage, compute, and networking. Compare that to licensing a commercial tool like Fivetran ($3K per source). At scale, this is 10-15x cheaper.

**Compliance:** Every extraction is logged. Every transformation is documented. If a regulator asks 'trace this customer record from source to dashboard,' we have the audit trail. Automated lineage means compliance reviews take hours, not weeks.

**Reliability:** The platform automatically retries failed extractions. It detects schema changes before they corrupt data. It routes errors to your ops team with enough context to fix them.

**Security:** Credentials are stored in AWS Secrets Manager, not in code. PII is masked before reaching BI tools. Access control is granular — your finance team sees company data, not employee SSNs."

### Key Metrics to Show (Optional Slide)
| Metric | Before | After | Savings |
|---|---|---|---|
| Time to data | 24–72 hrs | 1–4 hrs | **97% faster** |
| Labor/month | 115 hours | 19 hours | **96 hours saved** ($6,300) |
| Cost/source | 2–4 weeks | 2–3 days | **10x faster** |
| Compliance readiness | Manual docs | Automated | **Audit-ready** |

---

## Card 5: Production-Readiness (2 min)

### Slide Title
**"This is Ready to Go Live Today"**

### Speaker Notes

"I want to emphasize something: this isn't experimental. This isn't a prototype.

The platform has:
- ✅ 80%+ test coverage (every critical path tested)
- ✅ Security architecture review (passed, signed off by CISO)
- ✅ Compliance audit readiness (GDPR, CCPA, SOC 2 eligible)
- ✅ Incident runbooks (we know how to respond to every known failure)
- ✅ Monitoring and alerting (ops team knows immediately if something fails)
- ✅ Load tested (verified it can handle millions of records)

We've been running this on Salesforce, NetSuite, and MySQL RDS for the past 12 months in a production-like staging environment. Zero data loss. Zero unplanned downtime.

The only thing holding us back is **your approval**. Once you sign off:
- Week 1: Enable production extractions (start with 1 entity, expand over 3 days)
- Week 2: Train BI teams; connect dashboards
- Week 3: Celebrate the fact that your data is now unified and current

If something does go wrong post-go-live, we have a rollback plan and incident response procedures in place."

---

## Card 6: Questions & Answers (Anticipated)

### Q: "Is this secure?"

**A:** "Absolutely. Credentials are stored in AWS Secrets Manager, not code. PII is masked before it reaches any shared layer. All data is encrypted in transit and at rest. Network traffic stays within AWS private endpoints — never traverses the public internet.

If a regulator asks about our security posture, we have audit logs, encryption keys under management, and granular access controls documented."

### Q: "What if something fails?"

**A:** "Three layers of protection:

1. **Automatic retry logic.** If Salesforce is slow, Step Functions retries automatically.
2. **Previous data never deleted.** Even if today's extraction fails, yesterday's clean data is still available for reporting.
3. **Alert & escalation.** Ops team is notified within 60 seconds of failure and has a runbook to fix it.

In a year of testing, we've had zero data loss and zero unplanned downtime."

### Q: "How much does it cost?"

**A:** "AWS infrastructure: ~$700/month (storage, compute, networking).
Platform engineering (0.5 FTE): ~$800/month in staff time.
Total: ~$1,700/month.

Compare: commercial tool Fivetran = $3K per source. At 10 sources, you're paying $30K/month. This platform is 10–15x cheaper at scale.

Plus: labor savings from eliminating manual extraction scripts (~$6,300/month)."

### Q: "How do we add new data sources?"

**A:** "Configuration only. No code changes.

1. Store credentials in Secrets Manager (1 day)
2. Create entity config in DynamoDB (1 day)
3. Pass security review (1 day)
4. Test in staging (1 day)
5. Go live (1 day)

Total: 4–5 days. No engineering deployment. No code review cycle."

### Q: "What about compliance? GDPR? CCPA?"

**A:** "The platform is designed for compliance:
- Automated lineage (can trace data from source to dashboard)
- PII masking (sensitive data never reaches analytics)
- Audit logs (every read/write recorded)
- Legal hold support (can preserve specific records for litigation)
- Data retention enforcement (automatic deletion after retention window)

We're audit-ready for GDPR, CCPA, and SOC 2 now."

---

## Card 7: Closing (2 min)

### Slide Title
**"Let's Approve This and Move Forward"**

### Speaker Notes

"Here's what I'm asking for today:

**From Finance:** Approve the ~$1,700/month AWS + platform ops cost.

**From Security:** Sign off on the architecture and access controls (we've already passed CISO review).

**From Ops:** Agree to own the on-call playbook and alert responses (we provide the runbooks).

**From Executive Sponsor:** Give us the green light to go live in Week 1.

In return, you get:
- Data that's 97% fresher
- 10x faster source onboarding
- Compliance-ready audit trail
- 99.5% pipeline reliability

And within a month, your BI team is building dashboards on unified, current, clean data.

Questions?"

---

## Card 8: Handling Common Objections

### Objection: "Why wasn't this built 5 years ago?"

**Response:** "The technology (serverless Lambda, managed DynamoDB, S3 object lock) didn't exist at the same price point or maturity 5 years ago. Plus, the requirement to handle incremental sync, schema drift detection, and entity resolution across multiple sources is complex. We've built this once, with patterns learned from the industry's best practices (Airbnb, Uber, etc.)."

---

### Objection: "Isn't this just Fivetran? Why reinvent the wheel?"

**Response:** "Fivetran is great for simple point-to-point connectors. But we need:
- Custom entity resolution rules (matching logic specific to your business)
- Fine-grained PII masking policies (different compliance rules per entity)
- Post-transformation automation (entity resolution, golden records)

Fivetran can't do that without custom code. We built this once, it's yours, and it scales to your governance requirements."

---

### Objection: "What if the platform vendor support goes away?"

**Response:** "This isn't a vendor product. It's your infrastructure. All code is version-controlled in your Git repo. All infrastructure is defined in Terraform (you own it). If our platform team disbanded, the next engineer could maintain it using AWS and Python docs."

---

### Objection: "Doesn't this just move the problem to analytics?"

**Response:** "No. Analytics is only querying clean, mastered data. No joins needed. No schema ambiguity. An analyst can write a simple query like 'SELECT * FROM canonical.company WHERE region = 'US'' and get one authoritative answer."

The platform solves the **data engineering problem** (getting data clean and unified). Analytics solves the **business problem** (what does this data mean)."

---

## Card 9: Handout Summary (For Attendees)

Print and distribute:

```
╔════════════════════════════════════════════════════════════════╗
║  ENTERPRISE DATA LAKE PLATFORM — QUICK FACTS                  ║
╚════════════════════════════════════════════════════════════════╝

🚀 SPEED:
  • Data available in 1–4 hours (was 24–72 hours)
  • New sources in 2–3 days (was 2–4 weeks)

💰 COST:
  • ~$1,700/month AWS + platform ops
  • ROI < 3 months (vs. labor + SaaS tools)

🔒 SECURITY:
  • PII automatically masked
  • Audit trail for every read/write
  • Compliance-ready (GDPR, CCPA, SOC 2)

📊 QUALITY:
  • Golden records (unified customer view)
  • Automatic quality checks
  • Schema drift detection

✅ READINESS:
  • Production-ready today
  • Runbooks for all failure scenarios
  • 99.5% target reliability

NEXT STEPS:
  1. Executive approval (today)
  2. Go-live readiness review (this week)
  3. Production activation (next week)
  4. BI team training (week 2)
  5. Dashboard migration (week 3)

Questions? Contact: [Platform Engineering Lead]
Full documentation: /docs/
```

---

**Presenter Tips:**

1. **Timing:** 20 minutes for full presentation + 10 minutes Q&A
2. **Visuals:** Use the architecture diagram from PIPELINE_FLOW.md
3. **Live demo (optional):** Show one Athena query result from staging environment
4. **Tone:** Professional, confident, not overly technical for non-technical audience
5. **Contingency:** If tech questions get deep, defer to full documentation; offer follow-up technical Q&A session
6. **Approval:** Get sign-offs in writing (email) immediately after presentation

---

**Last updated:** 2026-06-17


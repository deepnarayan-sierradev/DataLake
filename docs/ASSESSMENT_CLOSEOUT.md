# Assessment Closeout — the frozen list

**Date:** 2026-07-29
**State:** all 21 findings from the third assessment addressed; **nothing applied to any AWS account**

This document exists to end a loop. Three assessments ran against this repository in two days and
produced 16, then 21 findings. The third pass was the point at which the pattern mattered more than
any individual finding, so this records both — what was fixed, and why the list is now closed rather
than continuing to grow.

---

## Why each pass found new things

The honest attribution, because it is the useful part:

| Pass | Findings | Of which were defects in the previous pass's own work |
|---|---|---|
| Wiring (2026-07-28) | — | fixed "the code exists but nothing calls it" |
| Remediation (2026-07-29) | 16 | **10** |
| Third assessment (2026-07-29) | 21 | **4** (F5 G4-shape, F13 paging adoption, F7/F10 partial cursors) |

So the previous pass *did* converge: 13 of its 16 landed properly. The other ~17 findings were in
code no earlier pass had examined. That is the mechanism: **"assess this repository" over 57k lines,
138 requirements and seven quality dimensions is an unbounded sample with no exit criterion.** Run it
again on this commit and it will return findings again, mostly different ones. That is not
convergence failure; it is the absence of a definition of done.

Two contributing habits, both mine:

1. **Commit messages and docs overclaimed.** "One DynamoDB paging primitive" was written when two of
   sixteen call sites used it. "Makes the omission a build error" was written about a gate that
   checked parameter defaults and not the `| None` type beside them. A reader reasonably concluded
   those were finished.
2. **The gates began certifying shape rather than effect.** Each was written by the same author as
   the code it guarded, so a misunderstanding of the failure mode was reproduced in both. G4
   approved a nullable parameter; G1 approved a module whose entry point was dead; G3 approved source
   text that never executed; G5 would have closed DL-SEM-07 by adding an id to an unreachable
   capability.

## What changed structurally, so the class stops recurring

Four root causes accounted for fourteen of the twenty-one. Each is fixed at the cause **and** gated:

| Root cause | Findings | The structural fix |
|---|---|---|
| Optional security parameter with a skip-on-None branch | F3, F5, F6 | Non-nullable types; `unrestricted_predicate(reason)` makes "unscoped" an audited object rather than an absence; **G4 now rejects `X \| None` and `is None` comparisons, not just `= None`** |
| An error path answered as an absence path | F2, F4, F16, F21 | `ScopeStoreUnavailableError`; empty unit set denies; `ScopeClaims` carries its partition model defaulting to the stricter value |
| A control conditioned on a value nothing supplies | F1, F11, F12, F14 | Terraform **interlocks** rather than flags: the unsafe combination is unreachable, and status is derived from the code (G9) rather than declared beside it |
| Reachability asserted at module granularity | F6, F7, F13 | **G10** asserts a delivered capability has a production caller; G3's export surface is now behavioural; G8 keeps the paging primitive singular |

Gates went from six to ten. Every new one has negative tests **and a positive control**, because a
gate that rejects everything passes every negative test and is exactly as useless as one that rejects
nothing.

## The 21, and where each stands

| # | Finding | State |
|---|---|---|
| F1 | IAM boundary unsatisfiable and unwired | **Mechanism + interlock done; 47 call sites remain (tracked, G9)** |
| F2 | Profile read failure read as `single` | Closed, negative test |
| F3 | Export ships every row on absent grant | Closed, behavioural test |
| F4 | `known and` short-circuit skipped unit validation | Closed |
| F5 | G4 enforced signature not semantics | Closed, gate hardened |
| F6 | `ExportService.execute` had no caller | Closed, artefact asserted in S3 |
| F7 | Semantic API exposed ~1/5 of the compiler | Closed, joins execute |
| F8 | Five of six stages enqueued to no DLQ | Closed, delivery asserted |
| F9 | `/entities` N+1, unbounded | Closed |
| F10 | `/runs` capped stage rows not runs | Closed, cursor continuity asserted |
| F11 | G6 absence alarms enabled nowhere | Closed (dev on; staging/prod at first run) |
| F12 | No CloudTrail; IAM metric name collided | Closed |
| F13 | Paging primitive adopted by 2 of 16 | Closed, G8 |
| F14 | Athena filters static over runtime registry | Closed (generator + drift alarm) |
| F15 | RLS drop/create denied readers every load | Closed |
| F16 | Scope denial surfaced as 500 | Closed |
| F17 | 3 packages outside the mypy scope | Closed |
| F18 | Suppressed counts disclosed peer existence | Closed |
| F19 | Docstring advertised a deleted route | Closed |
| F20 | Two full copies in a 512 MB Lambda | Closed |
| F21 | Page-token tenant check skippable | Closed |

**F1 is the one that is not fully closed, and it should not be recorded as closed.** The mechanism
(`tenancy/tenant_session.py`) exists, is tested, and the Terraform now refuses `enforce` until
adoption completes. But 47 data-plane call sites still build clients from ambient credentials, so
they sit outside the boundary regardless of the policy. Threading a tenant-tagged session through
~47 repository constructors is design-sized work. Three things keep that visible rather than
forgotten: `make tenant-session-adoption`, a pending-capability test that fails if the helper gains
a caller without this record being updated, and the waiver in `requirements/WAIVERS.md`.

## The frozen exit list

**Nothing gets added to this list.** It is the definition of done for deployability, and it is short
on purpose. New findings go to `docs/KNOWN_GAPS_AND_ROADMAP.md` as roadmap items, not here.

### Before any `terraform apply`

1. **Name the environment and approve the apply.** Nothing in this repository has ever been applied
   beyond dev's pre-programme state.
2. **Run the two data migrations first**, per environment: `make migrate-connections`, then
   `make migrate-credentials` (dry-run, verify, `--apply`).
3. **Re-run `scripts/seed_schedules.py`** after apply, or no cron triggers exist.

### Before `enforce` on either audit-mode control

4. **IAM tenant boundary:** complete the 47-site session adoption, set
   `tenant_session_tagging_adopted = true`, then observe `IamBoundaryAccessDenied` at zero for a
   sustained window. The interlock refuses the flip until the first step is done.
5. **WAF:** review counted matches, then switch `waf_enforcement_mode`.

### Before a second real tenant's data lands

6. **Populate `scope_unit_row_filters` and `scope_unit_grants`** from
   `scripts/generate_scope_unit_filters.py --write`, with real principal ARNs. Until then Athena
   row-level isolation enforces nothing, and `ScopeFilterDrift` will say so.

### Before any BI tool connects

7. **Decide the serving-store network path** (gap 4). AWS Client VPN is the recommended default.
8. **Build the component that sets `edl.scope_units`** on a BI connection. The RLS policy is correct
   and inert without it.
9. **Prove RLS behaviourally** against a real PostgreSQL: load → apply → load → reader sees only its
   own unit. The 18 existing tests are SQL-shape assertions and say so.

### Product answers still outstanding

10. **Is the 2–4 hour freshness commitment measured from each run's start, or an absolute daily
    deadline?** From run start, per-tenant staggering solves the concurrency wall for free; absolute,
    a concurrency-limit increase becomes mandatory. Cron jitter is deliberately unimplemented until
    this is settled (gap 23).
11. **Which entity gets the first quality policy, and at what thresholds?** The DL-DQ gate returns
    "ungated" everywhere until one is attached.

## Recommendation on how to work from here

Stop running the broad assessment. It will keep returning ~20 findings for as long as it is run,
because its scope is the whole repository rather than a decision.

The highest-value next action is not another audit: it is **deploying to the new account and running
one real tenant end to end.** Almost every durable finding in all three passes reduces to "this has
never been executed against real AWS" — the control plane's claims shape, the RLS behaviour, the
boundary's enforce mode, the DLQ alarms, the Lambda package's DuckDB paths. A day of that produces
more truth than a third re-read of the same code.

## Verification at closeout

```
2,981 tests          (was 2,854; +127, all behavioural rather than source-inspecting)
ruff                 clean
mypy                 clean, 425 files across 20 packages
bandit               0 findings at every severity
gates                10 green (G1–G10)
terraform validate   dev, staging, prod
applied to AWS       nothing
```

One process note worth keeping: a bulk comment-rewrap script written during this pass split code
across lines in 21 files. `ruff` caught it as syntax errors and the damage was reverted from the
previous commit. Mechanical edits over prose must not be able to touch code — the tooling equivalent
of the finding class this whole pass was about.

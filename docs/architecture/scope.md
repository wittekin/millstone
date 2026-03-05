# Scope and Boundaries

This document defines the intended scope for `millstone`, including operational boundaries and required controls.

## Scope Objective

`millstone` orchestrates knowledge-work loops where an author and reviewer produce verifiable outcomes from defined work items.

The project supports both:
- local artifact workflows (for example code/docs), and
- controlled remote-state workflows (for example ticket transitions, deploy actions), when safety controls are satisfied.

## Task Classes

| Class | Description | Typical effects |
|---|---|---|
| `transformative` | edits or creates artifacts | file updates, commits, docs revisions |
| `planning` | turns requests into executable work definitions | worklist/backlog updates, specs |
| `transactional` | applies bounded system-of-record updates | issue status changes, release metadata updates |
| `investigative` | analyzes state and produces recommendations | reports/findings with no direct state change |
| `operational` | executes live-system changes | deploys, rollbacks, runtime config changes |

To distinguish `transactional` from `operational`: transactional tasks update metadata or record state without observable runtime consequence (for example closing a ticket, updating release notes); operational tasks have observable runtime consequence (for example deploys or rollbacks affecting live traffic). When in doubt, classify as `operational`.

## Capability Tiers

| Tier | Definition | Allowed actions |
|---|---|---|
| `C0_read_only` | read-only observation | read files/APIs/logs; no state mutation |
| `C1_local_write` | local artifact mutation | modify local workspace artifacts |
| `C2_remote_bounded` | bounded remote mutation | idempotent, bounded API transitions |
| `C3_remote_critical` | high-impact remote mutation | production/runtime-affecting actions |

## Conformance Requirements

All workflows, providers, and profiles must conform to the task-class and capability-tier model in this document.

- Artifact contracts across `opportunity` / `design` / `tasklist` are normative.
- Provider adapters must preserve canonical fields, statuses, and reference integrity.
- Approval, evidence, and rollback controls are mandatory by tier as defined below.

## Required Controls by Tier

| Tier | Minimum controls |
|---|---|
| `C0_read_only` | audit log + evidence output |
| `C1_local_write` | mechanical checks + reviewer approval + evidence |
| `C2_remote_bounded` | explicit policy allowlist + reviewer approval + (idempotency required OR rollback/compensation plan documented) + evidence |
| `C3_remote_critical` | all `C2` controls + explicit human approval gate at execution time + environment/risk guardrails + post-action health checks |

## In Scope

- `transformative` and `planning` workflows by default.
- `investigative` workflows that produce recommendations/evidence.

## Conditionally In Scope

- `transactional` workflows when provider adapters and an explicit policy allowlist are in place (minimum `C2` controls).
- `operational` workflows only through explicit profiles with strict controls (`C3_remote_critical`).

## Out of Scope

These are hard constraints, not configuration defaults. The first two can move into conditional scope only when corresponding controls are fully satisfied. Privilege escalation outside declared profile capabilities is an absolute constraint.

- Unbounded autonomous production operations without explicit approvals.
- Cross-system remote effects without normalized providers, policy controls, and verifiable evidence.
- Privilege-escalating actions outside declared profile capabilities.

## Autonomous Cycle Operation

When `millstone` runs in cycle mode (`analyze -> design -> plan -> build -> eval`), the planning loop can generate its own work items. That creates self-directed risk beyond human-authored worklists because selection and decomposition are also automated.

Primary controls for this mode are approval gates (`approve_opportunities`, `approve_designs`, `approve_plans`). Generated work items still inherit normal capability-tier controls.

Fully autonomous operation (`--no-approve`) requires explicit opt-in and should be governed with the same approval rigor expected for `C3_remote_critical`, even when individual tasks are lower tier.

## Scope Decision Checklist

Before adding a new workflow/profile, answer:

1. What task class is this?
2. What capability tier is required?
3. What effects are expected?
4. What evidence will prove success?
5. (`C2+` only) What rollback/compensation path exists if effects fail?
6. What approval gates are required by risk level?
7. Does this require a dedicated design stage before planning (`opportunity -> design -> tasklist`), or can it proceed directly (`opportunity -> tasklist`)?

If any item is undefined, the workflow is not ready for general availability.

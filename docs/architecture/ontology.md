# Loop Ontology

This document is the normative terminology model for `millstone`.
It defines the terms used by core loop architecture and integrations.

## Core Execution Model

Every loop execution is modeled as:

`work item` + `context` + `capabilities` -> `effects` + `evidence`

- `work item`: the requested outcome.
- `context`: the information/state needed to do it.
- `capabilities`: the tools/permissions available to the loop.
- `effects`: the state changes caused by execution.
- `evidence`: verification artifacts proving the outcome.

This model applies to code, docs, planning, and operations workflows.

Effects and evidence are related but distinct: effects are what changed, evidence is the proof that those changes satisfy acceptance criteria. They are not symmetric outputs. Some loops may produce evidence without direct effects (for example investigative workflows), and low-risk local loops may use lightweight evidence rather than durable audit artifacts.

## Key Terms

- `work item`: the requested outcome, aka the thing to do.
- `backlog`: the full set of potential work from one or more systems of record.
- `worklist`: the selected, executable subset being worked by a loop. Moving items from backlog to worklist is itself a planning task, done by a planning loop or a human.
- `opportunity`: a candidate improvement identified during analysis; input to planning that can yield worklist entries.
- `design`: a specification derived from an opportunity that defines approach before concrete work items are generated.
- `context`: the information and state needed to execute the work item.
- `capability`: the tools and permissions available to the loop.
- `effect`: the state change caused by execution (local or remote).
- `evidence`: the proof that execution met requirements.
- `handoff artifact`: an artifact produced by one loop and consumed by another.
- `terminal artifact`: an artifact produced for end use with no required downstream loop consumer.
- `artifact provider`: an adapter for reading/writing normalized artifacts (file, Linear, Jira, etc.).
- `effect provider`: an adapter for applying/observing remote state transitions.
- `profile`: a workflow mode that binds role aliases, contracts, and controls.

## Canonical Roles

- `author`: produces or revises the target output for a work item.
- `reviewer`: evaluates output quality, policy compliance, and acceptance criteria.
- `sanity` (optional): validates output is non-degenerate before handing off (for example rejects refusals, gibberish, or destructive no-ops). It does not evaluate quality or correctness; that is the reviewer's responsibility.

Profile aliases are allowed, for example:
- development profile: `builder` alias for `author`.
- documentation profile: `editor` alias for `author`.

Aliases may be used intentionally in prompts to activate domain-specific priors in LLM behavior. Core APIs and architecture terms remain canonical.

## Artifact and Effect Semantics

- `input_artifacts`: artifacts consumed during authoring/review (requirements, codebase, redlines, runbooks, tickets).
- `output_artifacts`: artifacts directly produced (commits, docs, generated plans, reports).
- `effects`: broader state transitions beyond artifacts (deploy rollouts, ticket transitions, release publication, environment updates).

A `handoff artifact` is both an `output_artifact` of the producing loop and an `input_artifact` of the consuming loop.

Use `effects` when output is operational/remote and not primarily a file artifact.

When a handoff artifact has parser/validator expectations on both producer and consumer sides, treat it as a formal interface contract. Breaking its structure is a breaking change.

Canonical handoff chain in `millstone`:

- analyze -> `.millstone/opportunities.md` -> design loop
- design -> `.millstone/designs/<slug>.md` -> planning loop
- planning -> tasklist artifact (default `.millstone/tasklist.md`) -> execution loop

At runtime, this chain is implemented by the pipeline module (`loops/pipeline/`). Each link is a `Stage` with typed `input_kind` / `output_kind` edges using `HandoffKind` (`OPPORTUNITY`, `DESIGN`, `WORKLIST`). `PipelineDefinition.validate()` ensures adjacent stages have compatible handoff kinds. `--through` controls how far `--analyze`, `--design`, or `--plan` chains forward (e.g. `--analyze --through plan` stops before execution); `--cycle` resolves which pipeline shape to build based on triage (pending tasks, roadmap goals, or fresh analysis).

## Artifact Contract Model

`millstone` treats the handoff chain as three canonical artifact contracts, each with provider-backed identity and references:

- `opportunity`: backlog candidate selected for potential adoption and design.
- `design`: solution spec keyed by design identity and linked to source opportunity.
- `tasklist item`: executable work item optionally linked back to design and opportunity.

Canonical minimum fields:

| Contract | Required fields | Optional fields | Identity |
|---|---|---|---|
| `opportunity` | `opportunity_id`, `title`, `status`, `description` | `design_ref`, `source_ref`, `priority`, `tags`, `requires_design` | `opportunity_id` (slug-like) |
| `design` | `design_id`, `title`, `status`, `opportunity_ref`, `body` | `tasklist_ref`, `review_summary` | `design_id` (slug-like) |
| `tasklist item` | `task_id`, `title`, `status` | `design_ref`, `opportunity_ref`, `risk`, `tests`, `criteria`, `context` | `task_id` (stable item id) |

Providers must expose canonical identities in normalized models. When ingesting non-conforming sources, adapters must normalize records before handing them to orchestration.

`source_ref` is an optional pointer to where an opportunity came from (for example analysis run artifact, issue URL/key, roadmap entry, or note path). Providers should preserve it as provenance metadata.
`requires_design` is an optional explicit gate for whether a dedicated design artifact is required before planning.

Status enums (canonical):

- opportunity: `identified`, `adopted`, `rejected`
- design: `draft`, `reviewed`, `approved`, `superseded`
- tasklist item: `todo`, `in_progress`, `done`, `blocked`

`opportunity.design_ref` is relational state (a linked design exists); it should be treated as derived linkage, not as an intrinsic opportunity status enum value.
Derived "designed" state may be reported by providers, but it is not a canonical persisted status value.

Provider-specific encodings (for example markdown checkboxes, Jira fields, or Linear states) are not ontology-level definitions and must be specified in provider contract documents.

Reference integrity rules:

- `design.opportunity_ref` must resolve to an existing `opportunity.opportunity_id`.
- `opportunity.design_ref` (when present) must resolve to an existing `design.design_id`.
- `tasklist_item.design_ref` and `tasklist_item.opportunity_ref` (when present) must resolve.

File-backed provider defaults are deployment defaults, not ontology constraints:

- opportunities collection default: `.millstone/opportunities.md`
- designs collection default: `.millstone/designs/`
- tasklist default: `.millstone/tasklist.md`

These paths are gitignored by default (local-only). To commit artifacts to the repo, set
`commit_tasklist`, `commit_designs`, or `commit_opportunities` to `true` in
`.millstone/config.toml`, which falls back to the legacy tracked paths (`docs/tasklist.md`,
`designs/`, `opportunities.md`). For multi-maintainer collaboration, prefer an external
artifact provider (Jira, Linear, or GitHub Issues) over committed file artifacts.

Providers may map these contracts to files, Jira, Linear, Confluence, or other systems, but must preserve canonical fields and reference semantics.

## Design Gate

The canonical default path is:

- `opportunity -> tasklist`

Use this expanded path only when needed:

- `opportunity -> design -> tasklist`

`design` is required when at least one of the following is true:

- change is cross-cutting across multiple components or interfaces
- risk is `high` or rollback/compensation is non-trivial
- approach is ambiguous and needs alternatives/trade-off analysis
- work includes migration, compatibility, or external contract changes
- effects may be hard to reverse

If none apply, planning may proceed directly from opportunity to tasklist.

`requires_design` ownership and precedence:

- The field may be set by an analyzer/provider or by a human.
- If `requires_design` is explicitly set, that explicit value takes precedence.
- If the field is absent, planning logic evaluates the gate criteria and derives the decision.
- Human override is allowed and should be logged in evidence.

## Provider Model

Core orchestration consumes normalized models from adapters:

- `artifact provider` for contract-aware read/list/write/validate operations on `opportunity`, `design`, and `tasklist` artifacts.
- `effect provider` for remote state transitions and status reads.

Expected provider examples:

- file-backed markdown (`.millstone/tasklist.md`)
- MCP (agent-delegated reads and writes via configured MCP servers)
- deployment/ops APIs (profile-gated)

Provider-specific logic should stay in adapters, not in core loop semantics.

When an adapter both persists records and transitions remote state (for example updating and closing a Linear ticket), classify by primary purpose: if intent is record persistence, model it as an artifact provider; if intent is enacting a state transition, model it as an effect provider. Adapters that do both should expose both interfaces.

## Profile Model

A `profile` binds:

- role aliases,
- input/output artifact contracts,
- permitted effect classes,
- verification requirements,
- approval and safety gates,
- default providers.

Examples:

- `dev_implementation`: worklist item -> code changes + test evidence + worklist update.
- `feature_triage_to_plan`: feature requests -> prioritized worklist/backlog entries.
- `document_revision`: redlines -> revised document + review evidence.
- `operational_change`: runbook item -> controlled remote effects + health evidence.

## Naming Rules

- Core APIs/docs use canonical terms from this document.
- Domain-specific wording is profile-level aliasing, not ontology.
- New integrations must normalize to canonical models.
- Profile identifiers use `snake_case`.
- Scope and boundary decisions are defined in `docs/architecture/scope.md`.

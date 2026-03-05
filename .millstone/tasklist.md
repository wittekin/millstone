# Tasklist

E2E testing plan for millstone. New test files live under `tests/e2e/` with a role-aware stub harness so fixtures don't leak into the main suite.

## Completed

**Group A (Infrastructure):** Created `tests/e2e/` package with `stub_cli` fixture in `tests/e2e/conftest.py` that routes by role kwarg rather than prompt substring. Added `@pytest.mark.real_cli` and `@pytest.mark.real_mcp` markers with per-provider skip guards and opt-in `--run-real-cli`/`--run-real-mcp` pytest flags.

**Group B (Stub-CLI Inner Loop):** Added E2E assertions for prompt token resolution (`{{WORKING_DIRECTORY}}` substituted, no bare tokens), reviewer feedback forwarded verbatim to second builder call, per-role CLI dispatch verified via subprocess-level patching with set-membership assertions, and dry-run output scanned for unresolved template tokens.

**Group C (File Provider Lifecycle):** Covered analyze→design→plan→execute full cycle with empty repo fixture; `commit_tasklist=True` tested at both inner-loop and CLI subprocess levels; `commit_designs`/`commit_opportunities` artifact placement verified; custom `--prompts-dir` template substitution tested; `run_eval()` JSON output and `eval_on_commit=True` regression halt verified.

**Group D (MCP Provider Lifecycle):** Verified MCP tasklist rendered builder prompt contains correct READ/COMPLETE instruction clauses; `tasklist_filter` label and project clauses propagated through full config→prompt pipeline; explicit empty labels override `tasklist_filter`; MCP snapshot/restore removes only interleaved tasks.

**Group E (Real CLI Smoke Tests):** Real claude and codex `--task` single-task smoke tests with 120s SLA; mixed `--cli-builder claude --cli-reviewer codex` in-process orchestrator test verifying both binaries observed.

**Group F (Real MCP):** GitHub Issues full lifecycle test (create issue, run with MCP provider, assert closed and committed). Label filter narrows scope — unlabelled issue untouched.

**Group G (Parallel Worktree):** No new tests needed; existing `test_parallel.py` covers two-task merge and stale-worker cleanup.

---

## Review Fixes

- [x] **Rewrite `commit_tasklist` CLI-path test as a behavior contract (not a prompt oracle)** in
  `tests/e2e/test_e2e_file_lifecycle.py`.

  Keep two tests:
  1. Fast in-process path (`Orchestrator(tasklist="docs/tasklist.md")`) validating that docs tasklist can be completed and committed.
  2. Real config/CLI path (`commit_tasklist = true` + `millstone` subprocess) validating user-facing remap behavior through `main()`.

  For the CLI-path test, remove prompt-path assertions and remove `.millstone/_stub_tl_path` cache files. Use distinct first tasks in
  `docs/tasklist.md` and `.millstone/tasklist.md`; assert outcomes only:
  - docs task is completed/committed
  - default-path task remains untouched
  - run exits successfully

  Keep PATH/env cleanup via `try/finally`.

  - Est. LoC: ~50 changed
  - Risk: low
  - Criteria: no assertions on prompt text/path strings; remap proven by final repo state

- [x] **Convert per-role CLI dispatch test to a contract test** in
  `tests/e2e/test_e2e_inner_loop.py`.

  Replace call-order/count/version-filter assertions with a behavioral gate:
  configure distinct CLIs for builder/reviewer and use a subprocess spy/stubs such that
  only the correct CLI-role mapping can produce a successful run.
  Example contract:
  - builder CLI path can create/stage code changes
  - reviewer CLI path can return valid review JSON
  - swapped mapping must fail or no-op

  Assert only externally meaningful outcomes: run success, commit created, expected file change present.

  - Est. LoC: ~35 changed
  - Risk: medium
  - Criteria: test fails when role routing is wrong, passes when role routing is correct, without relying on internal call sequencing

- [x] **Remove circular placeholder oracles from MCP E2E prompt tests** in
  `tests/e2e/test_e2e_mcp_lifecycle.py`.

  In prompt-content tests, stop deriving expected text from `provider.get_prompt_placeholders()` and then asserting exact inclusion.
  Assert declarative properties instead:
  - configured server identifier is present
  - unresolved provider tokens are absent
  - completion guidance is non-empty and semantically present (without exact phrase pinning)

  - Est. LoC: ~25 changed
  - Risk: low
  - Criteria: no exact equality/inclusion checks against provider-generated placeholder strings in E2E prompt tests

- [x] **Move non-E2E MCP snapshot/restore coverage to unit suite**.

  Move `TestMCPSnapshotRestore.test_restore_snapshot_removes_only_interleaved_task`
  from `tests/e2e/test_e2e_mcp_lifecycle.py` to a unit module under `tests/`
  (reuse existing MCP provider unit suite file naming conventions).

  - Est. LoC: ~20 moved
  - Risk: low
  - Criteria: E2E file contains orchestrator flow tests only; provider internals tested in unit suite

- [x] **Remove unit-level provider-construction assertions from MCP filter E2E tests** in
  `tests/e2e/test_e2e_mcp_lifecycle.py`.

  For label/project/explicit-empty-label cases, drop direct `MCPTasklistProvider(...)` /
  `from_config(...)` assertions in E2E tests. Keep only end-to-end assertions through
  `config.toml` -> orchestrator -> rendered prompt / run behavior.

  - Est. LoC: ~20 changed
  - Risk: low
  - Criteria: E2E tests assert end-to-end behavior only; provider-constructor logic remains covered in unit tests

- [x] **Add passing path for `eval_on_commit=True`** in
  `tests/e2e/test_e2e_file_lifecycle.py`.

  Add `test_eval_on_commit_passing_does_not_halt` with passing eval script and assert exit 0.
  Keep existing regression test asserting exit 1.

  - Est. LoC: ~30
  - Risk: low
  - Criteria: both pass and fail behavior paths covered for eval-on-commit

- [x] **Strengthen full-cycle assertion to target intended task completion** in
  `tests/e2e/test_e2e_file_lifecycle.py`.

  In `test_full_file_provider_cycle`, assert the specific planned task title is the one completed
  (not merely existence of any `- [x]` checkbox).

  - Est. LoC: ~10
  - Risk: low
  - Criteria: completion assertion is tied to planned task identity, not generic checkbox presence

- [x] **Strengthen real CLI smoke tests to validate requested functionality** in
  `tests/e2e/test_e2e_real_cli.py`.

  In real claude/codex task tests, replace file-size checks with semantic checks on output artifact
  (e.g., `greet.py` contains `def greet(`). Apply the same semantic artifact assertion to the mixed
  builder/reviewer test so it does not rely only on commit-count deltas + binary-observation checks.
  Keep exit-code assertions.

  - Est. LoC: ~18
  - Risk: low
  - Criteria: no `stat().st_size` oracle; mixed test validates functional file content, not only commit-count/binary-call side effects

- [x] **Remove prompt-text path coupling from commit-designs E2E test** in
  `tests/e2e/test_e2e_file_lifecycle.py`.

  In `test_commit_designs_tracked_paths`, drop assertions that inspect prompt text for concrete path strings.
  Assert only observable artifact outcomes (files created in tracked locations, absent in `.millstone/` paths).

  - Est. LoC: ~15 changed
  - Risk: low
  - Criteria: test remains green/meaningful across prompt wording refactors while enforcing path behavior

- [x] **Strengthen custom-prompts E2E coverage to assert full placeholder resolution** in
  `tests/e2e/test_e2e_file_lifecycle.py`.

  `test_custom_prompts_dir` currently checks custom-template loading and `{{WORKING_DIRECTORY}}` only.
  Extend it to assert the provider placeholders used in the custom template are actually resolved too:
  - no raw `{{TASKLIST_READ_INSTRUCTIONS}}` or `{{TASKLIST_COMPLETE_INSTRUCTIONS}}` tokens remain
  - rendered prompt contains non-empty tasklist read/complete guidance

  Keep assertions behavioral (resolved output properties), not exact string pinning to provider internals.

  - Est. LoC: ~12
  - Risk: low
  - Criteria: custom template test fails when any required placeholder remains unresolved, passes across prompt wording refactors

- [x] **Rewrite real MCP full-lifecycle commit oracle to functional artifact assertions** in
  `tests/e2e/test_e2e_real_mcp.py`.

  In `test_real_mcp_github_issues_full_lifecycle`, stop using `len(git log)` deltas as the primary success signal.
  Assert requested behavior directly on repository artifacts: `hello.py` must contain `def hello(`,
  while retaining issue-state assertions.

  If commit existence is still checked, make it secondary and content-based (e.g., HEAD diff touches expected file)
  rather than raw commit-count growth.

  - Est. LoC: ~14
  - Risk: medium
  - Criteria: real MCP test enforces user-visible implementation outcome + issue closure, not incidental commit-count changes

---

## Real CLI / Linear MCP

- [x] **Fix real_cli skip guards to check CLI availability instead of API key env vars** in
  `tests/e2e/conftest.py`. The current guards check `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`,
  but both `claude` (Claude Code) and `codex` use OAuth-based auth — those env vars are never
  set in this environment. The correct signal is whether the CLI binary is installed and returns
  exit 0 from `--version`.

  Replace the env-var checks with calls to the millstone provider registry:
  ```python
  from millstone.agent_providers.registry import get_provider

  def _real_cli_skip_reason(provider, *, flag_passed):
      if not flag_passed:
          return "--run-real-cli not passed"
      cli_map = {"claude": ["claude"], "codex": ["codex"], "mixed": ["claude", "codex"]}
      for cli_name in cli_map.get(provider, [provider]):
          available, msg = get_provider(cli_name).check_available()
          if not available:
              return msg
      return None
  ```

  For `_real_mcp_skip_reason`: remove the `GH_TOKEN` check entirely — `real_mcp` tests
  requiring GitHub can add their own `pytest.skip` inline if `MILLSTONE_TEST_REPO` is absent.
  The shared guard only enforces `--run-real-mcp` flag presence.

  Also update `test_real_cli_markers.py` to remove any tests that assert on `ANTHROPIC_API_KEY`
  / `OPENAI_API_KEY` absence as a skip condition, since those conditions no longer apply.

  - Est. LoC: ~30 changed
  - Risk: low
  - Criteria: `pytest tests/e2e/test_e2e_real_cli.py --run-real-cli` runs (not skips) all three
    tests when `claude --version` and `codex --version` both succeed

- [x] **Fix real CLI test timeouts and mixed-test max_cycles** in
  `tests/e2e/test_e2e_real_cli.py`. Two observed failures from running with `--run-real-cli`:

  **Part A — codex timeout**: `test_real_codex_task_creates_commit` uses `timeout=120` in the
  `subprocess.run` call AND asserts `elapsed < timeout`. Codex takes >120s in practice (model
  is `gpt-5.3-codex` with `model_reasoning_effort = "high"`). Raise both the subprocess
  `timeout=` argument and the SLA assertion to `300` for codex. Update the 120s SLA comment in
  the test to note it is provider-specific.

  **Part B — mixed test exit 1**: `test_real_mixed_task_creates_commit` creates `Orchestrator`
  with default `max_cycles=3`. With claude as builder and codex as reviewer, 3 cycles can be
  exhausted if codex is a strict reviewer. Pass `max_cycles=6` explicitly. Also add
  `max_cycles=6` to the claude single-test Orchestrator equivalent (currently run as a
  subprocess; no change needed there). The elapsed SLA on the mixed test is currently 120s;
  raise it to `300` to match the codex-provider timeout.

  - Est. LoC: ~10 changed
  - Risk: low
  - Criteria: codex test runs to completion (no TimeoutExpired); mixed test reaches APPROVED or
    correct exit 0 outcome; SLA assertions still present but at realistic thresholds

- [x] **Add real Linear MCP tests** in new `tests/e2e/test_e2e_real_mcp_linear.py`. ✓ Both tests pass (265s / 150s).
  Marker: `@pytest.mark.real_mcp`. Skip guard (in addition to `--run-real-mcp` flag):
  check `MILLSTONE_TEST_LINEAR_TEAM` env var (Linear team name or ID for test isolation).

  **Test setup/teardown**: use the Linear GraphQL API (`https://api.linear.app/graphql`)
  with auth token from `LINEAR_API_KEY` env var if set, otherwise fall back to the OAuth
  access token stored by codex at `~/.codex/.credentials.json` under the key matching
  `server_name == "linear"`. Add a helper `_linear_api(query, variables)` that handles both.

  **Test 1 — full lifecycle** (`test_real_linear_mcp_task_creates_commit`):
  - Setup: create a Linear issue in `MILLSTONE_TEST_LINEAR_TEAM` with title
    `"millstone e2e: add hello() to hello.py"` and description
    `"Add a hello() function that prints 'hello world' to hello.py."`,
    labelled `millstone-e2e` (create label if absent).
  - Run: `millstone --cli codex` with `tasklist_provider=mcp`, `mcp_server=linear`,
    `tasklist_filter.label=millstone-e2e` in a `temp_repo`.
  - Assert: (a) exit 0; (b) `hello.py` exists in repo and contains `def hello(`; (c) issue
    state is `completed` or `done` via Linear API; (d) git commit created in repo.
  - Teardown: cancel/delete any remaining open `millstone-e2e` issues.

  **Test 2 — label filter** (`test_real_linear_label_filter_narrows_scope`):
  - Same pattern as GitHub label filter test: two issues, one labelled `millstone-e2e`,
    one unlabelled. Run with `tasklist_filter.label=millstone-e2e`. Assert only labelled
    issue is completed; unlabelled remains open.

  - Est. LoC: ~140
  - Risk: medium (live Linear; mitigated by dedicated label and teardown)
  - Criteria: `hello.py` contains `def hello(`; Linear issue completed; no orphan issues
    post-teardown; unlabelled issue untouched in filter test

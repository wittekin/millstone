"""E2E: file-provider lifecycle tests (stub-CLI, inner and outer loops)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

from millstone.runtime.orchestrator import Orchestrator
from tests.e2e.conftest import StubCli

# ---------------------------------------------------------------------------
# Shared canned responses
# ---------------------------------------------------------------------------

_APPROVED_JSON = (
    '{"status": "APPROVED", "review": "Looks good", "summary": "Looks good!",'
    ' "findings": [], "findings_by_severity":'
    ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}'
)
_SANITY_OK_JSON = '{"status": "OK", "reason": ""}'
# Analyze review: _parse_analyze_review_verdict expects {"verdict": "APPROVED", ...}
_ANALYZE_REVIEW_APPROVED = (
    '{"verdict": "APPROVED", "score": 9, "strengths": [], "issues": [], "feedback": ""}'
)
# Design review (keyword-based fallback): string containing "APPROVED"
_DESIGN_REVIEW_APPROVED = "APPROVED"
# Plan review: review_plan parses JSON and checks verdict=="APPROVED"
_PLAN_REVIEW_APPROVED = '{"verdict": "APPROVED", "feedback": [], "score": 9}'

# Opportunity and design IDs derived from title slug
_OPP_ID = "add-type-annotations"
_DESIGN_ID = "add-type-annotations"

# ---------------------------------------------------------------------------
# Side-effect helpers
# ---------------------------------------------------------------------------


def _write_opportunities(repo: Path) -> None:
    """Write a minimal opportunities.md in checklist format."""
    opp_file = repo / ".millstone" / "opportunities.md"
    opp_file.parent.mkdir(parents=True, exist_ok=True)
    opp_file.write_text(
        "- [ ] **Add type annotations**\n"
        f"  - Opportunity ID: {_OPP_ID}\n"
        "  - Description: Add type annotations to core functions.\n"
        "  - ROI Score: 8.0\n"
    )


def _write_design(repo: Path) -> None:
    """Write a minimal design file in canonical metadata-block format."""
    designs_dir = repo / ".millstone" / "designs"
    designs_dir.mkdir(parents=True, exist_ok=True)
    design_file = designs_dir / f"{_DESIGN_ID}.md"
    design_file.write_text(
        f"# Add type annotations\n\n"
        f"- **design_id**: {_DESIGN_ID}\n"
        f"- **title**: Add type annotations\n"
        f"- **status**: draft\n"
        f"- **opportunity_ref**: {_OPP_ID}\n\n"
        f"---\n\n"
        f"Add type annotations to all public functions in the codebase.\n"
    )


def _write_task(repo: Path) -> None:
    """Append a single unchecked task to the tasklist."""
    tasklist_path = repo / ".millstone" / "tasklist.md"
    content = tasklist_path.read_text()
    content += "\n- [ ] Stub task: add type annotations\n"
    tasklist_path.write_text(content)


def _make_code_change(repo: Path) -> None:
    """Create a Python file and stage it (so git diff is non-empty)."""
    (repo / "annotated.py").write_text("def greet(name: str) -> str:\n    return f'Hello {name}'\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=False)


def _commit_with_tick(repo: Path) -> None:
    """Tick the first unchecked task, stage all changes, and commit."""
    tasklist_path = repo / ".millstone" / "tasklist.md"
    content = tasklist_path.read_text()
    content = content.replace("- [ ]", "- [x]", 1)
    tasklist_path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)
    subprocess.run(
        ["git", "commit", "-m", "Stub: add type annotations"],
        cwd=repo,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullFileProviderCycle:
    """analyze → design → plan → execute full file-provider lifecycle."""

    def test_full_file_provider_cycle(self, empty_repo: Path, stub_cli: StubCli) -> None:
        """Run full cycle starting from an empty tasklist.

        The cycle must proceed through analyze → design → plan → execute
        because there are no pending tasks at the start.

        Stub sequencing (role dispatch order):
          1. analyzer  – writes opportunities.md
          2. reviewer  – approves analyze output
          3. author    – writes design file (design ArtifactReviewLoop, produce pass)
          4. reviewer  – approves design (design ArtifactReviewLoop, review pass)
          5. author    – writes task to tasklist (plan ArtifactReviewLoop, produce pass)
          6. author    – approves plan (plan ArtifactReviewLoop, review pass)
          7. author    – makes code change (inner-loop builder)
          8. sanity    – implementation sanity check passes
          9. reviewer  – inner-loop review approves
         10. builder   – ticks tasklist and commits

        Assertions:
          (a) .millstone/opportunities.md created and non-empty
          (b) at least one file under .millstone/designs/
          (c) at least one - [x] entry in tasklist.md
          (d) a new git commit exists (beyond the initial two)
        """
        # --- Analyze phase ---
        stub_cli.add(
            role="analyzer",
            output="Analysis complete.",
            side_effect=_write_opportunities,
        )
        stub_cli.add(role="reviewer", output=_ANALYZE_REVIEW_APPROVED)

        # --- Design phase (ArtifactReviewLoop inside run_design) ---
        stub_cli.add(
            role="author",
            output="Design written.",
            side_effect=_write_design,
        )
        stub_cli.add(role="reviewer", output=_DESIGN_REVIEW_APPROVED)

        # --- Plan phase (ArtifactReviewLoop inside run_plan) ---
        stub_cli.add(
            role="author",
            output="Tasks added.",
            side_effect=_write_task,
        )
        stub_cli.add(role="author", output=_PLAN_REVIEW_APPROVED)

        # --- Inner loop ---
        stub_cli.add(
            role="author",
            output="Implementation done.",
            side_effect=_make_code_change,
        )
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_commit_with_tick,
        )

        orch = Orchestrator(
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
            review_designs=False,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
            max_tasks=1,
        )
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run_cycle()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # (a) opportunities.md created and non-empty
        opp_file = empty_repo / ".millstone" / "opportunities.md"
        assert opp_file.exists(), ".millstone/opportunities.md was not created"
        assert opp_file.read_text().strip(), ".millstone/opportunities.md is empty"

        # (b) at least one file under .millstone/designs/
        designs_dir = empty_repo / ".millstone" / "designs"
        assert designs_dir.exists(), ".millstone/designs/ directory was not created"
        design_files = [f for f in designs_dir.iterdir() if f.is_file()]
        assert design_files, "No design files found under .millstone/designs/"

        # (c) the planned task is marked complete in tasklist.md
        planned_task_title = "Stub task: add type annotations"
        tasklist_content = (empty_repo / ".millstone" / "tasklist.md").read_text()
        assert f"- [x] {planned_task_title}" in tasklist_content, (
            f"Expected planned task '{planned_task_title}' to be completed (- [x]) "
            f"in tasklist:\n{tasklist_content}"
        )

        # (d) a new git commit exists beyond the single initial commit
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=empty_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        commits = [line for line in result.stdout.strip().splitlines() if line]
        assert len(commits) >= 2, (
            f"Expected at least 2 commits (1 initial + 1 new), got {len(commits)}:\n"
            + "\n".join(commits)
        )


# ---------------------------------------------------------------------------
# Helpers for commit_tasklist tests
# ---------------------------------------------------------------------------


def _make_code_change_for_docs(repo: Path) -> None:
    """Create a Python file and stage it (so git diff is non-empty)."""
    (repo / "impl.py").write_text("def stub(): pass\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)


def _commit_docs_tasklist_tick(repo: Path) -> None:
    """Tick docs/tasklist.md, stage all changes, and commit."""
    tasklist_path = repo / "docs" / "tasklist.md"
    content = tasklist_path.read_text()
    content = content.replace("- [ ]", "- [x]", 1)
    tasklist_path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)
    subprocess.run(
        ["git", "commit", "-m", "Stub: complete task"],
        cwd=repo,
        capture_output=True,
        check=False,
    )


# Stub CLI binary script for Part B (run as a subprocess, acts as the 'claude' binary).
# Routes responses based on CLI flags and prompt content.
#   --json-schema with "HALT" enum  → sanity check OK JSON
#   --json-schema without "HALT"    → reviewer APPROVED JSON
#   prompt starts with commit trigger → commit staged changes only (no git add -A)
#   otherwise                         → builder: tick docs/tasklist.md (tracked,
#                                       hardcoded), create + stage impl.py only;
#                                       leave the tick unstaged
#
# Remap correctness is proved by the orchestrator's own auto-commit logic, not by
# prompt parsing.  The builder always ticks docs/tasklist.md but does NOT stage
# the tick — only impl.py is staged.  After the commit step commits impl.py,
# delegate_commit() sees docs/tasklist.md as a remaining unstaged change and
# auto-commits it IFF self.tasklist == "docs/tasklist.md":
#
#   Remap OK:   tasklist == "docs/tasklist.md" → auto-commit fires → exit 0       ✓
#   Remap fail: tasklist == ".millstone/tasklist.md" → path mismatch →
#               commit failure → exit non-zero → assertion (a) catches it         ✓
_STUB_CLAUDE_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import sys, subprocess, os
    args = sys.argv[1:]

    # Handle version/availability checks (called by preflight_checks) without side effects.
    if "--version" in args or not any(a == "-p" for a in args):
        print("claude stub 0.0.1")
        sys.exit(0)

    prompt = ""
    for i, a in enumerate(args):
        if a == "-p" and i + 1 < len(args):
            prompt = args[i + 1]
            break
    schema_json = ""
    if "--json-schema" in args:
        idx = args.index("--json-schema") + 1
        schema_json = args[idx] if idx < len(args) else ""
    cwd = os.getcwd()
    if schema_json:
        if '"HALT"' in schema_json:
            print('{"status": "OK", "reason": ""}')
        else:
            print('{"status": "APPROVED", "review": "LGTM", "summary": "OK",'
                  ' "findings": [], "findings_by_severity":'
                  ' {"critical": [], "high": [], "medium": [], "low": [], "nit": []}}')
    elif "approved by the reviewer" in prompt[:500]:
        # Commit step: commit only the changes staged in the builder step (impl.py).
        # Do NOT git add -A — the docs/tasklist.md tick must remain unstaged so
        # delegate_commit()'s auto-commit logic can verify the tasklist path match.
        subprocess.run(
            ["git", "commit", "-m", "Stub task\\n\\nGenerated with millstone orchestrator"],
            cwd=cwd,
            capture_output=True,
        )
        print("Committed.")
    else:
        # Builder step: tick docs/tasklist.md (the git-tracked tasklist) but do NOT
        # stage the tick.  Stage only impl.py.  This leaves docs/tasklist.md as an
        # unstaged modification that delegate_commit() will auto-commit iff the
        # orchestrator's resolved tasklist path matches "docs/tasklist.md".
        docs_tl = os.path.join(cwd, "docs", "tasklist.md")
        if os.path.exists(docs_tl):
            txt = open(docs_tl).read()
            if "- [ ]" in txt:
                open(docs_tl, "w").write(txt.replace("- [ ]", "- [x]", 1))
        open(os.path.join(cwd, "impl.py"), "w").write("def stub(): pass\\n")
        subprocess.run(["git", "add", "impl.py"], cwd=cwd, capture_output=True)
        print("Done.")
""")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCommitTasklist:
    """commit_tasklist=True uses docs/tasklist.md and ticks it in a git commit."""

    def test_commit_tasklist_inner_loop(self, temp_repo: Path, stub_cli: StubCli) -> None:
        """Part A: Orchestrator(tasklist="docs/tasklist.md") reads from and ticks that path.

        Verifies:
          (a) docs/tasklist.md is ticked; .millstone/tasklist.md is left untouched
          (b) git show HEAD contains the tick (committed to the tracked path)
        """
        stub_cli.add(role="author", output="Done.", side_effect=_make_code_change_for_docs)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_commit_docs_tasklist_tick,
        )

        orch = Orchestrator(
            tasklist="docs/tasklist.md",
            max_tasks=1,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # (a) docs/tasklist.md was ticked; .millstone/tasklist.md was NOT
        docs_tl = (temp_repo / "docs" / "tasklist.md").read_text()
        assert "- [x]" in docs_tl, f"Expected '- [x]' in docs/tasklist.md:\n{docs_tl}"
        millstone_tl = (temp_repo / ".millstone" / "tasklist.md").read_text()
        assert "- [ ]" in millstone_tl, (
            "Expected .millstone/tasklist.md to still have unchecked tasks "
            "(it must not have been ticked)"
        )

        # (b) git show HEAD contains the tick
        result = subprocess.run(
            ["git", "show", "HEAD"],
            cwd=temp_repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "- [x]" in result.stdout, (
            f"Expected '- [x]' in git show HEAD:\n{result.stdout[:2000]}"
        )

    def test_commit_tasklist_cli_path(self, tmp_path: Path) -> None:
        """Part B: config.toml commit_tasklist=true drives docs/tasklist.md remap via main().

        Invokes millstone as a CLI subprocess so main() performs the
        config→tasklist path remap.  A stub 'claude' binary on PATH handles all
        agent calls without hitting real APIs.

        Remap is proven by repo state, not by prompt parsing.  The stub builder
        always ticks docs/tasklist.md (the only git-tracked tasklist) but stages
        only impl.py — the tick is left as an unstaged modification.  The commit
        step commits only staged changes.  delegate_commit() then auto-commits the
        tick iff self.tasklist == "docs/tasklist.md":
          - Remap OK:   auto-commit fires → exit 0, docs ticked               ✓
          - Remap fail: path mismatch → commit failure → exit non-zero →
                        assertion (a) catches the regression                   ✓

        docs/tasklist.md and .millstone/tasklist.md have distinct unchecked tasks
        so assertion (c) can verify the original default-path task is untouched.

        Verifies:
          (a) exit 0
          (b) docs/tasklist.md task is completed (- [x])
          (c) .millstone/tasklist.md retains its original unchecked task (untouched)
          (d) git show HEAD confirms the tick is committed
        """
        # Build a fresh tracked repo with docs/tasklist.md.
        # Distinct task titles in each file make outcome-only assertions meaningful.
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test.com"],
            ["git", "config", "user.name", "Test User"],
        ]:
            subprocess.run(cmd, cwd=repo_dir, capture_output=True)
        (repo_dir / ".gitignore").write_text("/.millstone/\n")
        (repo_dir / "docs").mkdir()
        (repo_dir / "docs" / "tasklist.md").write_text(
            "# Tasks\n\n- [ ] Docs task: implement the tracked feature\n"
        )
        subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_dir,
            capture_output=True,
        )

        # Write .millstone/config.toml with commit_tasklist = true.
        # The default tasklist has a distinct unchecked task so that:
        #   - if millstone fails to remap and reads .millstone/tasklist.md instead,
        #     the builder runs but ticks the wrong file, failing assertion (b); and
        #   - assertion (c) is a real contract (stub only touches git-tracked files;
        #     .millstone/ is gitignored so it can never be ticked).
        millstone_dir = repo_dir / ".millstone"
        millstone_dir.mkdir()
        (millstone_dir / "config.toml").write_text("commit_tasklist = true\n")
        (millstone_dir / "tasklist.md").write_text(
            "# Tasks\n\n- [ ] Default task: should remain untouched\n"
        )

        # Create stub 'claude' binary on a temp bin directory prepended to PATH.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub_binary = bin_dir / "claude"
        stub_binary.write_text(_STUB_CLAUDE_SCRIPT)
        stub_binary.chmod(stub_binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        try:
            result = subprocess.run(
                ["millstone"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            # env is a local copy; no parent-process PATH mutation to undo.
            # bin_dir and repo_dir are cleaned up by pytest's tmp_path fixture.
            pass

        # (a) run exits successfully
        assert result.returncode == 0, (
            f"millstone exited {result.returncode}\n"
            f"stdout: {result.stdout[:2000]}\n"
            f"stderr: {result.stderr[:2000]}"
        )

        # (b) docs task is completed — the remapped path was processed and ticked
        docs_tl = (repo_dir / "docs" / "tasklist.md").read_text()
        assert "- [x]" in docs_tl, (
            f"Expected docs/tasklist.md task to be completed (- [x]):\n{docs_tl}"
        )

        # (c) default tasklist is completely untouched — original unchecked task
        # must still be present and no [x] tick may appear.  If remap fails,
        # delegate_commit() returns failure (exit non-zero, caught by assertion (a))
        # and this file is never written by the stub, so both assertions hold.
        default_tl = (repo_dir / ".millstone" / "tasklist.md").read_text()
        assert "- [x]" not in default_tl, (
            f".millstone/tasklist.md must NOT be ticked:\n{default_tl}"
        )
        assert "- [ ] Default task: should remain untouched" in default_tl, (
            f".millstone/tasklist.md must retain the original unchecked task:\n{default_tl}"
        )

        # (d) git show HEAD confirms the tick is committed
        git_show = subprocess.run(
            ["git", "show", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert "- [x]" in git_show.stdout, (
            f"Expected '- [x]' in git show HEAD:\n{git_show.stdout[:2000]}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCommitDesigns:
    """commit_designs=True and commit_opportunities=True place artifacts at tracked paths."""

    def test_commit_designs_tracked_paths(self, empty_repo: Path, stub_cli: StubCli) -> None:
        """Artifacts land at committed (tracked) paths, not under .millstone/.

        Configure commit_designs=true and commit_opportunities=true via config.toml.
        Run analyze then design via stub_cli. Verify:
          (a) opportunities at repo_root/opportunities.md (not .millstone/)
          (b) design at repo_root/designs/<slug>.md (not .millstone/designs/)
        """
        # Write config.toml with commit flags before creating the Orchestrator so
        # load_config() picks them up and passes them to OuterLoopManager.
        (empty_repo / ".millstone" / "config.toml").write_text(
            "commit_designs = true\ncommit_opportunities = true\n"
        )

        def _write_tracked_opportunities(repo: Path) -> None:
            opp_file = repo / "opportunities.md"
            opp_file.write_text(
                "- [ ] **Add type annotations**\n"
                f"  - Opportunity ID: {_OPP_ID}\n"
                "  - Description: Add type annotations to core functions.\n"
                "  - ROI Score: 8.0\n"
            )

        def _write_tracked_design(repo: Path) -> None:
            designs_dir = repo / "designs"
            designs_dir.mkdir(parents=True, exist_ok=True)
            (designs_dir / f"{_DESIGN_ID}.md").write_text(
                f"# Add type annotations\n\n"
                f"- **design_id**: {_DESIGN_ID}\n"
                f"- **title**: Add type annotations\n"
                f"- **status**: draft\n"
                f"- **opportunity_ref**: {_OPP_ID}\n\n"
                f"---\n\n"
                f"Add type annotations to all public functions in the codebase.\n"
            )

        stub_cli.add(
            role="analyzer",
            output="Analysis complete.",
            side_effect=_write_tracked_opportunities,
        )
        stub_cli.add(role="reviewer", output=_ANALYZE_REVIEW_APPROVED)
        stub_cli.add(
            role="author",
            output="Design written.",
            side_effect=_write_tracked_design,
        )
        stub_cli.add(role="reviewer", output=_DESIGN_REVIEW_APPROVED)

        orch = Orchestrator(
            approve_opportunities=False,
            approve_designs=False,
            approve_plans=False,
            review_designs=False,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                analyze_result = orch.run_analyze()
                design_result = orch.run_design("Add type annotations", opportunity_id=_OPP_ID)
        finally:
            orch.cleanup()

        # run_analyze() succeeds only if the provider's configured path matches where
        # the stub wrote the file.  If commit_opportunities wiring regresses the
        # provider path to .millstone/opportunities.md, list_opportunities() returns
        # empty → success=False and this assertion catches the regression.
        assert analyze_result.get("success") is True, (
            "run_analyze() failed; the opportunity provider may be using the wrong path. "
            f"Result: {analyze_result}"
        )
        assert analyze_result.get("opportunities_file"), (
            "run_analyze() returned no opportunities_file path"
        )
        assert ".millstone" not in analyze_result["opportunities_file"], (
            "opportunities_file points inside .millstone/ when commit_opportunities=True; "
            f"got: {analyze_result['opportunities_file']}"
        )

        # run_design() succeeds only if the provider's configured path matches where
        # the stub wrote the design file.  If commit_designs wiring regresses to
        # .millstone/designs, list_designs() returns empty → success=False.
        assert design_result.get("success") is True, (
            "run_design() failed; the design provider may be using the wrong path. "
            f"Result: {design_result}"
        )
        assert design_result.get("design_file"), "run_design() returned no design_file path"
        assert ".millstone" not in design_result["design_file"], (
            "design_file points inside .millstone/ when commit_designs=True; "
            f"got: {design_result['design_file']}"
        )

        # Confirm artifact locations: tracked paths exist, .millstone/ paths absent.
        assert (empty_repo / "opportunities.md").exists(), (
            "opportunities.md must exist at repo root (tracked path) when commit_opportunities=True"
        )
        assert (empty_repo / "designs" / f"{_DESIGN_ID}.md").exists(), (
            f"Design file must exist at repo/designs/{_DESIGN_ID}.md (tracked path) when commit_designs=True"
        )
        assert not (empty_repo / ".millstone" / "opportunities.md").exists(), (
            "opportunities.md must NOT exist under .millstone/ when commit_opportunities=True"
        )
        assert not (empty_repo / ".millstone" / "designs" / f"{_DESIGN_ID}.md").exists(), (
            "Design file must NOT exist under .millstone/designs/ when commit_designs=True"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCustomPromptsDir:
    """--prompts-dir uses the custom template with standard substitutions intact."""

    def test_custom_prompts_dir(self, tmp_path: Path, temp_repo: Path, stub_cli: StubCli) -> None:
        """Custom tasklist_prompt.md is loaded and standard tokens are still resolved.

        Write a modified tasklist_prompt.md to a temp directory containing
        CUSTOM_MARKER_XYZ plus all standard provider tokens. Run inner loop via
        stub_cli. Assert:
          (a) captured builder prompt contains CUSTOM_MARKER_XYZ
          (b) str(temp_repo) appears in the prompt (WORKING_DIRECTORY resolved)
        """
        prompts_dir = tmp_path / "custom_prompts"
        # Seed the custom dir with the standard prompts so every load_prompt()
        # call resolves.  Then override tasklist_prompt.md with the custom template.
        from importlib.resources import files as _pkg_files

        pkg_prompts = Path(str(_pkg_files("millstone.prompts")))
        shutil.copytree(pkg_prompts, prompts_dir, dirs_exist_ok=True)

        # Custom template: includes the marker plus all standard provider tokens
        # so substitution logic is fully exercised.
        (prompts_dir / "tasklist_prompt.md").write_text(
            "CUSTOM_MARKER_XYZ\n"
            "Working directory: {{WORKING_DIRECTORY}}\n"
            "{{TASKLIST_READ_INSTRUCTIONS}}\n"
            "{{TASKLIST_COMPLETE_INSTRUCTIONS}}\n"
        )

        stub_cli.add(role="author", output="Done.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_commit_with_tick,
        )

        orch = Orchestrator(
            prompts_dir=prompts_dir,
            max_tasks=1,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, f"Expected exit 0, got {exit_code}"

        # The first author-role call is the builder prompt built from tasklist_prompt.md
        author_call = next(c for c in stub_cli.calls if c.role == "author")

        # (a) Custom marker is present — proves the custom template was loaded
        assert "CUSTOM_MARKER_XYZ" in author_call.prompt, (
            f"Expected CUSTOM_MARKER_XYZ in builder prompt; got:\n{author_call.prompt[:500]}"
        )

        # (b) WORKING_DIRECTORY token was resolved to the actual repo path
        assert str(temp_repo) in author_call.prompt, (
            f"Expected str(temp_repo)={str(temp_repo)!r} in builder prompt; "
            f"got:\n{author_call.prompt[:500]}"
        )

        # (c) Provider tokens were fully resolved — no raw placeholders remain
        assert "{{TASKLIST_READ_INSTRUCTIONS}}" not in author_call.prompt, (
            "{{TASKLIST_READ_INSTRUCTIONS}} was not resolved in the rendered prompt"
        )
        assert "{{TASKLIST_COMPLETE_INSTRUCTIONS}}" not in author_call.prompt, (
            "{{TASKLIST_COMPLETE_INSTRUCTIONS}} was not resolved in the rendered prompt"
        )

        # (d) Non-empty guidance was substituted for each token individually.
        # Retrieve the actual substituted values from the provider so this
        # assertion stays robust across prompt wording refactors.
        provider = orch._outer_loop_manager.tasklist_provider
        placeholders = provider.get_prompt_placeholders()
        read_instructions = placeholders.get("TASKLIST_READ_INSTRUCTIONS", "")
        complete_instructions = placeholders.get("TASKLIST_COMPLETE_INSTRUCTIONS", "")

        assert read_instructions, "TASKLIST_READ_INSTRUCTIONS resolved to empty string"
        assert complete_instructions, "TASKLIST_COMPLETE_INSTRUCTIONS resolved to empty string"
        assert read_instructions in author_call.prompt, (
            "Expected TASKLIST_READ_INSTRUCTIONS guidance in rendered prompt; "
            f"got:\n{author_call.prompt[:500]}"
        )
        assert complete_instructions in author_call.prompt, (
            "Expected TASKLIST_COMPLETE_INSTRUCTIONS guidance in rendered prompt; "
            f"got:\n{author_call.prompt[:500]}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvalOnCommit:
    """run_eval() writes JSON and eval_on_commit=True halts on regression."""

    def test_run_eval_writes_json(self, temp_repo: Path) -> None:
        """Part 1: run_eval() writes .millstone/evals/<timestamp>.json with _passed: true.

        Creates an Orchestrator with a passing custom eval script, calls run_eval()
        directly, and asserts:
          (a) .millstone/evals/<timestamp>.json was created
          (b) run_eval() returns a dict with _passed: true
          (c) the JSON file records test results (passed: 0, failed: 0)
        """
        import json

        orch = Orchestrator(
            eval_scripts=["python -c 'import sys; sys.exit(0)'"],
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            result = orch.run_eval()
        finally:
            orch.cleanup()

        # (a) At least one timestamped JSON file was created in .millstone/evals/
        evals_dir = temp_repo / ".millstone" / "evals"
        assert evals_dir.exists(), ".millstone/evals/ directory was not created"
        json_files = [f for f in evals_dir.glob("*.json") if f.name != "summary.json"]
        assert json_files, "No timestamped JSON eval file found under .millstone/evals/"

        # (b) The return value has _passed: true (custom script exited 0, no test failures)
        assert result.get("_passed") is True, (
            f"Expected _passed: true in run_eval() return; got: {result.get('_passed')!r}"
        )

        # (c) The JSON file on disk records the eval (timestamp, tests, custom_scripts)
        data = json.loads(json_files[0].read_text())
        assert "timestamp" in data, "JSON file missing 'timestamp' field"
        assert "custom_scripts" in data, "JSON file missing 'custom_scripts' field"
        custom = data["custom_scripts"]
        assert len(custom) == 1, f"Expected 1 custom script result; got {len(custom)}"
        assert custom[0]["exit_code"] == 0, (
            f"Expected custom script exit_code 0; got {custom[0]['exit_code']}"
        )

    def test_eval_on_commit_regression_halts(self, temp_repo: Path, stub_cli: StubCli) -> None:
        """Part 2: eval_on_commit=True exits 1 when post-commit eval introduces new failures.

        The baseline is captured before the builder runs (no tests exist yet).
        The author side_effect writes a failing pytest test and stages it.
        The builder commits everything. The post-commit eval detects a new test
        failure and halts with exit 1.

        eval_scripts=["python -c 'import sys; sys.exit(1)'"] is included as
        configured; because the script fails in both baseline and post-commit evals
        it does not trigger a regression on its own. The exit 1 observable behavior
        comes from the new pytest failure detected by _run_eval_on_commit.
        """

        def _write_failing_test(repo: Path) -> None:
            """Write a failing pytest test and stage it (not yet committed)."""
            tests_dir = repo / "tests"
            tests_dir.mkdir(exist_ok=True)
            (tests_dir / "test_regression.py").write_text(
                "def test_always_fails():\n"
                "    assert False, 'intentional regression for eval_on_commit test'\n"
            )
            subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=False)

        stub_cli.add(role="author", output="Done.", side_effect=_write_failing_test)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_commit_with_tick,
        )

        orch = Orchestrator(
            eval_on_commit=True,
            auto_rollback=True,  # avoid interactive stdin prompt during test
            eval_scripts=["python -c 'import sys; sys.exit(1)'"],
            max_tasks=1,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 1, f"Expected exit 1 (eval regression halts the run), got {exit_code}"

    def test_eval_on_commit_passing_does_not_halt(self, temp_repo: Path, stub_cli: StubCli) -> None:
        """Part 3: eval_on_commit=True exits 0 when post-commit eval passes.

        The baseline is captured before the builder runs (no failing tests).
        The author side_effect makes a normal code change and stages it.
        The builder commits everything. The post-commit eval passes (no regressions)
        and the run exits 0.
        """
        stub_cli.add(role="author", output="Done.", side_effect=_make_code_change)
        stub_cli.add(role="sanity", output=_SANITY_OK_JSON)
        stub_cli.add(role="reviewer", output=_APPROVED_JSON)
        stub_cli.add(
            role="builder",
            output="Committed.",
            side_effect=_commit_with_tick,
        )

        orch = Orchestrator(
            eval_on_commit=True,
            auto_rollback=True,
            eval_scripts=["python -c 'import sys; sys.exit(0)'"],
            max_tasks=1,
            task_constraints={
                "require_tests": False,
                "require_criteria": False,
                "require_risk": False,
                "require_context": False,
            },
        )
        try:
            with stub_cli.patch(orch):
                exit_code = orch.run()
        finally:
            orch.cleanup()

        assert exit_code == 0, (
            f"Expected exit 0 (passing eval does not halt the run), got {exit_code}"
        )

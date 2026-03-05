import re

from millstone.artifacts.tasklist import TasklistManager


class TestTaskIds:
    def test_parse_task_id_explicit(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        meta = mgr._parse_task_metadata("**Title**: desc\n  - ID: my-task\n  - Risk: low\n")
        assert meta["task_id"] == "my-task"

    def test_parse_task_id_html_comment(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        meta = mgr._parse_task_metadata("**Title**: desc\n  <!-- id: my-task -->\n")
        assert meta["task_id"] == "my-task"

    def test_parse_task_id_explicit_wins_over_html_comment(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        meta = mgr._parse_task_metadata(
            "**Title**: desc\n  <!-- id: comment-id -->\n  - ID: explicit-id\n"
        )
        assert meta["task_id"] == "explicit-id"

    def test_generate_task_id_stable(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tid1 = mgr.generate_task_id("**Title**: desc\n  - Risk: low\n")
        tid2 = mgr.generate_task_id("  **Title**:  desc\n\n  -   Risk: low\n")
        assert tid1 == tid2
        assert re.fullmatch(r"[0-9a-f]{8}", tid1)

    def test_generate_task_id_collision_suffix(self, temp_repo, monkeypatch):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [ ] **A**: one\n- [ ] **B**: two\n")

        monkeypatch.setattr(mgr, "generate_task_id", lambda _t: "abc12345")
        tasks = mgr.extract_all_task_ids()
        assert [t["task_id"] for t in tasks] == ["abc12345", "abc12345-1"]

    def test_mark_complete_by_id_found(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text(
            "# Tasklist\n\n"
            "- [ ] **Task One**: first\n"
            "  - ID: task-one\n"
            "- [ ] **Task Two**: second\n"
            "  - ID: task-two\n"
        )

        assert mgr.mark_task_complete_by_id("task-two", taskmap={}) is True
        content = tasklist.read_text()
        assert "- [ ] **Task One**" in content
        assert "- [x] **Task Two**" in content

    def test_mark_complete_by_id_not_found(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [ ] **A**: one\n")
        before = tasklist.read_text()
        assert mgr.mark_task_complete_by_id("nope", taskmap={}) is False
        after = tasklist.read_text()
        assert before == after


class TestTaskGroups:
    def test_extract_all_task_groups(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text(
            "# Tasklist\n\n"
            "- [ ] Prelude task\n"
            "## Group: Frontend\n"
            "- [ ] Task A\n"
            "- [ ] Task B\n"
            "## Group: Backend\n"
            "- [ ] Task C\n"
        )
        groups = mgr.extract_all_task_groups()
        assert groups == {0: None, 1: "Frontend", 2: "Frontend", 3: "Backend"}

    def test_extract_all_task_groups_no_headers(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [ ] A\n- [ ] B\n- [ ] C\n")
        groups = mgr.extract_all_task_groups()
        assert groups == {0: None, 1: None, 2: None}


class TestAcceptanceCriteria:
    def test_extract_criteria_basic(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = (
            "**Add retry logic**: To the HTTP client\n"
            "  **Acceptance criteria:**\n"
            "  - Retries up to 3 times on 5xx responses\n"
            "  - Uses exponential backoff with jitter\n"
        )
        result = mgr._extract_acceptance_criteria(task_text)
        assert result == [
            "Retries up to 3 times on 5xx responses",
            "Uses exponential backoff with jitter",
        ]

    def test_extract_criteria_absent(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = "**Simple task**: No criteria here\n  - Risk: low\n"
        result = mgr._extract_acceptance_criteria(task_text)
        assert result == []

    def test_extract_criteria_case_insensitive_header(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = "**Task**: description\n  **Acceptance Criteria:**\n  - Item one\n"
        result = mgr._extract_acceptance_criteria(task_text)
        assert result == ["Item one"]

    def test_extract_criteria_fully_lowercase_header(self, temp_repo):
        """Fully lowercase header must be recognised (case-insensitive match)."""
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = "**Task**: description\n  **acceptance criteria:**\n  - Item one\n"
        result = mgr._extract_acceptance_criteria(task_text)
        assert result == ["Item one"]

    def test_extract_criteria_stops_before_metadata_bullets(self, temp_repo):
        """Metadata bullets (Tests:, Risk:, etc.) must NOT be captured as criteria."""
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = (
            "**Task**: description\n"
            "  **Acceptance criteria:**\n"
            "  - Criterion A\n"
            "  - Criterion B\n"
            "  - Tests: test_foo.py\n"
            "  - Risk: low\n"
        )
        result = mgr._extract_acceptance_criteria(task_text)
        assert result == ["Criterion A", "Criterion B"]

    def test_extract_criteria_stops_at_blank_line(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = (
            "**Task**: description\n"
            "  **Acceptance criteria:**\n"
            "  - Criterion A\n"
            "\n"
            "  - Should not be included\n"
        )
        result = mgr._extract_acceptance_criteria(task_text)
        assert result == ["Criterion A"]

    def test_parse_task_metadata_includes_acceptance_criteria(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        task_text = (
            "**Add retry logic**: To the HTTP client\n"
            "  - Risk: low\n"
            "  **Acceptance criteria:**\n"
            "  - Retries on 5xx\n"
            "  - Backoff with jitter\n"
        )
        meta = mgr._parse_task_metadata(task_text)
        assert meta["acceptance_criteria"] == ["Retries on 5xx", "Backoff with jitter"]

    def test_extract_current_task_acceptance_criteria(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text(
            "# Tasklist\n\n"
            "- [ ] **Add retry**: desc\n"
            "  **Acceptance criteria:**\n"
            "  - Retries 3 times\n"
            "  - Backoff applied\n"
            "- [ ] **Other task**: second\n"
        )
        result = mgr.extract_current_task_acceptance_criteria()
        assert result == ["Retries 3 times", "Backoff applied"]

    def test_extract_current_task_no_criteria(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        tasklist = temp_repo / "docs" / "tasklist.md"
        tasklist.write_text("# Tasklist\n\n- [ ] **Plain task**: no criteria\n")
        result = mgr.extract_current_task_acceptance_criteria()
        assert result == []

    def test_extract_current_task_no_file(self, temp_repo):
        mgr = TasklistManager(repo_dir=temp_repo)
        result = mgr.extract_current_task_acceptance_criteria()
        assert result == []

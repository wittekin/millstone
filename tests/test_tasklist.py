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

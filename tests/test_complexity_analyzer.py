from unittest.mock import MagicMock, patch

from millstone.runtime.orchestrator import Orchestrator


def test_analyze_task_complexity():
    # Mock dependencies
    with (
        patch("millstone.runtime.orchestrator.load_config") as mock_load_config,
        patch("millstone.runtime.orchestrator.load_project_config"),
        patch("millstone.runtime.orchestrator.load_policy"),
        patch("millstone.runtime.orchestrator.TasklistManager"),
        patch("millstone.runtime.orchestrator.ContextManager"),
        patch("millstone.runtime.orchestrator.EvalManager"),
        patch("millstone.runtime.orchestrator.OuterLoopManager"),
        patch("millstone.runtime.orchestrator.InnerLoopManager"),
    ):
        # Mock config to enable model selection
        mock_load_config.return_value = {
            "model_selection": {"enabled": True},
            "category_weights": {},
            "category_thresholds": {},
            "task_constraints": {},
            "risk_settings": {},
        }

        orch = Orchestrator(tasklist="docs/tasklist.md")
        # Mock methods used in _analyze_task_complexity
        orch.work_dir = MagicMock()
        orch.repo_dir = MagicMock()
        orch.log = MagicMock()
        orch.run_agent = MagicMock()
        orch.load_prompt = MagicMock(return_value="Prompt {{TASK}} {{FILES}}")
        orch._extract_file_refs = MagicMock(return_value=["file1.py"])
        orch.get_task_context_file_content = MagicMock(return_value=None)
        orch._task_prefix = MagicMock(return_value="[Task 1/1]")

        # Mock run_agent response
        orch.run_agent.return_value = '{"complexity": "simple", "reasoning": "Simple change"}'

        # Test
        result = orch._analyze_task_complexity("Fix a bug")

        # Verify
        assert result["complexity"] == "simple"
        assert result["reasoning"] == "Simple change"
        orch.run_agent.assert_called_once()
        orch.log.assert_called_with(
            "task_complexity_analysis", complexity="simple", reasoning="Simple change"
        )


def test_analyze_task_complexity_disabled():
    with (
        patch("millstone.runtime.orchestrator.load_config") as mock_load_config,
        patch("millstone.runtime.orchestrator.load_project_config"),
        patch("millstone.runtime.orchestrator.load_policy"),
        patch("millstone.runtime.orchestrator.TasklistManager"),
        patch("millstone.runtime.orchestrator.ContextManager"),
        patch("millstone.runtime.orchestrator.EvalManager"),
        patch("millstone.runtime.orchestrator.OuterLoopManager"),
        patch("millstone.runtime.orchestrator.InnerLoopManager"),
    ):
        # Mock config to disable model selection
        mock_load_config.return_value = {
            "model_selection": {"enabled": False},
            "category_weights": {},
            "category_thresholds": {},
            "task_constraints": {},
            "risk_settings": {},
        }

        orch = Orchestrator(tasklist="docs/tasklist.md")
        orch._extract_file_refs = MagicMock()

        result = orch._analyze_task_complexity("Fix a bug")

        assert result == {}
        # Should not proceed to extraction if disabled
        orch._extract_file_refs.assert_not_called()

from importlib.resources import files

PROMPT_WORD_LIMITS = {
    "tasklist_prompt.md": 360,
    "task_prompt.md": 220,
    "review_prompt.md": 420,
    "analyze_prompt.md": 360,
    "analyze_review_prompt.md": 240,
    "analyze_fix_prompt.md": 140,
    "design_prompt.md": 250,
    "design_fix_prompt.md": 100,
    "review_design_prompt.md": 260,
    "plan_prompt.md": 460,
    "plan_review_prompt.md": 260,
    "plan_fix_prompt.md": 120,
    "sanity_check_impl.md": 180,
    "sanity_check_review.md": 220,
    "commit_prompt.md": 110,
}

DEV_CENTRIC_ROLE_PHRASES = [
    "software engineer",
    "software architect",
    "principal architect",
    "technical lead",
    "autonomous developer",
    "codebase audit",
]


def _prompt_text(name: str) -> str:
    return files("millstone.prompts").joinpath(name).read_text()


def test_core_prompts_stay_terse():
    """Core prompts should be compact enough that repo-specific context can dominate."""
    for name, limit in PROMPT_WORD_LIMITS.items():
        content = _prompt_text(name)
        words = len(content.split())
        assert words <= limit, f"{name} is too long: {words} words > {limit}"


def test_core_prompts_use_domain_neutral_roles():
    """Prompts should avoid assuming millstone is only for software engineering work."""
    for name in PROMPT_WORD_LIMITS:
        content = _prompt_text(name).lower()
        for phrase in DEV_CENTRIC_ROLE_PHRASES:
            assert phrase not in content, f"{name} should not contain {phrase!r}"

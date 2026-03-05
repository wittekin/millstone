"""Canonical loop definitions used by millstone."""

from millstone.loops.types import (
    AgentRole,
    ArtifactDisposition,
    ArtifactSource,
    ArtifactType,
    ContextMode,
    ContextRequirement,
    ContextType,
    DecisionType,
    LoopDefinition,
    MechanicalCheck,
    QualityGate,
    StateAction,
    Transition,
    TransitionCondition,
)

A = ContextMode.AMBIENT
INJECTED = ContextMode.INJECTED
C = ContextRequirement
T = ContextType

DEV_REVIEW_LOOP = LoopDefinition(
    id="dev.review",
    name="Developer Review Loop",
    description="Builder implements task, reviewer evaluates, iterate until approved",
    function="dev",
    roles=[
        AgentRole(
            id="author",
            name="Builder",
            input_artifacts=[ArtifactType.TASKLIST],
            input_context=[],
            guidance_prompt="prompts/tasklist_prompt.md",
            output_type=ArtifactType.DIFF,
            context_requirements=[
                C(T.CODEBASE, A),
                C(T.PRIOR_FEEDBACK, INJECTED, source="review.decision.feedback"),
            ],
        ),
        AgentRole(
            id="reviewer",
            name="Reviewer",
            input_artifacts=[ArtifactType.DIFF],
            input_context=[],
            guidance_prompt="prompts/review_prompt.md",
            output_type=ArtifactType.DECISION,
            output_schema="review_decision",
            context_requirements=[
                C(T.CODEBASE, A),
                C(T.REVIEW_GUIDELINES, A),
            ],
        ),
    ],
    checks=[
        MechanicalCheck(
            id="loc_threshold",
            name="Lines of Code Threshold",
            description="Halt if too many lines changed",
            check_type="loc_threshold",
            threshold=1000,
        ),
        MechanicalCheck(
            id="sensitive_files",
            name="Sensitive File Detection",
            description="Halt if credentials files modified",
            check_type="pattern_match",
            patterns=[".env", "credentials", "secret", ".pem", ".key"],
        ),
    ],
    initial_state="build",
    transitions=[
        Transition("build", TransitionCondition.always(), "check"),
        Transition("check", TransitionCondition.verdict(DecisionType.APPROVED), "review"),
        Transition("check", TransitionCondition.verdict(DecisionType.BLOCKED), "halted"),
        Transition("review", TransitionCondition.verdict(DecisionType.APPROVED), "commit"),
        Transition(
            "review",
            TransitionCondition.verdict(DecisionType.REQUEST_CHANGES),
            "fix",
            max_iterations=3,
        ),
        Transition("fix", TransitionCondition.always(), "check"),
        Transition("commit", TransitionCondition.always(), "done"),
    ],
    state_actions=[
        StateAction("build", "author", [ArtifactType.TASKLIST], [ArtifactType.DIFF]),
        StateAction(
            "check",
            "author",
            [ArtifactType.DIFF],
            [],
            ["loc_threshold", "sensitive_files"],
        ),
        StateAction("review", "reviewer", [ArtifactType.DIFF], [ArtifactType.DECISION]),
        StateAction("fix", "author", [ArtifactType.FEEDBACK], [ArtifactType.DIFF]),
        StateAction("commit", "author", [ArtifactType.DIFF], [ArtifactType.COMMIT]),
    ],
    input_sources=[
        ArtifactSource(ArtifactType.TASKLIST, "docs/tasklist.md"),
    ],
    output_dispositions=[
        ArtifactDisposition(ArtifactType.COMMIT, "commit", "git"),
    ],
    quality_gates=[
        QualityGate("review", {"require_explicit": True, "patterns": ["LGTM", "approved"]}),
    ],
    produces=["dev.commit_to_deploy", "dev.docs_to_support"],
    consumes=["support.escalation_to_dev", "qa.regression_to_dev"],
    capability_tier="C1_local_write",
)

LOOP_REGISTRY = {
    DEV_REVIEW_LOOP.id: DEV_REVIEW_LOOP,
}


__all__ = ["DEV_REVIEW_LOOP", "LOOP_REGISTRY"]

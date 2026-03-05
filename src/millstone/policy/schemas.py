# JSON Schema definitions used for structured CLI agent responses.
"""
Structured Output Schemas for Agent Communication

Defines JSON schemas that enforce typed responses from CLI agents (Claude Code, Codex, Gemini).
These schemas are passed to the CLI via --json-schema (Claude), --output-schema (Codex),
or injected into the prompt (Gemini) to ensure reliable, parseable signals from agents to the orchestrator.

Usage:
    from millstone.policy.schemas import REVIEW_DECISION_SCHEMA, get_schema_json

    # Get schema as JSON string for CLI flag
    schema_json = get_schema_json("review_decision")

    # Parse and validate response
    decision = parse_review_decision(agent_output)
"""

import contextlib
import json
from dataclasses import dataclass
from enum import Enum


class ReviewStatus(str, Enum):
    """Review decision status."""

    APPROVED = "APPROVED"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class FindingSeverity(str, Enum):
    """Review finding severity level."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NIT = "nit"


class SanityStatus(str, Enum):
    """Sanity check status."""

    OK = "OK"
    HALT = "HALT"


class DesignReviewVerdict(str, Enum):
    """Design review verdict."""

    APPROVED = "APPROVED"
    NEEDS_REVISION = "NEEDS_REVISION"


# =============================================================================
# JSON Schemas
# =============================================================================
# These are JSON Schema definitions passed to CLI agents to enforce structure.
#
# Structured Output Requirements (OpenAI-compatible):
# 1. ALL objects must have "additionalProperties": false
# 2. ALL objects with "properties" must have "required" listing EVERY property key
# 3. No if/then/else conditionals
# See: https://platform.openai.com/docs/guides/structured-outputs

REVIEW_DECISION_SCHEMA = {
    # Note: Avoid JSON Schema conditionals (if/then/else) - not supported by all providers
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["APPROVED", "REQUEST_CHANGES"],
            "description": "The review decision",
        },
        "review": {"type": "string", "description": "Full review content (free-form text)"},
        "summary": {"type": "string", "description": "Brief summary of the review"},
        "findings": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "List of issues that must be addressed (include when REQUEST_CHANGES)",
        },
        "findings_by_severity": {
            "type": ["object", "null"],
            "properties": {
                "critical": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Critical issues that block merge",
                },
                "high": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "High priority issues",
                },
                "medium": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Medium priority issues",
                },
                "low": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Low priority issues",
                },
                "nit": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Nitpicks and style suggestions",
                },
            },
            "required": ["critical", "high", "medium", "low", "nit"],
            "additionalProperties": False,
            "description": "Findings grouped by severity level",
        },
    },
    "required": ["status", "review", "summary", "findings", "findings_by_severity"],
    "additionalProperties": False,
}

SANITY_CHECK_SCHEMA = {
    # Note: Avoid JSON Schema conditionals (if/then/else) - not supported by all providers
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["OK", "HALT"],
            "description": "Whether to proceed (OK) or halt for human intervention (HALT)",
        },
        "reason": {
            "type": "string",
            "description": "Explanation of why halting (include when HALT)",
        },
    },
    "required": ["status", "reason"],
    "additionalProperties": False,
}

BUILDER_COMPLETION_SCHEMA = {
    # Note: Avoid JSON Schema conditionals (if/then/else) - not supported by all providers
    "type": "object",
    "properties": {
        "completed": {"type": "boolean", "description": "Whether the task was completed"},
        "summary": {"type": "string", "description": "Brief summary of what was done"},
        "files_changed": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of files that were modified",
        },
    },
    "required": ["completed", "summary", "files_changed"],
    "additionalProperties": False,
}

DESIGN_REVIEW_SCHEMA = {
    # Note: Avoid JSON Schema conditionals (if/then/else) - not supported by all providers
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["APPROVED", "NEEDS_REVISION"],
            "description": "The review verdict",
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of design strengths",
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of issues that must be addressed",
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Clarifying questions for the author",
        },
    },
    "required": ["verdict", "strengths", "issues", "questions"],
    "additionalProperties": False,
}

# Registry of all schemas
SCHEMAS = {
    "review_decision": REVIEW_DECISION_SCHEMA,
    "sanity_check": SANITY_CHECK_SCHEMA,
    "builder_completion": BUILDER_COMPLETION_SCHEMA,
    "design_review": DESIGN_REVIEW_SCHEMA,
}


# =============================================================================
# Schema Utilities
# =============================================================================


def get_schema_json(schema_name: str) -> str:
    """Get a schema as a JSON string for CLI flags.

    Args:
        schema_name: One of "review_decision", "sanity_check", "builder_completion",
            "design_review"

    Returns:
        JSON string representation of the schema.

    Raises:
        ValueError: If schema_name is not recognized.
    """
    if schema_name not in SCHEMAS:
        available = ", ".join(SCHEMAS.keys())
        raise ValueError(f"Unknown schema: {schema_name}. Available: {available}")
    return json.dumps(SCHEMAS[schema_name], separators=(",", ":"))


def get_schema_path(schema_name: str, work_dir: str) -> str:
    """Write schema to a file and return the path.

    Some CLIs (like Codex) require a file path rather than inline JSON.
    This writes the schema to the work directory and returns the path.

    Args:
        schema_name: One of "review_decision", "sanity_check", "builder_completion",
            "design_review"
        work_dir: Path to .millstone work directory

    Returns:
        Path to the schema file.
    """
    from pathlib import Path

    schema_dir = Path(work_dir) / "schemas"
    schema_dir.mkdir(exist_ok=True)

    schema_file = schema_dir / f"{schema_name}.json"
    schema_file.write_text(json.dumps(SCHEMAS[schema_name], indent=2))

    return str(schema_file)


# =============================================================================
# Response Dataclasses
# =============================================================================


@dataclass
class ReviewDecision:
    """Parsed review decision from agent."""

    status: ReviewStatus
    review: str | None = None
    findings: list[str] | None = None
    findings_by_severity: dict[str, list[str]] | None = None
    summary: str | None = None

    @property
    def is_approved(self) -> bool:
        return self.status == ReviewStatus.APPROVED

    @property
    def findings_count(self) -> int:
        """Total count of all findings."""
        count = 0
        if self.findings:
            count += len(self.findings)
        if self.findings_by_severity:
            for severity_findings in self.findings_by_severity.values():
                count += len(severity_findings)
        return count

    def get_severity_counts(self) -> dict[str, int]:
        """Get count of findings by severity level."""
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0}
        if self.findings_by_severity:
            for severity, findings in self.findings_by_severity.items():
                if severity in counts:
                    counts[severity] = len(findings)
        return counts


@dataclass
class SanityResult:
    """Parsed sanity check result from agent."""

    status: SanityStatus
    reason: str | None = None

    @property
    def should_halt(self) -> bool:
        return self.status == SanityStatus.HALT


@dataclass
class BuilderCompletion:
    """Parsed builder completion signal from agent."""

    completed: bool
    summary: str
    files_changed: list[str] | None = None


@dataclass
class DesignReviewResult:
    """Parsed design review result from agent."""

    verdict: DesignReviewVerdict
    strengths: list[str]
    issues: list[str]
    questions: list[str] | None = None

    @property
    def is_approved(self) -> bool:
        return self.verdict == DesignReviewVerdict.APPROVED


# =============================================================================
# Parsing Functions
# =============================================================================


def parse_review_decision(output: str) -> ReviewDecision | None:
    """Parse review decision from agent output.

    Attempts to extract JSON from the output and parse it as a ReviewDecision.
    Falls back to regex-based parsing for compatibility with agents that don't
    follow the schema exactly.

    Args:
        output: Raw agent output string

    Returns:
        ReviewDecision if parsed successfully, None otherwise.
    """
    import re

    # Try to find JSON in code blocks first (more reliable for nested objects)
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
    if code_block_match:
        try:
            data = json.loads(code_block_match.group(1))
            if "status" in data and data["status"] in ("APPROVED", "REQUEST_CHANGES"):
                if "review" not in data or "summary" not in data:
                    return None
                return ReviewDecision(
                    status=ReviewStatus(data["status"]),
                    review=data.get("review"),
                    findings=data.get("findings"),
                    findings_by_severity=data.get("findings_by_severity"),
                    summary=data.get("summary"),
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Try to extract JSON from output (handles nested objects with brace matching)
    # Find all { and match to closing }
    brace_positions = []
    depth = 0
    start = -1
    for i, char in enumerate(output):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                brace_positions.append((start, i + 1))
                start = -1

    # Try each JSON object found
    for start, end in brace_positions:
        try:
            data = json.loads(output[start:end])
            if "status" in data and data["status"] in ("APPROVED", "REQUEST_CHANGES"):
                if "review" not in data or "summary" not in data:
                    return None
                return ReviewDecision(
                    status=ReviewStatus(data["status"]),
                    review=data.get("review"),
                    findings=data.get("findings"),
                    findings_by_severity=data.get("findings_by_severity"),
                    summary=data.get("summary"),
                )
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    # Fallback: look for status patterns in plain text, but require review+summary
    has_review_summary = re.search(r'"review"\s*:\s*"', output, re.IGNORECASE) and re.search(
        r'"summary"\s*:\s*"', output, re.IGNORECASE
    )
    if re.search(r'"status"\s*:\s*"APPROVED"', output, re.IGNORECASE) and has_review_summary:
        return ReviewDecision(status=ReviewStatus.APPROVED)
    if re.search(r'"status"\s*:\s*"REQUEST_CHANGES"', output, re.IGNORECASE) and has_review_summary:
        # Try to extract findings
        findings_match = re.search(r'"findings"\s*:\s*\[(.*?)\]', output, re.DOTALL)
        findings = None
        if findings_match:
            with contextlib.suppress(json.JSONDecodeError):
                findings = json.loads(f"[{findings_match.group(1)}]")
        return ReviewDecision(status=ReviewStatus.REQUEST_CHANGES, findings=findings)

    return None


def parse_sanity_result(output: str) -> SanityResult | None:
    """Parse sanity check result from agent output.

    Args:
        output: Raw agent output string

    Returns:
        SanityResult if parsed successfully, None otherwise.
    """
    import re

    # Try to extract JSON
    json_match = re.search(r'\{[^{}]*"status"\s*:\s*"(OK|HALT)"[^{}]*\}', output, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            return SanityResult(
                status=SanityStatus(data["status"]),
                reason=data.get("reason"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Fallback: Check for HALT signals
    if re.search(r'"status"\s*:\s*"HALT"', output, re.IGNORECASE):
        reason_match = re.search(r'"reason"\s*:\s*"([^"]+)"', output)
        return SanityResult(
            status=SanityStatus.HALT, reason=reason_match.group(1) if reason_match else None
        )

    # Default to OK if no halt signal found
    return SanityResult(status=SanityStatus.OK)


def parse_builder_completion(output: str) -> BuilderCompletion | None:
    """Parse builder completion signal from agent output.

    Args:
        output: Raw agent output string

    Returns:
        BuilderCompletion if parsed successfully, None otherwise.
    """
    import re

    # Try to extract JSON
    json_match = re.search(
        r'\{[^{}]*"completed"\s*:\s*(true|false)[^{}]*\}', output, re.DOTALL | re.IGNORECASE
    )
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            return BuilderCompletion(
                completed=data["completed"],
                summary=data.get("summary", ""),
                files_changed=data.get("files_changed"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    return None


def parse_design_review(output: str) -> DesignReviewResult | None:
    """Parse design review result from agent output.

    Attempts to extract JSON from the output and parse it as a DesignReviewResult.
    The JSON must contain verdict, strengths, and issues fields.

    Args:
        output: Raw agent output string

    Returns:
        DesignReviewResult if parsed successfully, None otherwise.
    """
    import re

    if not output:
        return None

    # Try to extract JSON from output - look for a JSON block with verdict
    # Use a more permissive pattern that can match nested arrays
    json_match = re.search(
        r'\{[^{}]*"verdict"\s*:\s*"(APPROVED|NEEDS_REVISION)"[^{}]*'
        r'"strengths"\s*:\s*\[[^\]]*\][^{}]*'
        r'"issues"\s*:\s*\[[^\]]*\][^{}]*\}',
        output,
        re.DOTALL,
    )

    if json_match:
        try:
            data = json.loads(json_match.group(0))
            return DesignReviewResult(
                verdict=DesignReviewVerdict(data["verdict"]),
                strengths=data.get("strengths", []),
                issues=data.get("issues", []),
                questions=data.get("questions"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # Try a more flexible approach: find any JSON object with the right fields
    # This handles cases where fields may be in different order
    try:
        # Look for JSON code block
        code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
        if code_block_match:
            data = json.loads(code_block_match.group(1))
            if "verdict" in data and "strengths" in data and "issues" in data:
                verdict_str = data["verdict"]
                if verdict_str in ("APPROVED", "NEEDS_REVISION"):
                    return DesignReviewResult(
                        verdict=DesignReviewVerdict(verdict_str),
                        strengths=data.get("strengths", []),
                        issues=data.get("issues", []),
                        questions=data.get("questions"),
                    )
    except (json.JSONDecodeError, KeyError, ValueError):
        pass

    # Fallback: Look for verdict in plain text and construct minimal result
    if '"verdict"' in output:
        if '"APPROVED"' in output:
            return DesignReviewResult(
                verdict=DesignReviewVerdict.APPROVED,
                strengths=[],
                issues=[],
            )
        elif '"NEEDS_REVISION"' in output:
            return DesignReviewResult(
                verdict=DesignReviewVerdict.NEEDS_REVISION,
                strengths=[],
                issues=[],
            )

    return None

"""File-backed artifact provider implementations for millstone.

Contains FileOpportunityProvider, FileDesignProvider, FileTasklistProvider.
"""

import datetime
import re
from pathlib import Path
from typing import Any

from millstone.artifact_providers.base import (
    DesignProviderBase,
    OpportunityProviderBase,
    TasklistProviderBase,
)
from millstone.artifact_providers.registry import (
    register_design_provider_class,
    register_opportunity_provider_class,
    register_tasklist_provider_class,
)
from millstone.artifacts.models import (
    Design,
    DesignStatus,
    Opportunity,
    OpportunityStatus,
    TasklistItem,
    TaskStatus,
)
from millstone.artifacts.tasklist import TasklistManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _title_slug(title: str) -> str:
    """Derive a URL-safe slug from title.

    Lowercase, replace non-alphanumeric/non-hyphen chars with hyphens,
    collapse consecutive hyphens, strip leading/trailing hyphens.
    """
    s = title.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _normalize_meta_key(key: str) -> str:
    """Normalize a metadata key: lowercase, spaces/hyphens → underscores."""
    return key.lower().strip().replace(" ", "_").replace("-", "_")


# Normalized key spellings that map to Opportunity.design_ref
_DESIGN_REF_KEYS = {"design_ref", "design_reference"}


def _meta_get(meta: dict[str, str], *keys: str) -> str | None:
    """Look up keys in meta dict case-insensitively; return first match or None."""
    lower = {k.lower(): v for k, v in meta.items()}
    for key in keys:
        val = lower.get(key.lower())
        if val is not None:
            return val
    return None


# ---------------------------------------------------------------------------
# FileOpportunityProvider
# ---------------------------------------------------------------------------


class FileOpportunityProvider(OpportunityProviderBase):
    """File-backed OpportunityProvider reading/writing opportunities.md.

    Supports dual-format parsing:
    - Primary: checklist format (- [ ] / - [x]) per .millstone/opportunities.md
    - Legacy fallback: ### heading format
    """

    def __init__(self, path) -> None:
        self.path = Path(path)

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> "FileOpportunityProvider":
        path = options.get("path")
        if not path:
            raise ValueError("FileOpportunityProvider requires config option: path")
        return cls(Path(path))

    # ------------------------------------------------------------------
    # OpportunityProvider interface
    # ------------------------------------------------------------------

    def list_opportunities(self) -> list[Opportunity]:
        if not self.path.exists():
            return []
        content = self.path.read_text()
        if not content.strip():
            return []
        checklist = self._parse_checklist(content)
        if checklist:
            return checklist
        return self._parse_legacy(content)

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        for opp in self.list_opportunities():
            if opp.opportunity_id == opportunity_id:
                return opp
        return None

    def write_opportunity(self, opportunity: Opportunity) -> None:
        """Append opportunity in canonical checklist format."""
        opportunity.validate()
        block = self._to_checklist_block(opportunity)
        if self.path.exists():
            existing = self.path.read_text()
            if existing and not existing.endswith("\n"):
                existing += "\n"
            sep = "\n" if existing.strip() else ""
            self.path.write_text(existing + sep + block + "\n")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(block + "\n")

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        """Mutate checkbox and Status: metadata in-place."""
        if not self.path.exists():
            raise FileNotFoundError(f"opportunities file not found: {self.path}")
        content = self.path.read_text()
        lines = content.splitlines(keepends=True)
        i = 0
        while i < len(lines):
            line = lines[i].rstrip("\n").rstrip("\r")
            m = re.match(r"^- \[([ x])\] (.+)", line)
            if m:
                raw_title = m.group(2).strip()
                title = re.sub(r"^\*\*(.+)\*\*$", r"\1", raw_title)

                # Collect block extent (indented lines + blank lines)
                block_start = i
                j = i + 1
                while j < len(lines):
                    bl = lines[j].rstrip("\n").rstrip("\r")
                    if bl.startswith("  ") or bl == "":
                        j += 1
                    else:
                        break
                block_end = j

                # Parse entry id from block metadata
                meta = self._parse_meta_lines(lines, block_start + 1, block_end)
                entry_id = _meta_get(meta, "Opportunity ID", "ID") or _title_slug(title)

                if entry_id == opportunity_id:
                    self._apply_status_to_block(lines, block_start, block_end, status)
                    self.path.write_text("".join(lines))
                    return

                i = block_end
            else:
                i += 1
        raise KeyError(f"opportunity_id not found in {self.path}: {opportunity_id!r}")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_checklist(self, content: str) -> list[Opportunity]:
        """Parse primary checklist format (- [ ] / - [x] entries)."""
        lines = content.splitlines()
        results = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^- \[([ x])\] (.+)", line)
            if m:
                checked = m.group(1) == "x"
                raw_title = m.group(2).strip()
                title = re.sub(r"^\*\*(.+)\*\*$", r"\1", raw_title)

                # Collect indented metadata lines and blank separators
                block_lines = [line]
                j = i + 1
                while j < len(lines):
                    bl = lines[j]
                    if bl.startswith("  ") or bl == "":
                        block_lines.append(bl)
                        j += 1
                    else:
                        break

                # Parse key: value metadata
                meta: dict[str, str] = {}
                for bl in block_lines[1:]:
                    bm = re.match(r"^\s+- (.+?):\s*(.*)", bl)
                    if bm:
                        raw_key = bm.group(1).strip()
                        meta[raw_key.strip("*").strip()] = bm.group(2).strip()

                # Status: checkbox provides base; explicit Status: metadata overrides
                base_status = OpportunityStatus.adopted if checked else OpportunityStatus.identified
                status_val = _meta_get(meta, "Status")
                if status_val:
                    sv = status_val.lower()
                    if sv == "rejected":
                        status = OpportunityStatus.rejected
                    elif sv == "adopted":
                        status = OpportunityStatus.adopted
                    else:
                        status = base_status
                else:
                    status = base_status

                opp_id = _meta_get(meta, "Opportunity ID", "ID") or _title_slug(title)
                description = _meta_get(meta, "Description") or ""

                requires_design: bool | None = None
                rd_val = _meta_get(meta, "Requires Design")
                if rd_val is not None:
                    requires_design = rd_val.lower() == "true"

                design_ref: str | None = None
                for k, v in meta.items():
                    if _normalize_meta_key(k) in _DESIGN_REF_KEYS:
                        design_ref = v
                        break

                roi_score: float | None = None
                roi_raw = _meta_get(meta, "ROI Score")
                if roi_raw is not None:
                    try:
                        roi_score = float(roi_raw)
                    except ValueError:
                        roi_score = None

                raw = "\n".join(block_lines)
                results.append(
                    Opportunity(
                        opportunity_id=opp_id,
                        title=title,
                        status=status,
                        description=description,
                        requires_design=requires_design,
                        design_ref=design_ref,
                        roi_score=roi_score,
                        raw=raw,
                    )
                )
                i = j
            else:
                i += 1
        return results

    def _parse_legacy(self, content: str) -> list[Opportunity]:
        """Parse legacy ### heading format."""
        lines = content.splitlines()
        results = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^### (.+)", line)
            if m:
                title = m.group(1).strip()
                block_lines = [line]
                j = i + 1
                while j < len(lines) and not re.match(r"^### ", lines[j]):
                    block_lines.append(lines[j])
                    j += 1

                # Parse "Key: value" lines from block body
                meta: dict[str, str] = {}
                for bl in block_lines[1:]:
                    bm = re.match(r"^([\w][\w ]*?):\s*(.*)", bl.strip())
                    if bm:
                        meta[bm.group(1).strip()] = bm.group(2).strip()

                description = _meta_get(meta, "Description") or ""
                roi_score: float | None = None
                roi_raw = _meta_get(meta, "ROI Score")
                if roi_raw is None:
                    for bl in block_lines[1:]:
                        rm = re.match(r"^\*\*ROI Score\*\*:\s*(.*)", bl.strip())
                        if rm:
                            roi_raw = rm.group(1).strip()
                            break
                if roi_raw is not None:
                    try:
                        roi_score = float(roi_raw)
                    except ValueError:
                        roi_score = None

                opp_id = _title_slug(title)
                raw = "\n".join(block_lines)
                results.append(
                    Opportunity(
                        opportunity_id=opp_id,
                        title=title,
                        status=OpportunityStatus.identified,
                        description=description,
                        roi_score=roi_score,
                        raw=raw,
                    )
                )
                i = j
            else:
                i += 1
        return results

    def _parse_meta_lines(self, lines: list[str], start: int, end: int) -> dict[str, str]:
        """Extract key: value metadata from a slice of lines (with keepends)."""
        meta: dict[str, str] = {}
        for k in range(start, end):
            bl = lines[k].rstrip("\n").rstrip("\r")
            bm = re.match(r"^\s+- (.+?):\s*(.*)", bl)
            if bm:
                meta[bm.group(1).strip()] = bm.group(2).strip()
        return meta

    def _apply_status_to_block(
        self,
        lines: list[str],
        block_start: int,
        block_end: int,
        status: OpportunityStatus,
    ) -> None:
        """Mutate lines in-place: update checkbox and Status: metadata."""
        checkbox_char = "x" if status == OpportunityStatus.adopted else " "
        line = lines[block_start]
        end_char = "\n" if line.endswith("\n") else ""
        lines[block_start] = (
            re.sub(r"\[([ x])\]", f"[{checkbox_char}]", line.rstrip("\n").rstrip("\r")) + end_char
        )

        # Find existing Status: metadata line within block
        status_line_idx: int | None = None
        for k in range(block_start + 1, block_end):
            bl = lines[k].rstrip("\n").rstrip("\r")
            if re.match(r"^\s+- Status:\s*", bl, re.IGNORECASE):
                status_line_idx = k
                break

        if status == OpportunityStatus.rejected:
            if status_line_idx is not None:
                end_char = "\n" if lines[status_line_idx].endswith("\n") else ""
                lines[status_line_idx] = "  - Status: rejected" + end_char
            else:
                lines.insert(block_start + 1, "  - Status: rejected\n")
        else:
            # identified or adopted: remove any Status: line
            if status_line_idx is not None:
                lines.pop(status_line_idx)

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return placeholder instructions referencing this provider's file path."""
        return {
            "OPPORTUNITY_WRITE_INSTRUCTIONS": f"Write your findings to `{self.path}`.",
            "OPPORTUNITY_READ_INSTRUCTIONS": f"Read opportunities from `{self.path}`.",
        }

    def _to_checklist_block(self, opportunity: Opportunity) -> str:
        """Serialize an Opportunity to canonical checklist markdown block."""
        checkbox = "[x]" if opportunity.status == OpportunityStatus.adopted else "[ ]"
        parts = [f"- {checkbox} **{opportunity.title}**"]
        parts.append(f"  - Opportunity ID: {opportunity.opportunity_id}")
        if opportunity.status == OpportunityStatus.rejected:
            parts.append("  - Status: rejected")
        if opportunity.requires_design is not None:
            val = "true" if opportunity.requires_design else "false"
            parts.append(f"  - Requires Design: {val}")
        if opportunity.description:
            parts.append(f"  - Description: {opportunity.description}")
        if opportunity.design_ref:
            parts.append(f"  - Design Ref: {opportunity.design_ref}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# FileDesignProvider
# ---------------------------------------------------------------------------


class FileDesignProvider(DesignProviderBase):
    """File-backed DesignProvider reading/writing files under a configured directory.

    Supports dual-format parsing:
    - Primary: canonical metadata block (- **field**: value lines before ---).
    - Legacy fallback: # Design: Title heading + Status: Value line.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> "FileDesignProvider":
        path = options.get("path")
        if not path:
            raise ValueError("FileDesignProvider requires config option: path")
        return cls(Path(path))

    # ------------------------------------------------------------------
    # DesignProvider interface
    # ------------------------------------------------------------------

    def list_designs(self) -> list[Design]:
        if not self.path.exists():
            return []
        results = []
        for f in sorted(self.path.glob("*.md")):
            design = self._parse_file(f)
            if design is not None:
                results.append(design)
        return results

    def get_design(self, design_id: str) -> Design | None:
        if not self.path.exists():
            return None
        f = self.path / f"{design_id}.md"
        if not f.exists():
            return None
        return self._parse_file(f)

    def write_design(self, design: Design) -> None:
        design.validate()
        self.path.mkdir(parents=True, exist_ok=True)
        f = self.path / f"{design.design_id}.md"
        f.write_text(self._to_canonical(design))

    def update_design_status(self, design_id: str, status: DesignStatus) -> None:
        """Rewrite the status line in-place; body is untouched."""
        f = self.path / f"{design_id}.md"
        if not f.exists():
            raise FileNotFoundError(f"design file not found: {f}")
        content = f.read_text()

        # Canonical format: - **status**: <value>
        canonical_pat = re.compile(r"^(- \*\*status\*\*: )(.+)$", re.MULTILINE)
        if canonical_pat.search(content):
            f.write_text(canonical_pat.sub(rf"\g<1>{status.value}", content))
            return

        # Legacy format: Status: <value>
        legacy_pat = re.compile(r"^(Status: )(.+)$", re.MULTILINE)
        if legacy_pat.search(content):
            f.write_text(legacy_pat.sub(rf"\g<1>{status.value}", content))
            return

        raise ValueError(f"no status line found in design file: {f}")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_file(self, f: Path) -> Design | None:
        content = f.read_text()
        design = self._parse_canonical(f.stem, content)
        if design is None:
            design = self._parse_legacy(f.stem, content)
        return design

    def _parse_canonical(self, stem: str, content: str) -> Design | None:
        """Parse canonical metadata-block format (- **field**: value before ---)."""
        sep_match = re.search(r"^---\s*$", content, re.MULTILINE)
        if not sep_match:
            return None

        header = content[: sep_match.start()]
        body = content[sep_match.end() :].lstrip("\n")

        meta: dict[str, str] = {}
        for line in header.splitlines():
            m = re.match(r"^- \*\*(\w+)\*\*:\s*(.*)", line)
            if m:
                meta[m.group(1).lower()] = m.group(2).strip()

        # Require at least one recognized canonical metadata field
        if not meta:
            return None

        design_id = meta.get("design_id", stem)
        title = meta.get("title", stem)
        status_str = meta.get("status", "draft").lower()
        try:
            status = DesignStatus(status_str)
        except ValueError:
            status = DesignStatus.draft

        opportunity_ref = meta.get("opportunity_ref") or None

        design = Design(
            design_id=design_id,
            title=title,
            status=status,
            body=body,
            opportunity_ref=opportunity_ref,
        )
        design.review_summary = meta.get("review_summary") or None
        design.tasklist_ref = meta.get("tasklist_ref") or None
        return design

    def _parse_legacy(self, stem: str, content: str) -> Design | None:
        """Parse legacy # Design: Title + Status: Value format."""
        title = stem
        status = DesignStatus.draft

        for line in content.splitlines():
            m = re.match(r"^# Design:\s*(.+)", line)
            if m:
                title = m.group(1).strip()
                continue
            m = re.match(r"^Status:\s*(.+)", line, re.IGNORECASE)
            if m:
                status_str = m.group(1).strip().lower()
                try:
                    status = DesignStatus(status_str)
                except ValueError:
                    status = DesignStatus.draft

        return Design(
            design_id=stem,
            title=title,
            status=status,
            body=content,
            opportunity_ref=None,
        )

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return placeholder instructions referencing this provider's designs directory."""
        return {
            "DESIGN_WRITE_INSTRUCTIONS": (
                f"Write the design to `{self.path}/{{slug}}.md`. "
                "If the file already exists (e.g. revising from feedback), edit it in place."
            ),
            "DESIGN_READ_INSTRUCTIONS": f"Read the design from `{self.path}`.",
        }

    def _to_canonical(self, design: Design) -> str:
        """Serialize Design to canonical metadata block format."""
        lines = [
            f"# {design.title}",
            "",
            f"- **design_id**: {design.design_id}",
            f"- **title**: {design.title}",
            f"- **status**: {design.status.value}",
        ]
        if design.opportunity_ref:
            lines.append(f"- **opportunity_ref**: {design.opportunity_ref}")
        if design.review_summary is not None:
            lines.append(f"- **review_summary**: {design.review_summary}")
        if design.tasklist_ref is not None:
            lines.append(f"- **tasklist_ref**: {design.tasklist_ref}")
        lines.append(f"- **created**: {datetime.date.today().isoformat()}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(design.body)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# FileTasklistProvider
# ---------------------------------------------------------------------------


class FileTasklistProvider(TasklistProviderBase):
    """File-backed TasklistProvider wrapping TasklistManager.

    Reads/writes a markdown tasklist file with - [ ] / - [x] entries.
    Binary completion semantics are preserved: only todo→done is supported
    via TasklistManager; in_progress and blocked raise NotImplementedError.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        # TasklistManager expects repo_dir / tasklist; map full path via parent+name.
        self._mgr = TasklistManager(
            repo_dir=self.path.parent,
            tasklist=self.path.name,
        )

    @classmethod
    def from_config(cls, options: dict[str, Any]) -> "FileTasklistProvider":
        path = options.get("path")
        if not path:
            raise ValueError("FileTasklistProvider requires config option: path")
        return cls(Path(path))

    # ------------------------------------------------------------------
    # TasklistProvider interface
    # ------------------------------------------------------------------

    def list_tasks(self) -> list[TasklistItem]:
        if not self.path.exists():
            return []
        content = self.path.read_text()
        if not content.strip():
            return []
        return self._parse_tasks(content)

    def get_task(self, task_id: str) -> TasklistItem | None:
        for item in self.list_tasks():
            if item.task_id == task_id:
                return item
        return None

    def append_tasks(self, tasks: list[TasklistItem]) -> None:
        """Serialize TasklistItem objects to checklist markdown and append to file."""
        if not tasks:
            return
        for task in tasks:
            task.validate()
        blocks = [self._to_checklist_block(t) for t in tasks]
        new_text = "\n\n".join(blocks) + "\n"
        if self.path.exists():
            existing = self.path.read_text()
            if existing and not existing.endswith("\n"):
                existing += "\n"
            sep = "\n" if existing.strip() else ""
            self.path.write_text(existing + sep + new_text)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(new_text)

    def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        """Update task status; only todo→done is supported via TasklistManager."""
        if status == TaskStatus.in_progress:
            raise NotImplementedError(
                "FileTasklistProvider does not support in_progress status: "
                "tasklist format uses binary completion semantics (todo/done only)."
            )
        if status == TaskStatus.blocked:
            raise NotImplementedError(
                "FileTasklistProvider does not support blocked status: "
                "tasklist format uses binary completion semantics (todo/done only)."
            )
        if status == TaskStatus.done:
            self._mgr.mark_task_complete_by_id(task_id=task_id, taskmap={})

    def get_snapshot(self) -> str:
        """Return full tasklist content for provider-agnostic rollback."""
        if not self.path.exists():
            raise FileNotFoundError(f"tasklist file not found: {self.path}")
        return self.path.read_text()

    def restore_snapshot(self, content: str) -> None:
        """Restore full tasklist content from a previous snapshot."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content)

    def get_prompt_placeholders(self) -> dict[str, str]:
        """Return placeholder instructions referencing this provider's file path."""
        return {
            "TASKLIST_READ_INSTRUCTIONS": (
                f"Read tasks from `{self.path}`. Tasks use markdown checkboxes: "
                "`- [ ]` pending, `- [x]` complete. Select the FIRST unchecked task."
            ),
            "TASKLIST_COMPLETE_INSTRUCTIONS": (
                f"Mark exactly this one task complete by changing its `- [ ]` to "
                f"`- [x]` in `{self.path}` and stop. Do not modify any other tasks."
            ),
            "TASKLIST_REWRITE_INSTRUCTIONS": (
                f"Write the entire compacted content back to `{self.path}`, "
                "replacing the file completely."
            ),
            "TASKLIST_APPEND_INSTRUCTIONS": f"Append new tasks to `{self.path}`.",
            "TASKLIST_UPDATE_INSTRUCTIONS": (
                f"Edit the existing tasks in `{self.path}` in place. "
                "Do not re-append or duplicate them."
            ),
        }

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_tasks(self, content: str) -> list[TasklistItem]:
        """Parse all tasks from tasklist content into TasklistItem records."""
        task_records = self._mgr.extract_all_task_ids()
        results = []
        for rec in task_records:
            checked: bool = rec["checked"]
            raw_text: str = rec["raw_text"]
            task_id: str = rec["task_id"]
            metadata = self._mgr._parse_task_metadata(raw_text)
            title = metadata.get("title") or raw_text.split("\n")[0].strip()
            status = TaskStatus.done if checked else TaskStatus.todo
            results.append(
                TasklistItem(
                    task_id=task_id,
                    title=title,
                    status=status,
                    design_ref=metadata.get("design_ref"),
                    opportunity_ref=metadata.get("opportunity_ref"),
                    risk=metadata.get("risk"),
                    tests=metadata.get("tests"),
                    context=metadata.get("context"),
                    criteria=metadata.get("criteria"),
                    raw=raw_text,
                )
            )
        return results

    def _to_checklist_block(self, item: TasklistItem) -> str:
        """Serialize a TasklistItem to canonical checklist markdown block."""
        checkbox = "[x]" if item.status == TaskStatus.done else "[ ]"
        parts = [f"- {checkbox} **{item.title}**"]
        parts.append(f"  - ID: {item.task_id}")
        if item.design_ref:
            parts.append(f"  - design-ref: {item.design_ref}")
        if item.opportunity_ref:
            parts.append(f"  - opportunity-ref: {item.opportunity_ref}")
        if item.risk:
            parts.append(f"  - Risk: {item.risk}")
        if item.tests:
            parts.append(f"  - Tests: {item.tests}")
        if item.context:
            parts.append(f"  - Context: {item.context}")
        if item.criteria:
            parts.append(f"  - Acceptance: {item.criteria}")
        return "\n".join(parts)


register_opportunity_provider_class("file", FileOpportunityProvider)
register_design_provider_class("file", FileDesignProvider)
register_tasklist_provider_class("file", FileTasklistProvider)

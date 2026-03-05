"""Tests for file-backed artifact providers (file-opportunity-provider task).

TDD: tests written before implementation to confirm red → green.
"""

import pytest

# ---------------------------------------------------------------------------
# Checklist format parsing
# ---------------------------------------------------------------------------

class TestFileOpportunityProviderChecklistFormat:
    """list_opportunities() parses checklist format correctly."""

    def test_identified_from_unchecked_box(self, tmp_path):
        """- [ ] maps to OpportunityStatus.identified."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Improve abstraction**\n"
            "  - Opportunity ID: improve-abstraction\n"
            "  - Status: identified\n"
            "  - Description: Add provider interfaces.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert len(opps) == 1
        assert opps[0].status == OpportunityStatus.identified
        assert opps[0].opportunity_id == "improve-abstraction"
        assert opps[0].title == "Improve abstraction"

    def test_adopted_from_checked_box(self, tmp_path):
        """- [x] maps to OpportunityStatus.adopted."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [x] **Improve abstraction**\n"
            "  - Opportunity ID: improve-abstraction\n"
            "  - Description: Add provider interfaces.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert len(opps) == 1
        assert opps[0].status == OpportunityStatus.adopted

    def test_rejected_from_status_metadata(self, tmp_path):
        """- [ ] + Status: rejected metadata maps to OpportunityStatus.rejected."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Old Idea**\n"
            "  - Opportunity ID: old-idea\n"
            "  - Status: rejected\n"
            "  - Description: Not worth it.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert len(opps) == 1
        assert opps[0].status == OpportunityStatus.rejected

    def test_explicit_opportunity_id_takes_precedence(self, tmp_path):
        """Explicit Opportunity ID field takes precedence over title slug."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Some Title With Spaces**\n"
            "  - Opportunity ID: my-explicit-id\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].opportunity_id == "my-explicit-id"

    def test_id_field_variant_also_accepted(self, tmp_path):
        """ID: field (short form) is also accepted."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Short ID Form**\n"
            "  - ID: short-id-form\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].opportunity_id == "short-id-form"

    def test_title_slug_fallback_when_no_id_field(self, tmp_path):
        """When no explicit ID field, opportunity_id is derived from title slug."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Improve Performance Now**\n"
            "  - Description: Make it faster.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].opportunity_id == "improve-performance-now"

    def test_multiple_opportunities_parsed(self, tmp_path):
        """Multiple checklist entries are all parsed."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **First Opp**\n"
            "  - Opportunity ID: first-opp\n"
            "  - Description: desc1\n"
            "\n"
            "- [x] **Second Opp**\n"
            "  - Opportunity ID: second-opp\n"
            "  - Description: desc2\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert len(opps) == 2
        assert opps[0].opportunity_id == "first-opp"
        assert opps[1].opportunity_id == "second-opp"

    def test_description_field_parsed(self, tmp_path):
        """Description: metadata is populated on the record."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **An Opportunity**\n"
            "  - Opportunity ID: an-opportunity\n"
            "  - Description: This is the description.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].description == "This is the description."

    def test_requires_design_parsed(self, tmp_path):
        """Requires Design: true/false is parsed to bool."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Needs Design**\n"
            "  - Opportunity ID: needs-design\n"
            "  - Requires Design: true\n"
            "  - Description: desc\n"
            "\n"
            "- [ ] **No Design**\n"
            "  - Opportunity ID: no-design\n"
            "  - Requires Design: false\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].requires_design is True
        assert opps[1].requires_design is False


# ---------------------------------------------------------------------------
# design_ref key normalization
# ---------------------------------------------------------------------------

class TestDesignRefKeyNormalization:
    """design_ref is populated from accepted key variants."""

    def test_design_ref_key(self, tmp_path):
        """Design Ref: key maps to design_ref."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Opp**\n"
            "  - Opportunity ID: opp\n"
            "  - Description: desc\n"
            "  - Design Ref: my-design\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].design_ref == "my-design"

    def test_design_ref_snake_case_key(self, tmp_path):
        """design_ref: key maps to design_ref."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Opp**\n"
            "  - Opportunity ID: opp\n"
            "  - Description: desc\n"
            "  - design_ref: my-design\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].design_ref == "my-design"

    def test_design_reference_key(self, tmp_path):
        """Design Reference: key maps to design_ref."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Opp**\n"
            "  - Opportunity ID: opp\n"
            "  - Description: desc\n"
            "  - Design Reference: my-design\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].design_ref == "my-design"

    def test_design_ref_case_insensitive(self, tmp_path):
        """design_ref key matching is case-insensitive."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Opp**\n"
            "  - Opportunity ID: opp\n"
            "  - Description: desc\n"
            "  - DESIGN REF: my-design\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].design_ref == "my-design"


# ---------------------------------------------------------------------------
# ROI score parsing
# ---------------------------------------------------------------------------

class TestOpportunityRoiScoreParsing:
    """ROI score metadata parses for checklist and legacy formats."""

    def test_checklist_roi_score_parsed_from_bold_key(self, tmp_path):
        """Checklist parser supports **ROI Score** metadata key."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **ROI Opportunity**\n"
            "  - Opportunity ID: roi-opportunity\n"
            "  - **ROI Score**: 2.5\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].roi_score == 2.5

    def test_checklist_roi_score_parsed_from_plain_key(self, tmp_path):
        """Checklist parser supports non-bold ROI Score metadata key."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **ROI Opportunity**\n"
            "  - Opportunity ID: roi-opportunity\n"
            "  - ROI Score: 2.5\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].roi_score == 2.5

    def test_checklist_invalid_roi_score_treated_as_none(self, tmp_path):
        """Checklist parser treats invalid ROI score values as None."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **ROI Opportunity**\n"
            "  - Opportunity ID: roi-opportunity\n"
            "  - ROI Score: not-a-number\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].roi_score is None

    def test_checklist_roi_score_absent_defaults_to_none(self, tmp_path):
        """Checklist parser returns roi_score=None when metadata is absent."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **ROI Opportunity**\n"
            "  - Opportunity ID: roi-opportunity\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].roi_score is None


# ---------------------------------------------------------------------------
# Legacy ### heading format
# ---------------------------------------------------------------------------

class TestLegacyHeadingFormat:
    """list_opportunities() falls back to legacy ### heading format."""

    def test_legacy_heading_parsed(self, tmp_path):
        """### Title headings are parsed in legacy fallback mode."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "### Improve Performance\n"
            "\n"
            "ROI Score: 2.5\n"
            "Description: Make the system faster.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert len(opps) == 1
        assert opps[0].title == "Improve Performance"
        assert opps[0].status == OpportunityStatus.identified
        assert opps[0].opportunity_id == "improve-performance"
        assert opps[0].roi_score == 2.5

    def test_legacy_bold_roi_score_parsed(self, tmp_path):
        """Legacy parser supports bold **ROI Score** metadata key."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "### Improve Performance\n"
            "\n"
            "**ROI Score**: 1.67\n"
            "Description: Make the system faster.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].roi_score == 1.67

    def test_legacy_heading_title_slug_id(self, tmp_path):
        """Legacy ### heading derives opportunity_id from title slug."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "### Fix The Bug In The Auth Module\n"
            "\n"
            "Description: Critical fix needed.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert opps[0].opportunity_id == "fix-the-bug-in-the-auth-module"

    def test_legacy_multiple_headings(self, tmp_path):
        """Multiple ### headings are all parsed."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "### First Opportunity\n"
            "\n"
            "Description: First desc.\n"
            "\n"
            "### Second Opportunity\n"
            "\n"
            "Description: Second desc.\n"
        )
        provider = FileOpportunityProvider(f)
        opps = provider.list_opportunities()
        assert len(opps) == 2
        assert opps[0].title == "First Opportunity"
        assert opps[1].title == "Second Opportunity"


# ---------------------------------------------------------------------------
# get_opportunity
# ---------------------------------------------------------------------------

class TestGetOpportunity:
    """get_opportunity(id) resolves by opportunity_id."""

    def test_get_by_id(self, tmp_path):
        """get_opportunity returns the matching record."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **First**\n"
            "  - Opportunity ID: first\n"
            "  - Description: desc\n"
            "\n"
            "- [ ] **Second**\n"
            "  - Opportunity ID: second\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        result = provider.get_opportunity("second")
        assert result is not None
        assert result.opportunity_id == "second"
        assert result.title == "Second"

    def test_get_missing_returns_none(self, tmp_path):
        """get_opportunity returns None when id not found."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **An Opp**\n"
            "  - Opportunity ID: an-opp\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        result = provider.get_opportunity("not-exist")
        assert result is None

    def test_get_on_missing_file_returns_none(self, tmp_path):
        """get_opportunity returns None when the file does not exist."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        result = provider.get_opportunity("any-id")
        assert result is None


# ---------------------------------------------------------------------------
# write_opportunity
# ---------------------------------------------------------------------------

class TestWriteOpportunity:
    """write_opportunity emits canonical checklist format."""

    def test_write_creates_file_if_not_exists(self, tmp_path):
        """write_opportunity creates the file when it doesn't exist."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        opp = Opportunity(
            opportunity_id="new-opp",
            title="New Opportunity",
            status=OpportunityStatus.identified,
            description="A brand new opportunity.",
        )
        provider.write_opportunity(opp)
        assert f.exists()
        content = f.read_text()
        assert "- [ ]" in content
        assert "New Opportunity" in content
        assert "Opportunity ID: new-opp" in content

    def test_write_appends_to_existing(self, tmp_path):
        """write_opportunity appends to an existing file."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **Existing**\n"
            "  - Opportunity ID: existing\n"
            "  - Description: Already here.\n"
        )
        provider = FileOpportunityProvider(f)
        opp = Opportunity(
            opportunity_id="new-one",
            title="New One",
            status=OpportunityStatus.identified,
            description="Added later.",
        )
        provider.write_opportunity(opp)
        content = f.read_text()
        assert "existing" in content
        assert "new-one" in content

    def test_write_round_trip(self, tmp_path):
        """write_opportunity followed by list_opportunities returns the record."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        opp = Opportunity(
            opportunity_id="round-trip-opp",
            title="Round Trip",
            status=OpportunityStatus.identified,
            description="Full round trip test.",
            requires_design=True,
        )
        provider.write_opportunity(opp)
        opps = provider.list_opportunities()
        assert len(opps) == 1
        result = opps[0]
        assert result.opportunity_id == "round-trip-opp"
        assert result.title == "Round Trip"
        assert result.description == "Full round trip test."
        assert result.requires_design is True

    def test_write_canonical_format_structure(self, tmp_path):
        """write_opportunity uses canonical checklist format."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        opp = Opportunity(
            opportunity_id="canon-opp",
            title="Canonical Format",
            status=OpportunityStatus.identified,
            description="Testing canonical format.",
        )
        provider.write_opportunity(opp)
        content = f.read_text()
        assert "- [ ] **Canonical Format**" in content
        assert "  - Opportunity ID: canon-opp" in content
        assert "  - Description: Testing canonical format." in content

    def test_write_raises_on_invalid_opportunity_before_io(self, tmp_path):
        """Invalid Opportunity raises and does not create/write file."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import (
            ArtifactValidationError,
            Opportunity,
            OpportunityStatus,
        )

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        invalid = Opportunity(
            opportunity_id="",
            title="Invalid Opportunity",
            status=OpportunityStatus.identified,
            description="desc",
        )

        with pytest.raises(ArtifactValidationError):
            provider.write_opportunity(invalid)

        assert not f.exists()


# ---------------------------------------------------------------------------
# Checklist contract: status encoding invariants
# ---------------------------------------------------------------------------

class TestChecklistContractStatusEncoding:
    """_to_checklist_block() encodes status via checkbox only; Status: line only for rejected."""

    def test_identified_no_status_line(self, tmp_path):
        """write_opportunity(identified) produces [ ] with no Status: line."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        provider.write_opportunity(Opportunity(
            opportunity_id="ident-opp",
            title="Identified Opp",
            status=OpportunityStatus.identified,
            description="desc",
        ))
        content = f.read_text()
        assert "- [ ] **Identified Opp**" in content
        assert "Status:" not in content

    def test_adopted_no_status_line(self, tmp_path):
        """write_opportunity(adopted) produces [x] with no Status: line."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        provider.write_opportunity(Opportunity(
            opportunity_id="adopted-opp",
            title="Adopted Opp",
            status=OpportunityStatus.adopted,
            description="desc",
        ))
        content = f.read_text()
        assert "- [x] **Adopted Opp**" in content
        assert "Status:" not in content

    def test_rejected_has_status_line(self, tmp_path):
        """write_opportunity(rejected) produces [ ] with Status: rejected."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        provider.write_opportunity(Opportunity(
            opportunity_id="rejected-opp",
            title="Rejected Opp",
            status=OpportunityStatus.rejected,
            description="desc",
        ))
        content = f.read_text()
        assert "- [ ] **Rejected Opp**" in content
        assert "Status: rejected" in content

    def test_identified_roundtrip_without_status_line(self, tmp_path):
        """Round-trip write→list preserves identified status with no Status: line."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import Opportunity, OpportunityStatus

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        provider.write_opportunity(Opportunity(
            opportunity_id="rt-opp",
            title="Round Trip",
            status=OpportunityStatus.identified,
            description="desc",
        ))
        assert "Status:" not in f.read_text()
        result = provider.list_opportunities()[0]
        assert result.status == OpportunityStatus.identified


# ---------------------------------------------------------------------------
# update_opportunity_status
# ---------------------------------------------------------------------------

class TestUpdateOpportunityStatus:
    """update_opportunity_status mutates checkbox and Status: metadata correctly."""

    def test_identified_to_adopted_checks_box(self, tmp_path):
        """identified → adopted changes - [ ] to - [x]."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **An Opp**\n"
            "  - Opportunity ID: an-opp\n"
            "  - Status: identified\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        provider.update_opportunity_status("an-opp", OpportunityStatus.adopted)
        content = f.read_text()
        assert "- [x] **An Opp**" in content
        assert "- [ ] **An Opp**" not in content

    def test_adopted_to_identified_unchecks_box(self, tmp_path):
        """adopted → identified changes - [x] to - [ ]."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [x] **An Opp**\n"
            "  - Opportunity ID: an-opp\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        provider.update_opportunity_status("an-opp", OpportunityStatus.identified)
        content = f.read_text()
        assert "- [ ] **An Opp**" in content

    def test_identified_to_rejected_adds_status_line(self, tmp_path):
        """identified → rejected adds Status: rejected metadata."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **An Opp**\n"
            "  - Opportunity ID: an-opp\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        provider.update_opportunity_status("an-opp", OpportunityStatus.rejected)
        content = f.read_text()
        assert "- [ ] **An Opp**" in content
        assert "Status: rejected" in content

    def test_rejected_status_line_updated_on_status_change(self, tmp_path):
        """If Status: rejected exists, it is updated when transitioning away."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **An Opp**\n"
            "  - Opportunity ID: an-opp\n"
            "  - Status: rejected\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        provider.update_opportunity_status("an-opp", OpportunityStatus.identified)
        content = f.read_text()
        assert "Status: rejected" not in content

    def test_unknown_opportunity_id_raises_keyerror(self, tmp_path):
        """Unknown opportunity_id raises KeyError after scanning file."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifacts.models import OpportunityStatus

        f = tmp_path / "opportunities.md"
        f.write_text(
            "- [ ] **An Opp**\n"
            "  - Opportunity ID: an-opp\n"
            "  - Status: identified\n"
            "  - Description: desc\n"
        )
        provider = FileOpportunityProvider(f)
        with pytest.raises(
            KeyError,
            match=r"opportunity_id not found in .*: 'missing-opp'",
        ):
            provider.update_opportunity_status("missing-opp", OpportunityStatus.adopted)


# ---------------------------------------------------------------------------
# Edge cases: empty/missing file
# ---------------------------------------------------------------------------

class TestEmptyAndMissingFile:
    """Edge cases: missing file, empty file."""

    def test_list_on_missing_file_returns_empty(self, tmp_path):
        """list_opportunities returns empty list when file doesn't exist."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        assert provider.list_opportunities() == []

    def test_list_on_empty_file_returns_empty(self, tmp_path):
        """list_opportunities returns empty list for empty file."""
        from millstone.artifact_providers.file import FileOpportunityProvider

        f = tmp_path / "opportunities.md"
        f.write_text("")
        provider = FileOpportunityProvider(f)
        assert provider.list_opportunities() == []


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestOpportunityProviderProtocolConformance:
    """FileOpportunityProvider satisfies the OpportunityProvider Protocol."""

    def test_isinstance_check(self, tmp_path):
        """isinstance(FileOpportunityProvider(...), OpportunityProvider) is True."""
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifact_providers.protocols import OpportunityProvider

        f = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(f)
        assert isinstance(provider, OpportunityProvider)


# ---------------------------------------------------------------------------
# FileDesignProvider – canonical metadata-block format
# ---------------------------------------------------------------------------

class TestFileDesignProviderCanonicalFormat:
    """list_designs() parses canonical metadata-block format."""

    def test_canonical_design_parsed(self, tmp_path):
        """Canonical metadata block yields correct Design fields."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        (d / "my-design.md").write_text(
            "# My Design\n\n"
            "- **design_id**: my-design\n"
            "- **title**: My Design\n"
            "- **status**: draft\n"
            "- **opportunity_ref**: some-opportunity\n"
            "- **created**: 2026-03-01\n\n"
            "---\n\n"
            "## Body content\n\nHere is the body.\n"
        )
        provider = FileDesignProvider(d)
        designs = provider.list_designs()
        assert len(designs) == 1
        assert designs[0].design_id == "my-design"
        assert designs[0].title == "My Design"
        assert designs[0].status == DesignStatus.draft
        assert designs[0].opportunity_ref == "some-opportunity"
        assert "Body content" in designs[0].body

    def test_canonical_approved_status(self, tmp_path):
        """Canonical status: approved is parsed correctly."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        (d / "approved-design.md").write_text(
            "# Approved Design\n\n"
            "- **design_id**: approved-design\n"
            "- **title**: Approved Design\n"
            "- **status**: approved\n"
            "- **opportunity_ref**: some-opp\n"
            "- **created**: 2026-03-01\n\n"
            "---\n\n"
            "## Body\n"
        )
        provider = FileDesignProvider(d)
        designs = provider.list_designs()
        assert designs[0].status == DesignStatus.approved

    def test_multiple_designs_listed(self, tmp_path):
        """Multiple design files are all listed."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        for slug in ("design-a", "design-b"):
            (d / f"{slug}.md").write_text(
                f"# Design {slug.upper()}\n\n"
                f"- **design_id**: {slug}\n"
                f"- **title**: Design {slug.upper()}\n"
                "- **status**: draft\n"
                f"- **opportunity_ref**: opp-{slug[-1]}\n"
                "- **created**: 2026-03-01\n\n"
                "---\n\n"
                f"Body {slug}.\n"
            )
        provider = FileDesignProvider(d)
        designs = provider.list_designs()
        assert len(designs) == 2
        ids = {des.design_id for des in designs}
        assert "design-a" in ids
        assert "design-b" in ids


# ---------------------------------------------------------------------------
# FileDesignProvider – legacy format
# ---------------------------------------------------------------------------

class TestFileDesignProviderLegacyFormat:
    """list_designs() falls back to legacy # Design: Title + Status: Value format."""

    def test_legacy_design_parsed(self, tmp_path):
        """Legacy format yields title (stripped prefix), status, and None opportunity_ref."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        (d / "old-design.md").write_text(
            "# Design: Old Design Title\n\n"
            "Created: 2025-01-01\n"
            "Status: Draft\n\n"
            "## Problem Statement\n\nSomething was broken.\n"
        )
        provider = FileDesignProvider(d)
        designs = provider.list_designs()
        assert len(designs) == 1
        assert designs[0].design_id == "old-design"
        assert designs[0].title == "Old Design Title"
        assert designs[0].status == DesignStatus.draft
        assert designs[0].opportunity_ref is None

    def test_legacy_opportunity_ref_is_none(self, tmp_path):
        """Legacy files always have opportunity_ref = None."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        (d / "legacy.md").write_text(
            "# Design: Legacy\n\n"
            "Status: Draft\n\n"
            "## Body\n\nContent.\n"
        )
        provider = FileDesignProvider(d)
        designs = provider.list_designs()
        assert designs[0].opportunity_ref is None

    def test_legacy_status_reviewed(self, tmp_path):
        """Legacy Status: Reviewed parses to DesignStatus.reviewed."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        (d / "reviewed.md").write_text(
            "# Design: Reviewed\n\n"
            "Status: Reviewed\n\n"
            "## Body\n"
        )
        provider = FileDesignProvider(d)
        designs = provider.list_designs()
        assert designs[0].status == DesignStatus.reviewed


# ---------------------------------------------------------------------------
# FileDesignProvider – get_design
# ---------------------------------------------------------------------------

class TestFileDesignProviderGetDesign:
    """get_design(id) resolves by filename stem."""

    def test_get_by_id(self, tmp_path):
        """get_design returns the matching record by stem."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        (d / "my-design.md").write_text(
            "# My Design\n\n"
            "- **design_id**: my-design\n"
            "- **title**: My Design\n"
            "- **status**: draft\n"
            "- **opportunity_ref**: opp\n"
            "- **created**: 2026-03-01\n\n"
            "---\n\n"
            "Body.\n"
        )
        provider = FileDesignProvider(d)
        result = provider.get_design("my-design")
        assert result is not None
        assert result.design_id == "my-design"

    def test_get_missing_returns_none(self, tmp_path):
        """get_design returns None when no matching file."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        result = provider.get_design("not-exist")
        assert result is None

    def test_get_on_missing_dir_returns_none(self, tmp_path):
        """get_design returns None when designs directory does not exist."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        provider = FileDesignProvider(d)
        result = provider.get_design("any-id")
        assert result is None


# ---------------------------------------------------------------------------
# FileDesignProvider – write_design
# ---------------------------------------------------------------------------

class TestWriteDesign:
    """write_design always writes canonical metadata block format."""

    def test_write_creates_canonical_file(self, tmp_path):
        """write_design creates a file with canonical format fields."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import Design, DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        design = Design(
            design_id="new-design",
            title="New Design",
            status=DesignStatus.draft,
            body="## Section\n\nContent here.\n",
            opportunity_ref="some-opp",
        )
        provider.write_design(design)
        f = d / "new-design.md"
        assert f.exists()
        content = f.read_text()
        assert "- **design_id**: new-design" in content
        assert "- **title**: New Design" in content
        assert "- **status**: draft" in content
        assert "- **opportunity_ref**: some-opp" in content
        assert "---" in content
        assert "## Section" in content

    def test_write_round_trip(self, tmp_path):
        """write_design followed by get_design returns equivalent record."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import Design, DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        design = Design(
            design_id="round-trip",
            title="Round Trip Design",
            status=DesignStatus.reviewed,
            body="## Body\n\nSome content.\n",
            opportunity_ref="my-opp",
        )
        provider.write_design(design)
        result = provider.get_design("round-trip")
        assert result is not None
        assert result.design_id == "round-trip"
        assert result.title == "Round Trip Design"
        assert result.status == DesignStatus.reviewed
        assert result.opportunity_ref == "my-opp"
        assert "Some content." in result.body

    def test_write_emits_review_summary_and_tasklist_ref(self, tmp_path):
        """write_design emits review_summary/tasklist_ref when provided."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import Design, DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        design = Design(
            design_id="with-metadata",
            title="With Metadata",
            status=DesignStatus.reviewed,
            body="## Body\n\nSome content.\n",
            opportunity_ref="some-opp",
            review_summary="looks good",
            tasklist_ref="docs/tasklist.md",
        )
        provider.write_design(design)
        content = (d / "with-metadata.md").read_text()
        assert "- **review_summary**: looks good" in content
        assert "- **tasklist_ref**: docs/tasklist.md" in content

    def test_write_read_round_trip_preserves_review_summary_and_tasklist_ref(self, tmp_path):
        """write_design/get_design preserves review_summary/tasklist_ref values."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import Design, DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        design = Design(
            design_id="metadata-round-trip",
            title="Metadata Round Trip",
            status=DesignStatus.reviewed,
            body="## Body\n\nSome content.\n",
            opportunity_ref="some-opp",
            review_summary="looks good",
            tasklist_ref="docs/tasklist.md",
        )
        provider.write_design(design)
        result = provider.get_design("metadata-round-trip")
        assert result is not None
        assert result.review_summary == "looks good"
        assert result.tasklist_ref == "docs/tasklist.md"

    def test_write_omits_review_summary_and_tasklist_ref_when_none(self, tmp_path):
        """write_design omits review_summary/tasklist_ref when None."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import Design, DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        design = Design(
            design_id="without-metadata",
            title="Without Metadata",
            status=DesignStatus.draft,
            body="## Body\n\nSome content.\n",
            opportunity_ref="some-opp",
            review_summary=None,
            tasklist_ref=None,
        )
        provider.write_design(design)
        content = (d / "without-metadata.md").read_text()
        assert "**review_summary**" not in content
        assert "**tasklist_ref**" not in content

    def test_parse_old_canonical_without_metadata_defaults_to_none(self, tmp_path):
        """Canonical files without review_summary/tasklist_ref parse those fields as None."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        (d / "old-canonical.md").write_text(
            "# Old Canonical\n\n"
            "- **design_id**: old-canonical\n"
            "- **title**: Old Canonical\n"
            "- **status**: draft\n"
            "- **opportunity_ref**: some-opp\n"
            "- **created**: 2026-03-01\n\n"
            "---\n\n"
            "## Body\n\nSome content.\n"
        )
        provider = FileDesignProvider(d)
        result = provider.get_design("old-canonical")
        assert result is not None
        assert result.review_summary is None
        assert result.tasklist_ref is None

    def test_write_raises_on_invalid_design_before_io(self, tmp_path):
        """Invalid Design raises and does not create directory/file."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import ArtifactValidationError, Design, DesignStatus

        d = tmp_path / "designs"
        provider = FileDesignProvider(d)
        invalid = Design(
            design_id="invalid-design",
            title="Invalid Design",
            status=DesignStatus.draft,
            body="## Body",
            opportunity_ref=None,
        )

        with pytest.raises(ArtifactValidationError):
            provider.write_design(invalid)

        assert not d.exists()


# ---------------------------------------------------------------------------
# FileDesignProvider – update_design_status
# ---------------------------------------------------------------------------

class TestUpdateDesignStatus:
    """update_design_status rewrites only the status line, preserving body."""

    def test_canonical_status_update(self, tmp_path):
        """Canonical format: status line updated, body preserved."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        f = d / "my-design.md"
        f.write_text(
            "# My Design\n\n"
            "- **design_id**: my-design\n"
            "- **title**: My Design\n"
            "- **status**: draft\n"
            "- **opportunity_ref**: opp\n"
            "- **created**: 2026-03-01\n\n"
            "---\n\n"
            "## Body\n\nSome content.\n"
        )
        provider = FileDesignProvider(d)
        provider.update_design_status("my-design", DesignStatus.approved)
        updated = f.read_text()
        assert "- **status**: approved" in updated
        assert "- **status**: draft" not in updated
        assert "## Body" in updated
        assert "Some content." in updated

    def test_legacy_status_update(self, tmp_path):
        """Legacy format: Status: line updated, body preserved."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        f = d / "legacy.md"
        f.write_text(
            "# Design: Legacy\n\n"
            "Created: 2025-01-01\n"
            "Status: Draft\n\n"
            "## Problem Statement\n\nSomething was broken.\n"
        )
        provider = FileDesignProvider(d)
        provider.update_design_status("legacy", DesignStatus.reviewed)
        updated = f.read_text()
        assert "Status: reviewed" in updated
        assert "Status: Draft" not in updated
        assert "## Problem Statement" in updated
        assert "Something was broken." in updated

    def test_status_update_does_not_corrupt_body(self, tmp_path):
        """update_design_status leaves body byte-for-byte identical."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifacts.models import DesignStatus

        d = tmp_path / "designs"
        d.mkdir()
        original_body = "## Complex Body\n\nWith **markdown** content.\n\nAnd multiple paragraphs.\n"
        f = d / "design.md"
        f.write_text(
            "# Design\n\n"
            "- **design_id**: design\n"
            "- **title**: Design\n"
            "- **status**: draft\n"
            "- **opportunity_ref**: opp\n"
            "- **created**: 2026-03-01\n\n"
            "---\n\n" + original_body
        )
        provider = FileDesignProvider(d)
        provider.update_design_status("design", DesignStatus.approved)
        updated = f.read_text()
        assert original_body in updated


# ---------------------------------------------------------------------------
# FileDesignProvider – protocol conformance
# ---------------------------------------------------------------------------

class TestFileDesignProviderProtocolConformance:
    """FileDesignProvider satisfies the DesignProvider Protocol."""

    def test_isinstance_check(self, tmp_path):
        """isinstance(FileDesignProvider(...), DesignProvider) is True."""
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifact_providers.protocols import DesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        assert isinstance(provider, DesignProvider)


# ---------------------------------------------------------------------------
# FileDesignProvider – edge cases
# ---------------------------------------------------------------------------

class TestFileDesignProviderEdgeCases:
    """Edge cases: missing dir, empty dir."""

    def test_list_on_missing_dir_returns_empty(self, tmp_path):
        """list_designs returns empty list when designs dir does not exist."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        provider = FileDesignProvider(d)
        assert provider.list_designs() == []

    def test_list_on_empty_dir_returns_empty(self, tmp_path):
        """list_designs returns empty list for empty designs dir."""
        from millstone.artifact_providers.file import FileDesignProvider

        d = tmp_path / "designs"
        d.mkdir()
        provider = FileDesignProvider(d)
        assert provider.list_designs() == []


# ---------------------------------------------------------------------------
# FileTasklistProvider – list_tasks
# ---------------------------------------------------------------------------

class TestFileTasklistProviderListTasks:
    """list_tasks() returns TasklistItem records with correct status mapping."""

    def test_unchecked_task_is_todo(self, tmp_path):
        """- [ ] task maps to status=todo."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Implement feature**\n"
            "  - ID: implement-feature\n"
            "  - Risk: low\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.todo
        assert tasks[0].title == "Implement feature"

    def test_checked_task_is_done(self, tmp_path):
        """- [x] task maps to status=done."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [x] **Finished task**\n"
            "  - ID: finished-task\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == TaskStatus.done

    def test_mixed_tasks_returned(self, tmp_path):
        """Both done and todo tasks are returned in list order."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [x] **Done task**\n"
            "  - ID: done-task\n"
            "\n"
            "- [ ] **Todo task**\n"
            "  - ID: todo-task\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert len(tasks) == 2
        assert tasks[0].task_id == "done-task"
        assert tasks[0].status == TaskStatus.done
        assert tasks[1].task_id == "todo-task"
        assert tasks[1].status == TaskStatus.todo

    def test_task_id_from_id_metadata(self, tmp_path):
        """ID: metadata field is used as task_id."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **My Task**\n"
            "  - ID: my-explicit-task-id\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].task_id == "my-explicit-task-id"

    def test_risk_metadata_populated(self, tmp_path):
        """Risk: metadata is populated on TasklistItem."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Risky task**\n"
            "  - ID: risky-task\n"
            "  - Risk: high\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].risk == "high"

    def test_tests_metadata_populated(self, tmp_path):
        """Tests: metadata is populated on TasklistItem."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with tests**\n"
            "  - ID: task-with-tests\n"
            "  - Tests: pytest tests/test_foo.py\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].tests == "pytest tests/test_foo.py"

    def test_empty_file_returns_empty(self, tmp_path):
        """list_tasks() returns empty list for empty file."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text("")
        provider = FileTasklistProvider(f)
        assert provider.list_tasks() == []

    def test_missing_file_returns_empty(self, tmp_path):
        """list_tasks() returns empty list when file does not exist."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        assert provider.list_tasks() == []


# ---------------------------------------------------------------------------
# FileTasklistProvider – canonical metadata field parity
# ---------------------------------------------------------------------------

class TestTasklistParserCanonicalFields:
    """Tasklist parser/provider round-trips canonical metadata fields."""

    def test_design_ref_snake_case_parsed_in_metadata_and_tasks(self, tmp_path):
        """design_ref metadata parses and maps onto TasklistItem.design_ref."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with design ref**\n"
            "  - ID: task-with-design-ref\n"
            "  - design_ref: my-design\n"
        )
        provider = FileTasklistProvider(f)

        raw_text = provider._mgr.extract_all_task_ids()[0]["raw_text"]
        metadata = provider._mgr._parse_task_metadata(raw_text)
        assert metadata["design_ref"] == "my-design"

        tasks = provider.list_tasks()
        assert tasks[0].design_ref == "my-design"

    def test_design_ref_hyphenated_key_parsed(self, tmp_path):
        """design-ref metadata parses to design_ref."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with design ref**\n"
            "  - ID: task-with-design-ref\n"
            "  - design-ref: my-design\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].design_ref == "my-design"

    def test_opportunity_ref_snake_case_parsed_in_metadata_and_tasks(self, tmp_path):
        """opportunity_ref metadata parses and maps onto TasklistItem.opportunity_ref."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with opportunity ref**\n"
            "  - ID: task-with-opportunity-ref\n"
            "  - opportunity_ref: my-opportunity\n"
        )
        provider = FileTasklistProvider(f)

        raw_text = provider._mgr.extract_all_task_ids()[0]["raw_text"]
        metadata = provider._mgr._parse_task_metadata(raw_text)
        assert metadata["opportunity_ref"] == "my-opportunity"

        tasks = provider.list_tasks()
        assert tasks[0].opportunity_ref == "my-opportunity"

    def test_opportunity_ref_hyphenated_key_parsed(self, tmp_path):
        """opportunity-ref metadata parses to opportunity_ref."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with opportunity ref**\n"
            "  - ID: task-with-opportunity-ref\n"
            "  - opportunity-ref: my-opportunity\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].opportunity_ref == "my-opportunity"

    def test_acceptance_criteria_parsed(self, tmp_path):
        """Acceptance metadata parses to criteria."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with acceptance**\n"
            "  - ID: task-with-acceptance\n"
            "  - Acceptance: criteria text\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].criteria == "criteria text"

    def test_legacy_criteria_and_success_keys_still_parse(self, tmp_path):
        """Criteria and Success spellings continue to parse to criteria."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Task with criteria key**\n"
            "  - ID: task-with-criteria-key\n"
            "  - Criteria: criteria value\n"
            "\n"
            "- [ ] **Task with success key**\n"
            "  - ID: task-with-success-key\n"
            "  - Success: success value\n"
        )
        provider = FileTasklistProvider(f)
        tasks = provider.list_tasks()
        assert tasks[0].criteria == "criteria value"
        assert tasks[1].criteria == "success value"

    def test_append_round_trip_preserves_canonical_fields(self, tmp_path):
        """append_tasks -> list_tasks preserves design_ref, opportunity_ref, criteria."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TasklistItem, TaskStatus

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        item = TasklistItem(
            task_id="canonical-round-trip",
            title="Canonical Round Trip",
            status=TaskStatus.todo,
            design_ref="my-design",
            opportunity_ref="my-opportunity",
            criteria="must pass",
        )
        provider.append_tasks([item])

        content = f.read_text()
        assert "  - design-ref: my-design" in content
        assert "  - opportunity-ref: my-opportunity" in content
        assert "  - Acceptance: must pass" in content

        tasks = provider.list_tasks()
        assert tasks[0].design_ref == "my-design"
        assert tasks[0].opportunity_ref == "my-opportunity"
        assert tasks[0].criteria == "must pass"


# ---------------------------------------------------------------------------
# FileTasklistProvider – get_task
# ---------------------------------------------------------------------------

class TestFileTasklistProviderGetTask:
    """get_task(id) finds task by task_id."""

    def test_get_existing_task(self, tmp_path):
        """get_task returns the matching TasklistItem."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **First task**\n"
            "  - ID: first-task\n"
            "\n"
            "- [ ] **Second task**\n"
            "  - ID: second-task\n"
        )
        provider = FileTasklistProvider(f)
        result = provider.get_task("second-task")
        assert result is not None
        assert result.task_id == "second-task"
        assert result.title == "Second task"

    def test_get_missing_task_returns_none(self, tmp_path):
        """get_task returns None when id not found."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Some task**\n"
            "  - ID: some-task\n"
        )
        provider = FileTasklistProvider(f)
        result = provider.get_task("not-exist")
        assert result is None

    def test_get_on_missing_file_returns_none(self, tmp_path):
        """get_task returns None when file does not exist."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        result = provider.get_task("any-id")
        assert result is None


# ---------------------------------------------------------------------------
# FileTasklistProvider – append_tasks
# ---------------------------------------------------------------------------

class TestFileTasklistProviderAppendTasks:
    """append_tasks() serializes items to checklist markdown and appends to file."""

    def test_append_creates_file_if_not_exists(self, tmp_path):
        """append_tasks creates the file when it doesn't exist."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TasklistItem, TaskStatus

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        item = TasklistItem(
            task_id="new-task",
            title="New Task",
            status=TaskStatus.todo,
        )
        provider.append_tasks([item])
        assert f.exists()
        content = f.read_text()
        assert "- [ ]" in content
        assert "New Task" in content

    def test_append_adds_to_existing_file(self, tmp_path):
        """append_tasks appends to existing file without overwriting."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TasklistItem, TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **Existing task**\n"
            "  - ID: existing-task\n"
        )
        provider = FileTasklistProvider(f)
        item = TasklistItem(
            task_id="new-task",
            title="New Task",
            status=TaskStatus.todo,
        )
        provider.append_tasks([item])
        content = f.read_text()
        assert "existing-task" in content
        assert "New Task" in content

    def test_append_round_trip(self, tmp_path):
        """append_tasks + list_tasks returns the appended items."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TasklistItem, TaskStatus

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        item = TasklistItem(
            task_id="round-trip-task",
            title="Round Trip Task",
            status=TaskStatus.todo,
            risk="medium",
        )
        provider.append_tasks([item])
        tasks = provider.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_id == "round-trip-task"
        assert tasks[0].title == "Round Trip Task"
        assert tasks[0].risk == "medium"

    def test_append_multiple_items(self, tmp_path):
        """append_tasks appends all items in the list."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TasklistItem, TaskStatus

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        items = [
            TasklistItem(task_id="task-a", title="Task A", status=TaskStatus.todo),
            TasklistItem(task_id="task-b", title="Task B", status=TaskStatus.todo),
        ]
        provider.append_tasks(items)
        tasks = provider.list_tasks()
        ids = [t.task_id for t in tasks]
        assert "task-a" in ids
        assert "task-b" in ids

    def test_append_task_id_in_output(self, tmp_path):
        """append_tasks writes the ID: field so round-trip preserves task_id."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TasklistItem, TaskStatus

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        item = TasklistItem(
            task_id="explicit-id",
            title="Some Task",
            status=TaskStatus.todo,
        )
        provider.append_tasks([item])
        content = f.read_text()
        assert "ID: explicit-id" in content

    def test_append_raises_on_invalid_task_before_io(self, tmp_path):
        """Invalid TasklistItem raises and does not create/write file."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import (
            ArtifactValidationError,
            TasklistItem,
            TaskStatus,
        )

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        invalid = TasklistItem(task_id="", title="Invalid", status=TaskStatus.todo)

        with pytest.raises(ArtifactValidationError):
            provider.append_tasks([invalid])

        assert not f.exists()

    def test_append_raises_without_partial_writes(self, tmp_path):
        """Any invalid task in batch raises and writes nothing."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import (
            ArtifactValidationError,
            TasklistItem,
            TaskStatus,
        )

        f = tmp_path / "tasklist.md"
        f.write_text("- [ ] **Existing task**\n  - ID: existing-task\n")
        provider = FileTasklistProvider(f)
        tasks = [
            TasklistItem(task_id="valid-task", title="Valid", status=TaskStatus.todo),
            TasklistItem(task_id="", title="Invalid", status=TaskStatus.todo),
        ]

        with pytest.raises(ArtifactValidationError):
            provider.append_tasks(tasks)

        assert f.read_text() == "- [ ] **Existing task**\n  - ID: existing-task\n"


# ---------------------------------------------------------------------------
# FileTasklistProvider – update_task_status
# ---------------------------------------------------------------------------

class TestFileTasklistProviderUpdateTaskStatus:
    """update_task_status delegates todo→done and raises for unsupported statuses."""

    def test_todo_to_done_marks_task_complete(self, tmp_path):
        """update_task_status(id, done) marks the task as complete."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **My Task**\n"
            "  - ID: my-task\n"
        )
        provider = FileTasklistProvider(f)
        provider.update_task_status("my-task", TaskStatus.done)
        content = f.read_text()
        assert "- [x] **My Task**" in content
        assert "- [ ] **My Task**" not in content

    def test_in_progress_raises_not_implemented(self, tmp_path):
        """update_task_status raises NotImplementedError for in_progress."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **My Task**\n"
            "  - ID: my-task\n"
        )
        provider = FileTasklistProvider(f)
        with pytest.raises(NotImplementedError, match="in_progress"):
            provider.update_task_status("my-task", TaskStatus.in_progress)

    def test_blocked_raises_not_implemented(self, tmp_path):
        """update_task_status raises NotImplementedError for blocked."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [ ] **My Task**\n"
            "  - ID: my-task\n"
        )
        provider = FileTasklistProvider(f)
        with pytest.raises(NotImplementedError, match="blocked"):
            provider.update_task_status("my-task", TaskStatus.blocked)

    def test_done_to_done_is_noop(self, tmp_path):
        """update_task_status for already-done task is a no-op (returns without error)."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifacts.models import TaskStatus

        f = tmp_path / "tasklist.md"
        f.write_text(
            "- [x] **Completed Task**\n"
            "  - ID: completed-task\n"
        )
        provider = FileTasklistProvider(f)
        # Should not raise
        provider.update_task_status("completed-task", TaskStatus.done)
        content = f.read_text()
        assert "- [x] **Completed Task**" in content

    def test_get_snapshot_returns_full_content(self, tmp_path):
        """get_snapshot returns full tasklist content."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        content = "# Tasklist\n\n- [ ] **Task**\n  - ID: task-1\n"
        f.write_text(content)
        provider = FileTasklistProvider(f)

        assert provider.get_snapshot() == content

    def test_restore_snapshot_replaces_full_content(self, tmp_path):
        """restore_snapshot replaces tasklist file contents."""
        from millstone.artifact_providers.file import FileTasklistProvider

        f = tmp_path / "tasklist.md"
        f.write_text("# Tasklist\n")
        provider = FileTasklistProvider(f)
        replacement = "# Tasklist\n\n- [ ] **Restored**\n  - ID: restored\n"

        provider.restore_snapshot(replacement)

        assert f.read_text() == replacement


# ---------------------------------------------------------------------------
# FileTasklistProvider – protocol conformance
# ---------------------------------------------------------------------------

class TestFileTasklistProviderProtocolConformance:
    """FileTasklistProvider satisfies the TasklistProvider Protocol."""

    def test_isinstance_check(self, tmp_path):
        """isinstance(FileTasklistProvider(...), TasklistProvider) is True."""
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifact_providers.protocols import TasklistProvider

        f = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(f)
        assert isinstance(provider, TasklistProvider)


# ---------------------------------------------------------------------------
# File providers - from_config and registry registration
# ---------------------------------------------------------------------------

class TestFileProvidersFromConfigAndRegistration:
    """from_config and default "file" backend registration behavior."""

    def test_opportunity_from_config_returns_provider(self, tmp_path):
        from millstone.artifact_providers.file import FileOpportunityProvider
        from millstone.artifact_providers.protocols import OpportunityProvider

        provider = FileOpportunityProvider.from_config({"path": str(tmp_path / "opps.md")})
        assert isinstance(provider, FileOpportunityProvider)
        assert isinstance(provider, OpportunityProvider)

    def test_opportunity_from_config_missing_path_raises(self):
        from millstone.artifact_providers.file import FileOpportunityProvider

        with pytest.raises(ValueError, match="path"):
            FileOpportunityProvider.from_config({})

    def test_opportunity_file_backend_registered(self):
        import millstone.artifact_providers.file  # noqa: F401
        from millstone.artifact_providers.registry import list_opportunity_backends

        assert "file" in list_opportunity_backends()

    def test_design_from_config_returns_provider(self, tmp_path):
        from millstone.artifact_providers.file import FileDesignProvider
        from millstone.artifact_providers.protocols import DesignProvider

        provider = FileDesignProvider.from_config({"path": str(tmp_path / "designs")})
        assert isinstance(provider, FileDesignProvider)
        assert isinstance(provider, DesignProvider)

    def test_design_from_config_missing_path_raises(self):
        from millstone.artifact_providers.file import FileDesignProvider

        with pytest.raises(ValueError, match="path"):
            FileDesignProvider.from_config({})

    def test_design_file_backend_registered(self):
        import millstone.artifact_providers.file  # noqa: F401
        from millstone.artifact_providers.registry import list_design_backends

        assert "file" in list_design_backends()

    def test_tasklist_from_config_returns_provider(self, tmp_path):
        from millstone.artifact_providers.file import FileTasklistProvider
        from millstone.artifact_providers.protocols import TasklistProvider

        provider = FileTasklistProvider.from_config({"path": str(tmp_path / "tasklist.md")})
        assert isinstance(provider, FileTasklistProvider)
        assert isinstance(provider, TasklistProvider)

    def test_tasklist_from_config_missing_path_raises(self):
        from millstone.artifact_providers.file import FileTasklistProvider

        with pytest.raises(ValueError, match="path"):
            FileTasklistProvider.from_config({})

    def test_tasklist_file_backend_registered(self):
        import millstone.artifact_providers.file  # noqa: F401
        from millstone.artifact_providers.registry import list_tasklist_backends

        assert "file" in list_tasklist_backends()

# ---------------------------------------------------------------------------
# get_prompt_placeholders() tests
# ---------------------------------------------------------------------------

class TestFileProviderGetPromptPlaceholders:
    """File providers return correct placeholder keys with embedded file paths."""

    def test_tasklist_provider_returns_all_keys(self, tmp_path):
        from millstone.artifact_providers.file import FileTasklistProvider

        path = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(path)
        placeholders = provider.get_prompt_placeholders()

        assert "TASKLIST_READ_INSTRUCTIONS" in placeholders
        assert "TASKLIST_COMPLETE_INSTRUCTIONS" in placeholders
        assert "TASKLIST_REWRITE_INSTRUCTIONS" in placeholders
        assert "TASKLIST_APPEND_INSTRUCTIONS" in placeholders
        assert "TASKLIST_UPDATE_INSTRUCTIONS" in placeholders

    def test_tasklist_provider_embeds_path(self, tmp_path):
        from millstone.artifact_providers.file import FileTasklistProvider

        path = tmp_path / "tasklist.md"
        provider = FileTasklistProvider(path)
        placeholders = provider.get_prompt_placeholders()

        path_str = str(path)
        for value in placeholders.values():
            assert path_str in value, f"Expected path in: {value!r}"

    def test_opportunity_provider_returns_keys(self, tmp_path):
        from millstone.artifact_providers.file import FileOpportunityProvider

        path = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(path)
        placeholders = provider.get_prompt_placeholders()

        assert "OPPORTUNITY_WRITE_INSTRUCTIONS" in placeholders
        assert "OPPORTUNITY_READ_INSTRUCTIONS" in placeholders

    def test_opportunity_provider_embeds_path(self, tmp_path):
        from millstone.artifact_providers.file import FileOpportunityProvider

        path = tmp_path / "opportunities.md"
        provider = FileOpportunityProvider(path)
        placeholders = provider.get_prompt_placeholders()

        path_str = str(path)
        for value in placeholders.values():
            assert path_str in value, f"Expected path in: {value!r}"

    def test_design_provider_returns_keys(self, tmp_path):
        from millstone.artifact_providers.file import FileDesignProvider

        path = tmp_path / "designs"
        provider = FileDesignProvider(path)
        placeholders = provider.get_prompt_placeholders()

        assert "DESIGN_WRITE_INSTRUCTIONS" in placeholders
        assert "DESIGN_READ_INSTRUCTIONS" in placeholders

    def test_design_provider_embeds_path(self, tmp_path):
        from millstone.artifact_providers.file import FileDesignProvider

        path = tmp_path / "designs"
        provider = FileDesignProvider(path)
        placeholders = provider.get_prompt_placeholders()

        path_str = str(path)
        for value in placeholders.values():
            assert path_str in value, f"Expected path in: {value!r}"

    def test_all_values_are_strings(self, tmp_path):
        from millstone.artifact_providers.file import (
            FileDesignProvider,
            FileOpportunityProvider,
            FileTasklistProvider,
        )

        providers = [
            FileTasklistProvider(tmp_path / "tasklist.md"),
            FileOpportunityProvider(tmp_path / "opportunities.md"),
            FileDesignProvider(tmp_path / "designs"),
        ]
        for provider in providers:
            for key, value in provider.get_prompt_placeholders().items():
                assert isinstance(value, str), f"Value for {key!r} is not str"
                assert value, f"Value for {key!r} is empty"

"""Source layer: connectors, tiers, caching, citation parsing.

The theme running through this file is the rule that matters most in the whole
system: **a model cannot influence a source's evidence tier.** Several tests
here exist only to make that structurally checkable rather than merely
documented.

Network tests are marked ``network`` and are skipped by default. A unit suite
that reaches a state legislature's web server is slow, flaky, and rude.
Run them with ``pytest -m network``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from abca.canonical import digest_text
from abca.schema.enums import ClaimType, SourceTier
from abca.sources.base import (
    InvalidCitation,
    RetrievedDocument,
    SourceNotFound,
    SourceUnavailable,
    build_document,
)
from abca.sources.cache import SourceCache
from abca.sources.ilcs import ILCSConnector, find_citations, parse_citation
from abca.sources.registry import ROUTING, build_connectors, connectors_for, tier_of
from abca.sources.text import collapse, extract_title, html_to_text

#: Statute text with the hard line wraps ILGA actually serves. Quotes in real
#: model output span these breaks, which is why verification collapses
#: whitespace.
STATUTE = """(10 ILCS 5/10-2) (from Ch. 46, par. 10-2)

Sec. 10-2.
Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled.
"""


class StubConnector:
    """A connector whose tier is fixed, like every real one."""

    name = "stub"
    tier = SourceTier.T0

    def fetch(self, locator: str) -> RetrievedDocument:  # pragma: no cover
        raise SourceNotFound(locator, connector=self.name)

    def search(self, query: str, *, limit: int = 5):  # pragma: no cover
        return []


@pytest.fixture
def document():
    return build_document(
        connector=StubConnector(), doc_id="s-001", title="10 ILCS 5/10-2",
        url="https://www.ilga.gov/x", text=STATUTE, locator="10 ILCS 5/10-2",
        retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


# ==========================================================================
# The tier rule
# ==========================================================================


class TestTierIsNotNegotiable:
    def test_tier_comes_from_the_connector(self, document):
        assert document.tier is SourceTier.T0

    def test_to_citation_has_no_tier_parameter(self, document):
        """The structural guarantee: a caller cannot pass a tier in.

        If this signature ever grows a tier argument, model output could reach
        it, and every downstream evidence gate becomes decorative.
        """
        import inspect

        parameters = inspect.signature(document.to_citation).parameters
        assert "tier" not in parameters

    def test_citation_inherits_the_document_tier(self, document):
        citation = document.to_citation("Sec. 10-2.")
        assert citation.tier is SourceTier.T0
        assert citation.supports_verdict

    def test_connector_tier_is_a_class_attribute(self):
        """Not an instance field, so it cannot be set per-construction."""
        assert ILCSConnector.tier is SourceTier.T0
        assert tier_of("ilcs") is SourceTier.T0

    def test_citation_carries_the_documents_real_hash(self, document):
        citation = document.to_citation("Sec. 10-2.")
        assert citation.content_hash == document.content_hash
        assert citation.content_hash == digest_text(document.text)


# ==========================================================================
# Quote verification
# ==========================================================================


class TestQuoteVerification:
    def test_exact_quote_verifies(self, document):
        assert document.contains("25,000 qualified voters, whichever is less")

    def test_quote_spanning_a_line_wrap_verifies(self, document):
        """Statute text is hard-wrapped; no model reproduces that byte for byte.

        Rejecting a correct quote over a line break would push the adjudicator
        toward citing nothing, which is the opposite of what the evidence gate
        is for.
        """
        assert document.contains(
            "signed by 1% of the number of voters who voted at the next preceding "
            "Statewide general election or 25,000 qualified voters, whichever is less"
        )

    def test_stored_quote_is_the_sources_wording(self, document):
        """A citation must reproduce the source, not the model's reflow of it."""
        recovered = document.locate(
            "petition signed by 1% of the number of voters who voted"
        )
        assert recovered is not None
        assert "\n" in recovered  # the statute's own line break survived
        assert recovered in document.text

    def test_fabricated_quote_is_rejected(self, document):
        assert not document.contains(
            "signed by 25,000 qualified voters of the State in every case"
        )

    def test_one_altered_number_is_rejected(self, document):
        """The dangerous case: plausible, nearly identical, legally opposite."""
        assert not document.contains("signed by 5% of the number of voters")

    def test_dropped_negation_is_rejected(self, document):
        assert not document.contains("shall not file a petition")

    def test_empty_quote_never_verifies(self, document):
        """A zero-width quote would otherwise 'appear' in every document."""
        assert not document.contains("")
        assert document.locate("") is None

    def test_locate_returns_none_for_absent_text(self, document):
        assert document.locate("no such language anywhere") is None


# ==========================================================================
# Citation parsing
# ==========================================================================


class TestCitationParsing:
    @pytest.mark.parametrize(
        ("citation", "filename"),
        [
            ("10 ILCS 5/10-2", "001000050K10-2.htm"),
            ("5 ILCS 140/1", "000501400K1.htm"),
            ("20 ILCS 3960/4.5", "002039600K4.5.htm"),
            ("10 ILCS 5/1-101", "001000050K1-101.htm"),
        ],
    )
    def test_filename_encoding(self, citation, filename):
        """Verified against the live site. The act's trailing decimal digit is
        not decorative -- a plain five-digit zero-pad 404s on every request."""
        assert parse_citation(citation).filename == filename

    def test_round_trips_to_string(self):
        assert str(parse_citation("10 ILCS 5/10-2")) == "10 ILCS 5/10-2"

    def test_finds_a_citation_inside_prose(self):
        parsed = parse_citation("As required under 10 ILCS 5/10-2, petitions must...")
        assert parsed.section == "10-2"

    def test_case_insensitive(self):
        assert parse_citation("10 ilcs 5/10-2").chapter == 10

    def test_multiple_citations_deduplicated_in_order(self):
        found = find_citations("See 10 ILCS 5/10-2 and 5 ILCS 140/1, then 10 ILCS 5/10-2.")
        assert [str(c) for c in found] == ["10 ILCS 5/10-2", "5 ILCS 140/1"]

    @pytest.mark.parametrize("text", ["no citation here", "10 ILCS", "ILCS 5/10-2", ""])
    def test_malformed_citations_are_rejected(self, text):
        """Guessing would fetch the wrong statute, and the adjudicator would
        then quote it faithfully -- a confident, well-cited, wrong answer."""
        with pytest.raises(InvalidCitation):
            parse_citation(text)


# ==========================================================================
# HTML extraction
# ==========================================================================


class TestTextExtraction:
    def test_title(self):
        assert extract_title("<html><head><title>10 ILCS 5/10-2</title></head>") == "10 ILCS 5/10-2"

    def test_tags_stripped_and_entities_unescaped(self):
        assert html_to_text("<p>A &amp; B</p>") == "A & B"

    def test_block_elements_become_paragraph_breaks(self):
        """Paragraphs are separated by a blank line, matching how the page reads."""
        assert html_to_text("<p>One</p><p>Two</p>").splitlines() == ["One", "", "Two"]

    def test_crlf_normalized(self):
        """A stray \\r would end up inside stored quotes AND inside the content
        hash, making the same statute hash differently on different platforms."""
        assert "\r" not in html_to_text("<p>One\r\nTwo</p>")

    def test_scripts_and_styles_dropped(self):
        assert "alert" not in html_to_text("<script>alert(1)</script><p>Text</p>")

    def test_collapse_is_indexing_only(self):
        assert collapse("a\n  b\tc") == "a b c"


# ==========================================================================
# Cache
# ==========================================================================


class TestSourceCache:
    def test_round_trip(self, tmp_path, document):
        cache = SourceCache(tmp_path)
        cache.put(document)
        entry = cache.get("stub", "10 ILCS 5/10-2")
        assert entry is not None
        assert entry.document.content_hash == document.content_hash
        assert not entry.stale

    def test_retrieved_at_is_preserved_not_refreshed(self, tmp_path, document):
        """A timestamp that advanced on a cache hit would make stale evidence
        look fresh and would defeat drift detection entirely."""
        cache = SourceCache(tmp_path)
        cache.put(document)
        assert cache.get("stub", "10 ILCS 5/10-2").document.retrieved_at == document.retrieved_at

    def test_miss_returns_none(self, tmp_path):
        assert SourceCache(tmp_path).get("stub", "nothing") is None

    def test_planted_text_is_rejected(self, tmp_path, document):
        """The security boundary.

        Without the read-time hash check, anyone who can write to the cache
        could plant fabricated statute text -- and the adjudicator's quote
        verification would then confirm it perfectly, because quotes are checked
        against exactly this text.
        """
        cache = SourceCache(tmp_path)
        path = cache.put(document)
        payload = json.loads(path.read_text())
        payload["text"] = "FABRICATED: petitions require exactly 25,000 signatures."
        path.write_text(json.dumps(payload))
        assert cache.get("stub", "10 ILCS 5/10-2") is None

    def test_corrupt_entry_is_a_miss_not_an_error(self, tmp_path, document):
        cache = SourceCache(tmp_path)
        path = cache.put(document)
        path.write_text("{not json")
        assert cache.get("stub", "10 ILCS 5/10-2") is None

    def test_unknown_format_version_is_ignored(self, tmp_path, document):
        cache = SourceCache(tmp_path)
        path = cache.put(document)
        payload = json.loads(path.read_text())
        payload["format"] = 999
        path.write_text(json.dumps(payload))
        assert cache.get("stub", "10 ILCS 5/10-2") is None

    def test_ttl_marks_entries_stale(self, tmp_path, document):
        cache = SourceCache(tmp_path, ttl=timedelta(seconds=-1))
        cache.put(document)
        assert cache.get("stub", "10 ILCS 5/10-2").stale

    def test_stats_and_clear(self, tmp_path, document):
        cache = SourceCache(tmp_path)
        cache.put(document)
        assert cache.stats() == {"stub": 1}
        assert cache.clear("stub") == 1
        assert cache.stats() == {"stub": 0}


# ==========================================================================
# Registry and routing
# ==========================================================================


class TestRegistry:
    def test_legal_claims_route_to_every_statute_connector(self):
        """Which one actually runs is decided by what the claim CITES.

        Two gates rather than one: the type says which connectors are even
        admissible, and the citation grammar picks among them. A legal claim
        naming a CFR section must not be sent to the Illinois statute
        connector -- it would 404 there, and on a source that happened to
        answer it would return the wrong provision.
        """
        routed = connectors_for(ClaimType.LEGAL, build_connectors())
        assert sorted(c.name for c in routed) == ["ecfr", "fedreg", "ilcs"]
        assert all(c.tier is SourceTier.T0 for c in routed)

    @pytest.mark.parametrize(
        "claim_type",
        [ClaimType.NORMATIVE, ClaimType.PREDICTIVE, ClaimType.DEFINITIONAL],
    )
    def test_non_eligible_types_retrieve_nothing(self, claim_type):
        """Spending network calls on a claim the contract forbids adjudicating
        would be buying an answer that must then be discarded."""
        assert ROUTING[claim_type] == ()

    def test_unknown_connector_names_are_ignored(self):
        assert build_connectors(names=["nope"]) == {}


# ==========================================================================
# Offline behaviour
# ==========================================================================


class TestOfflineMode:
    def test_cached_document_is_served(self, tmp_path, document):
        cache = SourceCache(tmp_path)
        cache.put(build_document(
            connector=ILCSConnector(cache=cache, offline=True),
            doc_id="s-001", title="10 ILCS 5/10-2", url="https://www.ilga.gov/x",
            text=STATUTE, locator="10 ILCS 5/10-2",
        ))
        connector = ILCSConnector(cache=cache, offline=True)
        assert connector.fetch("10 ILCS 5/10-2").text == STATUTE

    def test_uncached_document_raises_rather_than_silently_skipping(self, tmp_path):
        """A run that could not consult a source must say so."""
        connector = ILCSConnector(cache=SourceCache(tmp_path), offline=True)
        with pytest.raises(SourceUnavailable, match="offline"):
            connector.fetch("10 ILCS 5/10-2")


# ==========================================================================
# Live network
# ==========================================================================


@pytest.mark.network
class TestLiveILCS:
    """Hits ilga.gov. Skipped by default; run with `pytest -m network`."""

    def test_fetches_the_real_statute(self, tmp_path):
        connector = ILCSConnector(cache=SourceCache(tmp_path))
        document = connector.fetch("10 ILCS 5/10-2")
        assert document.tier is SourceTier.T0
        assert document.title == "10 ILCS 5/10-2"
        assert "established political party" in document.text
        assert document.content_hash == digest_text(document.text)

    def test_real_statute_verifies_a_real_quote(self, tmp_path):
        connector = ILCSConnector(cache=SourceCache(tmp_path))
        document = connector.fetch("10 ILCS 5/10-2")
        assert document.contains(
            "25,000 qualified voters, whichever is less"
        )

    def test_nonexistent_section_is_not_found(self, tmp_path):
        connector = ILCSConnector(cache=SourceCache(tmp_path))
        with pytest.raises(SourceNotFound):
            connector.fetch("10 ILCS 5/99-999")

    def test_second_fetch_is_served_from_cache(self, tmp_path):
        cache = SourceCache(tmp_path)
        connector = ILCSConnector(cache=cache)
        first = connector.fetch("10 ILCS 5/10-6")
        second = connector.fetch("10 ILCS 5/10-6")
        assert first.content_hash == second.content_hash
        assert second.retrieved_at == first.retrieved_at

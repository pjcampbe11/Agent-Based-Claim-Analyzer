"""The shared citation grammar and the federal connectors.

The grammar is the routing table of the whole evidence system: retrieval is
deterministic, so what gets fetched is decided entirely by what a claim NAMES.
That makes a loose pattern more dangerous than a missing one — a near-match
fetches the wrong provision, and the adjudicator then quotes the wrong provision
faithfully, producing an answer indistinguishable from a correct one.
"""

from __future__ import annotations

from datetime import date

import pytest

from abca.pipeline.replay import make_source_probe
from abca.providers.transport import FakeTransport, TextResponse
from abca.schema.enums import ClaimType, SourceTier
from abca.sources.base import SourceNotFound, SourceUnavailable
from abca.sources.cache import SourceCache
from abca.sources.citations import (
    NO_CONNECTOR_YET,
    CitationKind,
    find_citations,
    parse_citation,
)
from abca.sources.ecfr import ECFRConnector, xml_to_text
from abca.sources.ecfr import parse_citation as parse_cfr
from abca.sources.fedreg import FederalRegisterConnector
from abca.sources.fedreg import parse_citation as parse_fr
from abca.sources.ilcs import ILCSConnector
from abca.sources.registry import ROUTING, build_connectors, connectors_for, tier_of

# --------------------------------------------------------------------------
# The grammar
# --------------------------------------------------------------------------


class TestCitationGrammar:
    @pytest.mark.parametrize(
        ("text", "kind", "locator"),
        [
            ("Under 10 ILCS 5/10-2 you need signatures.", CitationKind.ILCS, "10 ILCS 5/10-2"),
            ("see 11 CFR 100.5(a)", CitationKind.CFR, "11 CFR 100.5"),
            ("see 29 C.F.R. § 1910.1200", CitationKind.CFR, "29 CFR 1910.1200"),
            ("40 CFR Part 60 applies", CitationKind.CFR, "40 CFR 60"),
            ("FR Doc. 2026-18141", CitationKind.FR_DOCUMENT, "FR Doc. 2026-18141"),
            ("[FR Doc No: 2024-01234]", CitationKind.FR_DOCUMENT, "FR Doc. 2024-01234"),
            ("published at 89 FR 12345", CitationKind.FR_PAGE, "89 FR 12345"),
            ("91 Fed. Reg. 56737", CitationKind.FR_PAGE, "91 FR 56737"),
            ("defined by 52 U.S.C. 30101(4)", CitationKind.USC, "52 U.S.C. 30101"),
            ("18 U.S.C. §1030(a)(2)", CitationKind.USC, "18 U.S.C. 1030"),
            ("Citizens United, 558 U.S. 310", CitationKind.CASE, "558 U.S. 310"),
            ("558 F.3d 1082 held", CitationKind.CASE, "558 F.3d 1082"),
            ("135 S. Ct. 2584", CitationKind.CASE, "135 S. Ct. 2584"),
        ],
    )
    def test_recognised_forms(self, text, kind, locator):
        found = find_citations(text)
        assert [(c.kind, c.locator) for c in found][:1] == [(kind, locator)]

    def test_subsections_are_dropped_from_the_locator(self):
        """eCFR serves whole sections.

        Keeping the subsection would make ``11 CFR 100.5`` and
        ``11 CFR 100.5(a)`` two cache entries for one document — and the
        subsection is still in the claim text, where the adjudicator sees it.
        """
        assert find_citations("11 CFR 100.5(a)(1)")[0].locator == "11 CFR 100.5"

    def test_citations_are_deduplicated_and_ordered_by_position(self):
        found = find_citations(
            "11 CFR 100.5, then 10 ILCS 5/10-2, then 11 CFR 100.5 again."
        )
        assert [c.locator for c in found] == ["11 CFR 100.5", "10 ILCS 5/10-2"]

    def test_prose_without_citations_finds_nothing(self):
        assert find_citations("Unemployment was 4.1% in June and rents rose 3%.") == []

    def test_a_bare_hyphenated_number_is_not_a_document_number(self):
        """``FR Doc`` is required, and that is a deliberate strictness.

        A bare ``2026-18141`` is indistinguishable from a date range, a case
        number or a page span. Fetching a rule because a sentence contained two
        hyphenated numbers is exactly the confident-wrong retrieval the grammar
        exists to prevent.
        """
        assert find_citations("Between 2026-18141 and 2027 the rule applied") == []

    def test_a_trailing_period_is_not_part_of_an_ilcs_section(self):
        """``10-2.`` encodes to a filename that 404s on every request."""
        assert find_citations("Under 10 ILCS 5/10-2.")[0].locator == "10 ILCS 5/10-2"

    def test_a_bare_number_pair_is_not_a_case_citation(self):
        """A reporter abbreviation is required, and that is the whole guard."""
        assert find_citations("It cost 25,000 dollars over 3 years") == []
        assert find_citations("see page 644 of the brief") == []

    def test_unservable_kinds_are_recognised_and_flagged(self):
        """Recognised-but-unfetchable is a coverage gap somebody can act on.

        "Found no citation at all" looks like the claim cited nothing, which is
        a different and much less useful finding.
        """
        for text in ("52 U.S.C. 30101", "89 FR 12345", "576 U.S. 644"):
            citation = find_citations(text)[0]
            assert citation.kind in NO_CONNECTOR_YET, text
            assert not citation.servable, text
        assert find_citations("11 CFR 100.5")[0].servable

    def test_parse_citation_is_strict(self):
        with pytest.raises(ValueError, match="Recognised forms"):
            parse_citation("no citation in this sentence")

    def test_parse_citation_can_demand_a_kind(self):
        with pytest.raises(ValueError, match="of kind ecfr"):
            parse_citation("10 ILCS 5/10-2", kind=CitationKind.CFR)

    def test_every_kind_names_a_connector_or_is_declared_unserved(self):
        """A kind that named a connector nobody built would fail at fetch time."""
        built = set(build_connectors())
        for kind in CitationKind:
            assert kind in NO_CONNECTOR_YET or kind.value in built


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


class TestRouting:
    def test_legal_claims_reach_every_statute_connector(self):
        routed = connectors_for(ClaimType.LEGAL, build_connectors())
        assert sorted(c.name for c in routed) == ["ecfr", "fedreg", "ilcs"]

    def test_every_routed_connector_is_t0(self):
        for connector in connectors_for(ClaimType.LEGAL, build_connectors()):
            assert connector.tier is SourceTier.T0
            assert tier_of(connector.name) is SourceTier.T0

    def test_empirical_claims_have_no_route_on_purpose(self):
        """Not an omission.

        An empirical claim names no source, so serving it would mean a model
        choosing a statistical series — which would make the set of sources
        consulted depend on model output, and the run irreproducible.
        """
        assert ROUTING[ClaimType.EMPIRICAL] == ()

    def test_a_cfr_citation_is_not_sent_to_the_illinois_connector(self, tmp_path):
        """It would 404 there, and on a source that answered it would be wrong."""
        from abca.pipeline.models import DraftClaim
        from abca.pipeline.retrieve import candidate_citations

        claim = DraftClaim(
            id="c-001", text="Under 11 CFR 100.5 a committee is one that spends $1,000.",
            sentence_index=0, claim_type=ClaimType.LEGAL,
        )
        assert [c.connector for c in candidate_citations(claim)] == ["ecfr"]


# --------------------------------------------------------------------------
# eCFR
# --------------------------------------------------------------------------

CFR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DIV8 N="100.5" TYPE="SECTION">
<HEAD>&#167; 100.5 Political committee.</HEAD>
<P><I>Political committee</I> means any group meeting one of the following:</P>
<P>(a) any group which receives contributions aggregating in excess of $1,000.</P>
</DIV8>"""


class TestECFR:
    def _connector(self, tmp_path, **kwargs):
        transport = FakeTransport({
            "/api/versioner/v1/titles.json": {
                "titles": [{"number": 11, "latest_issue_date": "2026-06-08"}]
            },
            "/api/versioner/v1/full/2026-06-08/title-11.xml?part=100&section=100.5": CFR_XML,
        })
        return ECFRConnector(
            cache=SourceCache(tmp_path), transport=transport, **kwargs
        ), transport

    def test_parses_a_citation_and_derives_the_part(self):
        citation = parse_cfr("11 CFR 100.5")
        assert citation.title == 11
        assert citation.section == "100.5"
        assert citation.part == "100"

    def test_a_bare_part_citation_is_its_own_part(self):
        assert parse_cfr("40 CFR Part 60").part == "60"

    def test_fetch_returns_a_t0_document_with_the_regulation_text(self, tmp_path):
        connector, _ = self._connector(tmp_path)
        document = connector.fetch("11 CFR 100.5")
        assert document.tier is SourceTier.T0
        assert document.title == "11 CFR 100.5"
        assert "contributions aggregating in excess of $1,000" in document.text
        assert document.contains("aggregating in excess of $1,000")

    def test_the_issue_date_is_asked_for_not_assumed(self, tmp_path):
        """eCFR only serves dates it has issued.

        A title amended in February may have a latest issue of June, and
        requesting today 404s — which would make a perfectly valid citation
        look like a nonexistent section.
        """
        connector, transport = self._connector(tmp_path)
        connector.fetch("11 CFR 100.5")
        assert "/api/versioner/v1/titles.json" in transport.paths_called()
        assert connector.issue_date(11) == date(2026, 6, 8)

    def test_the_issue_date_is_resolved_once_per_title(self, tmp_path):
        connector, transport = self._connector(tmp_path)
        connector.fetch("11 CFR 100.5")
        connector.fetch("11 CFR 100.5")
        assert transport.paths_called().count("/api/versioner/v1/titles.json") == 1

    def test_an_explicit_date_overrides_resolution(self, tmp_path):
        """A replay asks for the snapshot the original run cited."""
        connector, transport = self._connector(tmp_path, on=date(2024, 1, 1))
        assert connector.issue_date(11) == date(2024, 1, 1)
        with pytest.raises((SourceNotFound, SourceUnavailable)):
            connector.fetch("11 CFR 100.5")
        assert "/api/versioner/v1/titles.json" not in transport.paths_called()

    def test_the_document_records_the_snapshot_it_asked_for(self, tmp_path):
        connector, _ = self._connector(tmp_path)
        document = connector.fetch("11 CFR 100.5")
        assert document.metadata["as_of"] == "2026-06-08"
        assert "2026-06-08" in document.metadata["api_url"]

    def test_offline_miss_raises_rather_than_returning_nothing(self, tmp_path):
        connector = ECFRConnector(cache=SourceCache(tmp_path), offline=True)
        with pytest.raises(SourceUnavailable, match="not cached"):
            connector.fetch("11 CFR 100.5")

    def test_search_is_empty_not_an_error(self, tmp_path):
        """Keyword search would mean a model choosing the source."""
        connector, _ = self._connector(tmp_path)
        assert connector.search("political committee") == []

    def test_url_round_trips_to_a_locator(self, tmp_path):
        connector, _ = self._connector(tmp_path)
        url = connector.fetch("11 CFR 100.5").url
        assert connector.locator_for_url(url) == "11 CFR 100.5"
        assert connector.locator_for_url("https://example.com/x") is None

    def test_xml_flattens_without_losing_text(self):
        text = xml_to_text(CFR_XML)
        assert "§ 100.5 Political committee." in text
        assert "in excess of $1,000" in text
        assert "<P>" not in text


# --------------------------------------------------------------------------
# Federal Register
# --------------------------------------------------------------------------

FR_META = {
    "document_number": "2026-18141",
    "title": "Establishing the United States Space Academy",
    "citation": "91 FR 56737",
    "html_url": "https://www.federalregister.gov/documents/2026/09/03/2026-18141/x",
    "publication_date": "2026-09-03",
    "type": "Presidential Document",
    "start_page": 56737,
    "end_page": 56739,
}
FR_TEXT = (
    "<html><body><pre>[Federal Register Volume 91, Number 170]\n"
    "[Pages 56737-56739]\nThe rule takes effect on October 1, 2026.</pre></body></html>"
)


class TestFederalRegister:
    def _connector(self, tmp_path, *, meta=None, text=FR_TEXT):
        api = FakeTransport({
            f"/api/v1/documents/2026-18141.json?{_fields()}": meta or FR_META,
        })
        gpo = FakeTransport({
            "/content/pkg/FR-2026-09-03/html/2026-18141.htm": text,
        })
        return FederalRegisterConnector(
            cache=SourceCache(tmp_path), transport=api, text_transport=gpo,
        ), api, gpo

    def test_parses_the_document_number_forms(self):
        assert parse_fr("FR Doc. 2026-18141").number == "2026-18141"
        assert parse_fr("[FR Doc No: 2024-01234]").number == "2024-01234"

    def test_the_volume_page_form_is_refused_with_the_reason(self):
        from abca.sources.base import InvalidCitation

        with pytest.raises(InvalidCitation, match="volume-and-page"):
            parse_fr("91 FR 56737")

    def test_fetch_identifies_from_the_register_and_quotes_from_gpo(self, tmp_path):
        """Two hosts, and the split is not architectural taste.

        Every full-text endpoint on federalregister.gov is behind a bot wall
        keyed on TLS fingerprint. GPO publishes the same printed page openly,
        and GPO is the authority the Register is printed by.
        """
        connector, api, gpo = self._connector(tmp_path)
        document = connector.fetch("FR Doc. 2026-18141")
        assert document.tier is SourceTier.T0
        assert document.metadata["citation"] == "91 FR 56737"
        assert "govinfo.gov" in document.metadata["text_source"]
        assert document.contains("takes effect on October 1, 2026")
        assert api.paths_called() and gpo.paths_called()

    def test_a_document_with_no_publication_date_is_not_guessed_at(self, tmp_path):
        meta = {**FR_META}
        meta.pop("publication_date")
        connector, _, _ = self._connector(tmp_path, meta=meta)
        with pytest.raises(SourceNotFound, match="publication date"):
            connector.fetch("FR Doc. 2026-18141")

    def test_a_cross_host_redirect_is_refused(self, tmp_path):
        """The bot wall answers with one, and a tier is a promise about a host."""
        connector, _, _ = self._connector(tmp_path)
        connector._text_transport = FakeTransport({
            "/content/pkg/FR-2026-09-03/html/2026-18141.htm": TextResponse(
                status=302, text="",
                headers={"location": "https://unblock.federalregister.gov/"},
            ),
        })
        with pytest.raises(SourceUnavailable, match="redirected off"):
            connector.fetch("FR Doc. 2026-18141")

    def test_url_round_trips_to_a_locator(self, tmp_path):
        connector, _, _ = self._connector(tmp_path)
        url = connector.fetch("FR Doc. 2026-18141").url
        assert connector.locator_for_url(url) == "FR Doc. 2026-18141"
        assert connector.locator_for_url("https://example.com/x") is None


def _fields() -> str:
    from abca.sources.fedreg import FIELDS

    return "&".join(f"fields[]={field}" for field in FIELDS)


# --------------------------------------------------------------------------
# The drift probe
# --------------------------------------------------------------------------


class TestDriftProbe:
    """Every connector must be probeable, or its drift goes unreported.

    The probe used to ``isinstance``-check the single connector that existed.
    That meant each connector added afterwards was silently un-probeable, and a
    changed source behind it would not have surfaced while ``verify`` still
    reported the run as fine.
    """

    def test_every_built_connector_can_resolve_its_own_urls(self):
        for connector in build_connectors().values():
            assert hasattr(connector, "locator_for_url"), connector.name

    def test_a_url_belonging_to_no_connector_returns_none(self, tmp_path):
        probe = make_source_probe(build_connectors(cache=SourceCache(tmp_path), offline=True))
        assert probe("https://example.com/whatever") is None

    def test_an_unreachable_source_is_not_reported_as_drift(self, tmp_path):
        """Unreachable now says nothing about whether the source changed."""
        connectors = {"ilcs": ILCSConnector(cache=SourceCache(tmp_path), offline=True)}
        probe = make_source_probe(connectors)
        url = ("https://www.ilga.gov/documents/legislation/ilcs/documents/"
               "001000050K10-2.htm")
        assert probe(url) is None


# --------------------------------------------------------------------------
# Live network
# --------------------------------------------------------------------------


@pytest.mark.network
class TestLiveFederalSources:
    """Opt-in: ``pytest -m network``. These hit real government servers."""

    def test_ecfr_returns_the_committee_threshold(self, tmp_path):
        """11 CFR 100.5 is the citation docs/03 rests its FEC section on.

        If this ever fails, the registration memo needs re-reading — which is
        the whole point of pinning a claim to a fetchable source.
        """
        connector = ECFRConnector(cache=SourceCache(tmp_path))
        document = connector.fetch("11 CFR 100.5")
        assert document.tier is SourceTier.T0
        assert document.contains("contributions aggregating in excess of $1,000")
        assert document.content_hash.startswith("sha256:")

    def test_ecfr_second_fetch_is_served_from_cache(self, tmp_path):
        connector = ECFRConnector(cache=SourceCache(tmp_path))
        first = connector.fetch("11 CFR 100.5")
        second = connector.fetch("11 CFR 100.5")
        assert first.content_hash == second.content_hash

    def test_ecfr_nonexistent_section_is_not_found(self, tmp_path):
        connector = ECFRConnector(cache=SourceCache(tmp_path))
        with pytest.raises((SourceNotFound, SourceUnavailable)):
            connector.fetch("11 CFR 999.999")

    def test_federal_register_document_resolves_through_gpo(self, tmp_path):
        connector = FederalRegisterConnector(cache=SourceCache(tmp_path))
        document = connector.fetch("FR Doc. 2026-18141")
        assert document.tier is SourceTier.T0
        assert "govinfo.gov" in document.metadata["text_source"]
        assert document.metadata["citation"].endswith("56737")
        assert "Federal Register" in document.text

    def test_the_drift_probe_reaches_both_federal_sources(self, tmp_path):
        cache = SourceCache(tmp_path)
        probe = make_source_probe({
            "ecfr": ECFRConnector(cache=cache),
            "fedreg": FederalRegisterConnector(cache=cache),
        })
        assert probe("https://www.ecfr.gov/current/title-11/section-100.5")
        assert probe(
            "https://www.federalregister.gov/documents/2026/09/03/2026-18141/x"
        )

"""Sentence splitter tests.

Every case here comes from a shape this domain actually produces. A splitter
that mangles ``10 ILCS 5/10-2`` produces two garbage claims and a citation that
resolves to nothing, so these are correctness tests, not tidiness tests.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from abca.pipeline.sentences import Sentence, locate, split_sentences


def texts(text: str) -> list[str]:
    return [sentence.text for sentence in split_sentences(text)]


class TestSpanInvariant:
    """Everything downstream indexes these spans; the invariant is load-bearing."""

    @pytest.mark.parametrize(
        "text",
        [
            "One. Two. Three.",
            "See 10 ILCS 5/10-2. The statute also requires a slate.",
            "  Leading space. Trailing space.  ",
            "Multi\nline\ntext. Second sentence.",
            "Unicode: café politics. Second.",
        ],
    )
    def test_slicing_the_document_reproduces_the_sentence(self, text):
        for sentence in split_sentences(text):
            assert text[sentence.start : sentence.end] == sentence.text

    def test_indices_are_sequential_from_zero(self):
        sentences = split_sentences("First one. Second one. Third one.")
        assert [s.index for s in sentences] == [0, 1, 2]

    def test_spans_do_not_overlap(self):
        sentences = split_sentences("First one. Second one. Third one.")
        for earlier, later in pairwise(sentences):
            assert earlier.end <= later.start


class TestLegalCitations:
    """The cases that motivated writing this instead of using a library."""

    def test_ilcs_citation_is_not_split(self):
        assert texts("See 10 ILCS 5/10-2. The statute also requires a slate.") == [
            "See 10 ILCS 5/10-2.",
            "The statute also requires a slate.",
        ]

    def test_usc_internal_periods(self):
        assert len(texts("Under 52 U.S.C. 30101 a committee registers. That is separate.")) == 2

    def test_cfr_citation(self):
        assert len(texts("See 11 C.F.R. 100.5 for the threshold. It is $1,000.")) == 2

    def test_case_citation_with_v(self):
        assert texts("Brown v. Board of Ed. changed everything. Later cases narrowed it.") == [
            "Brown v. Board of Ed. changed everything.",
            "Later cases narrowed it.",
        ]

    def test_reporter_citation(self):
        assert len(texts("Cited at 410 F.2d 701 in the opinion. The court disagreed.")) == 2


class TestAbbreviations:
    @pytest.mark.parametrize(
        "text",
        [
            "Sen. Smith objected. The vote proceeded.",
            "Gov. Pritzker signed it. It took effect in June.",
            "The Dept. of Justice filed. The case is pending.",
            "Acme Corp. lobbied for it. The bill passed.",
            "Dr. Chen testified. The committee adjourned.",
        ],
    )
    def test_titles_and_org_abbreviations(self, text):
        assert len(texts(text)) == 2

    def test_initials_are_not_boundaries(self):
        assert len(texts("J. R. Smith filed the petition. It was rejected.")) == 2

    def test_eg_and_ie(self):
        assert len(texts("Some states, e.g. Illinois, require a slate. Others do not.")) == 2


class TestNumbers:
    def test_decimal_is_not_a_boundary(self):
        assert texts("Turnout fell 3.5 percent. That is a large drop.") == [
            "Turnout fell 3.5 percent.",
            "That is a large drop.",
        ]

    def test_thousands_separator(self):
        assert len(texts("It requires 25,000 signatures. The cap is lower after redistricting.")) == 2

    def test_dollar_amount(self):
        assert len(texts("The threshold is $1,000.00 per year. Above that, register.")) == 2


class TestQuotesAndPunctuation:
    def test_boundary_falls_after_the_closing_quote(self):
        sentences = split_sentences('He said "this is wrong." Then he left.')
        assert sentences[0].text == 'He said "this is wrong."'

    def test_closing_paren(self):
        assert len(texts("It passed (barely.) Nobody expected that.")) == 2

    def test_ellipsis_then_question(self):
        assert texts("Wait... what? That cannot be right!") == [
            "Wait... what?",
            "That cannot be right!",
        ]

    def test_multiple_terminators(self):
        assert len(texts("Really?! I had no idea.")) == 2

    def test_lowercase_after_period_vetoes_the_split(self):
        """Usually a mid-sentence abbreviation we do not know about."""
        assert len(texts("Filed under sec. 10 of the act. It was rejected.")) == 2


class TestListsAndParagraphs:
    def test_numbered_items_are_separate_claims(self):
        """People put their strongest political claims in bullet lists."""
        assert texts("1. First item\n2. Second item\n3. Third") == [
            "1. First item",
            "2. Second item",
            "3. Third",
        ]

    def test_bulleted_items(self):
        assert len(texts("Reasons:\n- signatures\n- full slate\n- deadline")) == 4

    def test_lettered_items(self):
        assert len(texts("a) one\nb) two")) == 2

    def test_inline_enumerators_do_not_split(self):
        assert len(texts("The list is 1. long and 2. tedious.")) == 1

    def test_paragraph_break_without_punctuation(self):
        assert texts("First para.\n\nSecond para without punctuation") == [
            "First para.",
            "Second para without punctuation",
        ]


class TestEdgeCases:
    @pytest.mark.parametrize("text", ["", "   ", "\n\n\n"])
    def test_empty_input_yields_nothing(self, text):
        assert split_sentences(text) == []

    def test_no_terminal_punctuation(self):
        assert texts("just a fragment with no period") == ["just a fragment with no period"]

    def test_single_sentence(self):
        assert len(texts("One sentence only.")) == 1

    def test_min_length_filter(self):
        sentences = split_sentences("Ok. Real sentence here.", min_length=5)
        assert [s.text for s in sentences] == ["Real sentence here."]

    def test_determinism(self):
        """Spans are the coordinate system; instability would be DIVERGENT for nothing."""
        text = "Illinois requires 25,000 signatures. See 10 ILCS 5/10-2 for details."
        first = split_sentences(text)
        for _ in range(20):
            assert split_sentences(text) == first

    def test_single_capital_letter_is_read_as_an_initial(self):
        """A deliberate trade, recorded here so it is a decision and not a bug.

        "J. R. Smith filed" must not split, so a lone capital followed by a
        period is treated as an initial. The cost is that "A. B. C." reads as
        one sentence rather than three.

        For this domain that is the right side of the trade by a wide margin:
        names with initials appear constantly in political and legal text,
        while single-letter sentences essentially never do.
        """
        assert len(split_sentences("A. B. C.")) == 1
        assert len(split_sentences("J. R. Smith filed the petition. It failed.")) == 2

    def test_sentence_length(self):
        sentence = Sentence(index=0, text="abc", start=5, end=8)
        assert len(sentence) == 3


class TestLocate:
    def test_finds_within_a_window(self):
        assert locate("hello world", "world", within=(6, 11)) == (6, 11)

    def test_returns_none_outside_the_window(self):
        assert locate("hello world", "hello", within=(6, 11)) is None

    def test_empty_needle_is_none(self):
        """Never returns a zero-width span -- it would point at nothing."""
        assert locate("hello", "") is None

    def test_no_fuzzy_matching(self):
        """An approximate span points a reader at text the claim did not come from."""
        assert locate("the quick brown fox", "the quick  brown fox") is None

#!/usr/bin/env python3
"""levelset -- a comprehension-gap measurement instrument.

Tool ``levelset/0.4.0`` · Taxonomy ``gaps/1.0.0`` · Python 3.11+, standard
library only. Specified by ``docs/21-levelset-tool.md``.

WHAT THIS IS
============
A research instrument. It reads text-only political posts -- posts with content
but **no reference link** -- and records, in a fixed taxonomy, the *comprehension
gaps* their wording carries: places where the phrasing requires an assumption
about how a process works that does not match how it works.

**The unit of analysis is the corpus, not the post.** A single reading is an
intermediate record and is close to worthless on its own. The product is the
aggregate: across N posts and threads, which mechanisms Americans most often get
wrong, how often, in what combinations, and on which topics. That is a
publishable finding about political language that requires adjudicating nothing.

WHAT THIS IS NOT, BY CONSTRUCTION
=================================
It issues no verdicts, because it has no sources. It produces no citations and
no run hashes, because it cannot verify anything. It makes no legal claims. It
records **no author identity anywhere** -- not in a record, not in a report, not
in a log. A corpus of political speech that also stores who said it is a file on
people, and this project does not build those.

It also never says a person failed to understand something. It records that a
*sentence* carries an assumption. Those are different statements, and the
difference is enforced by a lint below rather than requested in a prompt.

+------------------+---------------------------+------------------------------+
|                  | Analyzer (docs 01-20)     | levelset                     |
+==================+===========================+==============================+
| Unit             | One claim                 | **A corpus**                 |
| Purpose          | Adjudicate vs. evidence   | Measure what is misunderstood|
| Sources          | T0-T2 required            | **None**                     |
| Output           | Verdict + cites + hash    | Coded records -> frequency   |
| Publishable      | Yes, with its hash        | **Only past the sym. gate**  |
+------------------+---------------------------+------------------------------+

ISOLATION FROM THE PIPELINE (doc 21 s7) -- DO NOT BREAK THIS
============================================================
This file imports nothing from ``src/abca/``. It writes no ledger entry. It
produces no artifact the Analyzer consumes. It could be deleted without
affecting a build, and ``tests/test_levelset.py`` asserts all of that
mechanically rather than trusting this paragraph.

The reason is the provenance chain. The Analyzer's credibility rests on every
published claim carrying the hash of the run that checked it. A component
producing unhashed, uncited readings must not feed the component producing
hashed, cited verdicts, or the chain has an unverified link and the argument
collapses. The temptation to import ``abca.sources`` "just to resolve one
citation" is exactly how that happens, so the import is impossible rather than
discouraged.

WHY A GAP REPORT IS NOT PUBLISHABLE UNTIL THE SYMMETRY GATE PASSES
==================================================================
If this instrument records more gaps in posts of one political valence than the
other, the report says one side understands government worse than the other.
That is an enormous claim; it is far more likely an artifact of the model than a
fact about the population; and no reader can tell which from the report alone.

``scripts/check_levelset_symmetry.py`` runs this tool over 24 matched post pairs
-- same gap, same shape, opposite valence -- and tests six measures with exact
paired statistics. Doc 21 s8 calls this the highest-priority open item, and it
is a gate rather than a follow-up: a partisan-looking gap report would do more
damage than no report at all.
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

TOOL_VERSION = "levelset/0.4.0"
TAXONOMY_VERSION = "gaps/1.0.0"

# --------------------------------------------------------------------------
# 1. The gap taxonomy -- gaps/1.0.0
# --------------------------------------------------------------------------
# Free-text gaps cannot be counted. "misunderstands how voting works" and
# "thinks a procedural vote is a policy vote" are the same finding written twice,
# and an aggregate over free text is an aggregate over the model's vocabulary
# rather than over the corpus. So every gap gets one of these fixed codes, and
# anything else is coerced to OTHER and flagged.
#
# The taxonomy is versioned SEPARATELY from the tool. Adding a code is a minor
# bump. Changing what an existing code MEANS is a major bump, because it
# silently invalidates comparison against every report produced before it -- the
# counts would still line up and would no longer mean the same thing.

GAP_GROUPS: dict[str, dict[str, str]] = {
    "Legislative process": {
        "VOTE_TYPE": (
            "A procedural vote (cloture, motion to table, motion to recommit, "
            "motion to proceed) is described as a vote on the underlying policy."
        ),
        "VOTE_STAGE": (
            "A step in the process is treated as the end of it -- a committee "
            "vote, or one chamber's passage, described as enactment."
        ),
        "VENUE": (
            "The wrong body is named as having acted, or the scope and "
            "reviewability of the acting body's power is assumed."
        ),
        "CHAMBER_SCOPE": (
            "A power held by one chamber, one leader or one committee is "
            "attributed to 'Congress' as a whole."
        ),
    },
    "Instruments & authority": {
        "INSTRUMENT": (
            "The kind of document is mistaken -- an executive order, agency "
            "guidance or a memo treated as having the force of a statute."
        ),
        "GOVT_LEVEL": (
            "A responsibility held at the federal, state or local level is "
            "assigned to a different level."
        ),
        "AUTHORITY": (
            "An action that requires appropriation, rulemaking or legislation is "
            "described as something an official may simply decide to do."
        ),
        "RULEMAKING": (
            "The notice-and-comment sequence is collapsed -- a proposed rule "
            "treated as final, or a final rule as effective on announcement."
        ),
    },
    "Money": {
        "FUNDING": (
            "Authorization, appropriation, obligation and outlay are treated as "
            "the same event."
        ),
        "SCORING": (
            "A budget score is read outside its window or its baseline -- a "
            "ten-year total read as an annual or per-household cost."
        ),
    },
    "Text & time": {
        "TEXT_VS_TITLE": (
            "A bill's short title, or a headline about it, is treated as a "
            "description of what its text does."
        ),
        "TIMING": (
            "An outcome is attributed to an action that could not yet have "
            "produced it, or a sequence of events is reordered."
        ),
        "SCOPE_QUANTIFIER": (
            "A universal or near-universal quantifier -- every, all, nobody, "
            "always -- is applied beyond what the underlying event supports."
        ),
    },
    "Statistics": {
        "STAT_LEVEL_RATE": (
            "A level and a rate of change are conflated: a falling rate read as "
            "a falling level, or growth read as a total."
        ),
        "STAT_DENOMINATOR": (
            "A count, share or percentage change is used without the base it is "
            "computed on."
        ),
        "STAT_NOMINAL_REAL": (
            "A nominal figure is compared across time without adjustment for "
            "inflation or population."
        ),
        "STAT_BASELINE": (
            "A change measured against a projected baseline is read as a change "
            "against last year's actual figure."
        ),
    },
    "Reasoning & context": {
        "CAUSAL": (
            "Correlation, sequence in time, or a single case is offered as "
            "sufficient proof of causation."
        ),
        "QUOTE_CONTEXT": (
            "A quotation is treated as self-interpreting when its meaning "
            "depends on context that is not present."
        ),
        "RECENCY": (
            "An older, pending or hypothetical action is presented as having "
            "just occurred."
        ),
        "PROCESS_OPACITY": (
            "The post identifies the process itself as unintelligible -- a gap "
            "in what was made available, not in the reader."
        ),
        "OTHER": (
            "A comprehension gap that fits none of the above. Always flagged; a "
            "rising OTHER rate means the taxonomy needs a new code."
        ),
    },
}

#: Flat lookup. Order is the order of GAP_GROUPS, which is the order --taxonomy
#: prints and the order the report renders, so a reader comparing two reports
#: sees rows in the same places.
GAP_CODES: dict[str, str] = {
    code: description
    for group in GAP_GROUPS.values()
    for code, description in group.items()
}

#: The catch-all. Kept as a named constant because three separate code paths
#: coerce to it and a typo in any of them would silently create a 23rd code.
OTHER = "OTHER"

#: Topics are a closed list for the same reason gap codes are. An open topic
#: field produces a report with a hundred one-post topics and no comparability
#: between runs. Unrecognised topics coerce to "other" and are flagged.
TOPICS: tuple[str, ...] = (
    "healthcare", "economy", "taxes", "immigration", "elections", "guns",
    "education", "environment", "criminal-justice", "foreign-policy",
    "labor", "housing", "civil-rights", "technology", "budget", "other",
)

#: Confidence values a reading may carry, weakest first. Ordering matters: the
#: consensus pass DEMOTES, and demotion has to be a well-defined direction.
CONFIDENCE_LEVELS: tuple[str, ...] = ("low", "medium", "high")


# --------------------------------------------------------------------------
# 2. The lints -- doc 21 s4
# --------------------------------------------------------------------------
# "The prompt asks; the code checks." A rule that lives only in a prompt fails
# silently and surfaces months later inside a published number, which is the
# single failure mode this whole repository exists to be an alternative to. So
# every constraint below is enforced here, after the model has spoken, on the
# text that will actually be written to a record.
#
# Redaction is DESTRUCTIVE ON PURPOSE and there is no flag to disable it.

#: Citations and URLs. Redacted -- INCLUDING CORRECT ONES.
#:
#: This looks like vandalism and is the most important rule in the file. The
#: tool has no sources and cannot verify a citation, so a citation it emits is
#: unverified whether or not it happens to be right. A reader cannot tell an
#: accurate one from an invented one, so an accurate one is *worse*: it teaches
#: the reader to trust a channel that will eventually hand them a fabrication.
#: The Analyzer emits citations because it retrieved and hash-pinned them. This
#: tool emits none.
CITATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"https?://\S+", re.IGNORECASE),
    re.compile(r"\bwww\.[^\s,;]+", re.IGNORECASE),
    # 5 U.S.C. 552 / 5 USC § 552(b)(5)
    re.compile(r"\b\d+\s*U\.?\s?S\.?\s?C\.?\s*(?:§+\s*)?\d[\w.\-()§ ]*", re.IGNORECASE),
    # 24 CFR 570.208 / 24 C.F.R. § 570.208
    re.compile(r"\b\d+\s*C\.?\s?F\.?\s?R\.?\s*(?:§+\s*)?\d[\w.\-()§ ]*", re.IGNORECASE),
    # 10 ILCS 5/7-10
    re.compile(r"\b\d+\s*ILCS\s*[\d/.\-]+", re.IGNORECASE),
    # Pub. L. 117-169 / Public Law 117-169
    re.compile(r"\bPub(?:lic)?\.?\s*L(?:aw)?\.?\s*(?:No\.?\s*)?\d+[-–]\d+", re.IGNORECASE),
    # H.R. 1234 / S. 25 / H.Res. 100 / S.J.Res. 4
    re.compile(r"\b(?:H\.?\s?R\.?|S\.?|H\.?\s?J\.?\s?Res\.?|S\.?\s?J\.?\s?Res\.?|"
               r"H\.?\s?Res\.?|S\.?\s?Res\.?)\s?\d{1,5}\b"),
    # Roll Call 316 / roll call vote no. 45
    re.compile(r"\broll[\s\-]?call(?:\s+vote)?(?:\s+(?:no\.?|number))?\s*#?\s*\d+",
               re.IGNORECASE),
    # 597 U.S. 215 -- a reported case citation
    re.compile(r"\b\d+\s+U\.\s?S\.\s+\d+\b"),
    # Federal Register volume/page
    re.compile(r"\b\d+\s*Fed\.?\s*Reg\.?\s*\d+", re.IGNORECASE),
]
CITATION_REPLACEMENT = "[citation removed: this tool does not verify sources]"

#: Verdict vocabulary. FLAGGED, not redacted.
#:
#: Sometimes legitimate: "the post treats the claim as false" is a description
#: of the post's stance, not this tool rendering a verdict. Distinguishing those
#: reliably needs judgement the lint does not have, so it counts them and puts
#: the count in the report footer, where a reader can discount accordingly. A
#: rising flag rate means the prompt has drifted toward adjudication.
VERDICT_WORDS: tuple[str, ...] = (
    "true", "false", "misleading", "debunked", "fact-check", "fact check",
    "disinformation", "misinformation", "hoax", "fabricated", "accurate",
    "inaccurate", "verified", "unsupported", "proven", "disproven", "lie",
)
VERDICT_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in VERDICT_WORDS) + r")\b",
    re.IGNORECASE,
)

#: Deficiency attributions. REDACTED.
#:
#: This is the thing the instrument must never produce. "The post assumes a
#: procedural vote is a policy vote" is a statement about a sentence. "The
#: poster doesn't understand how votes work" is a statement about a person, and
#: a corpus of those is a corpus of insults with a taxonomy attached. The
#: distinction is the entire ethical basis for the tool, so it is enforced in
#: code and not requested in a prompt.
DEFICIENCY_PATTERN = re.compile(
    r"\b(?:"
    r"do(?:es)?n'?t\s+(?:understand|realize|realise|know|grasp|get)"
    r"|do(?:es)?\s+not\s+(?:understand|realize|realise|know|grasp)"
    r"|misunderstand(?:s|ing)?"
    r"|is\s+(?:misinformed|uninformed|ignorant|confused|mistaken|wrong)"
    r"|lack(?:s|ing)?\s+(?:understanding|knowledge|awareness)"
    r"|fail(?:s|ed|ing)?\s+to\s+(?:understand|grasp|realize|realise)"
    r"|(?:is\s+)?unaware\s+(?:of|that)"
    r"|has\s+no\s+(?:idea|understanding)"
    r"|been\s+(?:misled|duped|fooled)"
    r"|is\s+(?:gullible|naive|ill-informed)"
    r")\b",
    re.IGNORECASE,
)
DEFICIENCY_REPLACEMENT = "[removed: this tool does not describe people]"

#: Bare author references. REWRITTEN to "the post".
#:
#: Redaction is wrong here. "That is not the poster's fault" is a sentence about
#: a person, but deleting it destroys a true and useful observation. Redirecting
#: the subject keeps the sense and drops the person: "that is not the post's
#: fault". The possessive is handled first so "the poster's" does not become
#: "the post's" by accident of ordering.
AUTHOR_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bthe\s+(?:poster|author|writer|commenter|user)'s\b", re.I), "the post's"),
    (re.compile(r"\bthe\s+(?:poster|author|writer|commenter|user)\b", re.I), "the post"),
    (re.compile(r"\bOP'?s\b"), "the post's"),
    (re.compile(r"\bOP\b"), "the post"),
    (re.compile(r"\bthis\s+(?:person|individual|guy|woman|man)\b", re.I), "this post"),
    (re.compile(r"\bwhoever\s+(?:wrote|posted)\s+this\b", re.I), "this post"),
)


@dataclass(slots=True)
class LintCounts:
    """How many times each lint fired. Reported, never suppressed.

    These are quality telemetry about the *tool*, not about the corpus. A
    corpus run whose citation count climbs is a run whose prompt has started
    hallucinating references, and the number surfaces that before a report does.
    """

    citations_redacted: int = 0
    deficiency_redacted: int = 0
    author_rewritten: int = 0
    verdict_flagged: int = 0
    codes_coerced: int = 0
    topics_coerced: int = 0

    def add(self, other: LintCounts) -> None:
        self.citations_redacted += other.citations_redacted
        self.deficiency_redacted += other.deficiency_redacted
        self.author_rewritten += other.author_rewritten
        self.verdict_flagged += other.verdict_flagged
        self.codes_coerced += other.codes_coerced
        self.topics_coerced += other.topics_coerced

    @property
    def total(self) -> int:
        return (
            self.citations_redacted + self.deficiency_redacted
            + self.author_rewritten + self.verdict_flagged
            + self.codes_coerced + self.topics_coerced
        )


def scrub(text: str, counts: LintCounts) -> str:
    """Apply every text lint, in the one order that is correct.

    Order is load-bearing and is therefore fixed here rather than left to the
    caller:

    1. **Citations first.** A URL can contain words that later patterns would
       match ("...whitehouse.gov/does-not-understand"), and rewriting inside a
       URL would corrupt it into something that still looks like a link.
    2. **Deficiency next.** These phrases frequently sit adjacent to an author
       reference ("the poster doesn't understand"). Removing the predicate
       before redirecting the subject leaves "the post [removed: ...]", which
       reads as what it is. The reverse order leaves a redaction marker with a
       dangling subject.
    3. **Author references last**, on text that no longer contains anything the
       earlier passes would have wanted.

    ``counts`` is mutated rather than returned so a caller can accumulate across
    every field of a reading without threading a tuple through six call sites.
    """
    def _sub(pattern: re.Pattern[str], replacement: str, value: str) -> tuple[str, int]:
        result, n = pattern.subn(replacement, value)
        return result, n

    for pattern in CITATION_PATTERNS:
        text, hits = _sub(pattern, CITATION_REPLACEMENT, text)
        counts.citations_redacted += hits

    text, hits = _sub(DEFICIENCY_PATTERN, DEFICIENCY_REPLACEMENT, text)
    counts.deficiency_redacted += hits

    for pattern, replacement in AUTHOR_REWRITES:
        text, hits = _sub(pattern, replacement, text)
        counts.author_rewritten += hits

    # Flag only. Counted on the FINAL text so redacted spans are not counted --
    # the replacement strings deliberately contain no verdict vocabulary.
    counts.verdict_flagged += len(VERDICT_PATTERN.findall(text))

    # Collapse whitespace left behind by redaction so records stay readable.
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def normalise_code(raw: str, counts: LintCounts) -> tuple[str, bool]:
    """Map a model-supplied gap code onto the taxonomy.

    Returns ``(code, was_coerced)``. Lowercase and spaced variants are accepted
    because they are transcription noise rather than a different answer;
    anything genuinely off-taxonomy becomes OTHER and is flagged, which keeps
    the counts sound and makes an underspecified taxonomy visible as a rising
    OTHER rate instead of as a quietly dropped observation.
    """
    candidate = (raw or "").strip().upper().replace(" ", "_").replace("-", "_")
    if candidate in GAP_CODES:
        return candidate, False
    counts.codes_coerced += 1
    return OTHER, True


def normalise_topic(raw: str, counts: LintCounts) -> str:
    """Map a model-supplied topic onto the closed list, or 'other'."""
    candidate = (raw or "").strip().lower().replace(" ", "-").replace("_", "-")
    if candidate in TOPICS:
        return candidate
    counts.topics_coerced += 1
    return "other"


def normalise_confidence(raw: str) -> str:
    """Map a confidence label onto the closed list, defaulting to the weakest.

    Defaulting DOWN is deliberate. An unparseable confidence is an absence of
    information, and treating an absence as 'high' would let malformed output
    strengthen a finding.
    """
    candidate = (raw or "").strip().lower()
    return candidate if candidate in CONFIDENCE_LEVELS else "low"


# --------------------------------------------------------------------------
# 3. Records -- what one reading produces
# --------------------------------------------------------------------------
# Deliberately absent from every structure below: any author identity. Not a
# name, not a handle, not a user id, not a profile URL, not a display name. The
# only provenance a record carries is platform and date, which are properties of
# where the language was found rather than of who produced it.


@dataclass(slots=True)
class Gap:
    """One comprehension gap found in one post.

    ``phrase`` is the span of the post that carries the assumption; quoting it
    is what makes a record checkable by a human reading the corpus later.
    ``how_this_generally_works`` is the plain-language correction, held to the
    same eighth-grade standard the tool requires elsewhere -- and it is a
    statement about the mechanism, never about the person.
    """

    code: str
    phrase: str
    how_this_generally_works: str
    confidence: str = "low"
    coerced: bool = False
    #: Set by the consensus pass when a gap survived only one run of N.
    consensus_note: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Reading:
    """The record for a single post. An intermediate artifact, not a product.

    ``in_scope`` is false for a post that is not making a political claim about
    a mechanism at all -- a slogan, a photo caption, "lol". Out-of-scope posts
    are kept in the records but **excluded from the denominator** in the report,
    because a gap rate computed over slogans measures the corpus's noise level
    rather than anything about political language.
    """

    text: str
    in_scope: bool
    topic: str
    gaps: list[Gap] = field(default_factory=list)
    platform: str = "unknown"
    observed_date: str = ""
    thread_id: str = ""
    position: int = 0
    role: str = "standalone"        # standalone | parent | comment
    runs: int = 1
    lints: LintCounts = field(default_factory=LintCounts)
    error: str = ""
    tool: str = TOOL_VERSION
    taxonomy: str = TAXONOMY_VERSION

    @property
    def codes(self) -> list[str]:
        return [gap.code for gap in self.gaps]

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["lints"] = asdict(self.lints)
        payload["gaps"] = [gap.to_json() for gap in self.gaps]
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Reading:
        lints = LintCounts(**payload.get("lints", {}))
        gaps = [Gap(**gap) for gap in payload.get("gaps", [])]
        known = {
            key: value for key, value in payload.items()
            if key not in {"lints", "gaps"}
        }
        known.pop("tool", None)
        known.pop("taxonomy", None)
        return cls(
            **known, lints=lints, gaps=gaps,
            tool=payload.get("tool", TOOL_VERSION),
            taxonomy=payload.get("taxonomy", TAXONOMY_VERSION),
        )


# --------------------------------------------------------------------------
# 4. The prompt
# --------------------------------------------------------------------------
# Embedded here for now. Doc 21 s8 item 3 records that it should move to a
# versioned file so reports can be compared across prompt versions; until it
# does, PROMPT_VERSION below is bumped by hand whenever the text changes, so a
# report at least records WHICH prompt produced it.

#: Bumped whenever the prompt text below changes, and recorded in every report,
#: so two reports can be compared only when they were produced the same way.
#:
#: 0.5.0 -- added the hard length limits. Small instruct models ignore a soft
#: "two or three sentences" and will happily write six paragraphs of civics,
#: which is not better output: it blows the token ceiling, truncates the JSON,
#: and turns a reading into a counted failure. Measured on a 1.5B model, the
#: soft instruction produced a 67% failure rate at a 512-token cap. The limits
#: are stated in words, with an explicit stop instruction, because that is what
#: small models actually obey.
PROMPT_VERSION = "levelset-prompt/0.5.0"

_TAXONOMY_BLOCK = "\n".join(
    f"- {code}: {description}" for code, description in GAP_CODES.items()
)

SYSTEM_PROMPT = f"""\
You are a language-measurement instrument, not a fact checker. You have no
sources and you may not adjudicate anything.

You read one social media post about politics and record the COMPREHENSION GAPS
its wording carries: places where the phrasing requires an assumption about how
a government process works that does not match how it actually works.

Absolute rules:
1. You describe SENTENCES, never people. Never write that someone does not
   understand something, is misinformed, or has been misled. Write what the
   wording assumes.
2. You emit NO citations, NO URLs, NO statute or bill numbers, and no roll call
   numbers -- not even correct ones. You cannot verify a source, so you cite
   none.
3. You render NO verdict. Do not say a claim is true, false, misleading or
   debunked. Whether the claim is correct is not what is being measured.
4. You record NO author identity of any kind.
5. If the post makes no claim about a political mechanism, set in_scope to
   false and return no gaps. A slogan or an expression of feeling is not a gap.

Every gap must use one of these codes exactly:
{_TAXONOMY_BLOCK}

Topic must be one of: {", ".join(TOPICS)}.
Confidence must be one of: {", ".join(CONFIDENCE_LEVELS)}.

LENGTH LIMITS -- these are hard, not stylistic:
- At most 3 gaps. Record the clearest ones; do not pad the list.
- "phrase" is a SHORT quoted span from the post, at most 15 words.
- "how_this_generally_works" is AT MOST 40 WORDS. One or two sentences about
  the mechanism, at an eighth-grade reading level. Do not add history, do not
  add examples, do not restate the post, and do not explain your explanation.
- Stop as soon as the JSON object is closed. Write nothing after it.

Reply with JSON only, in this shape:
{{"in_scope": true, "topic": "<topic>", "gaps": [
  {{"code": "<CODE>", "phrase": "<short quoted span, <=15 words>",
    "how_this_generally_works": "<how the process actually works, <=40 words>",
    "confidence": "<confidence>"}}
]}}
"""


def build_user_prompt(text: str, parent: str | None = None) -> str:
    """The user turn: the post, plus its parent when it is a comment.

    A reply's meaning depends on what it replies to. "Same here, and mine went
    up too" carries no mechanism on its own and carries the parent's mechanism
    when read with it, so a comment read without its parent is systematically
    under-coded -- which would bias every thread in the corpus in the same
    direction.
    """
    if parent:
        return (
            "This is a COMMENT on the post below. Read the comment, using the "
            "parent only as context for what it refers to. Record gaps carried "
            "by the COMMENT's wording, not the parent's.\n\n"
            f"--- parent post ---\n{parent}\n\n--- comment ---\n{text}"
        )
    return f"--- post ---\n{text}"


# --------------------------------------------------------------------------
# 5. Backends
# --------------------------------------------------------------------------
# Two, both over the standard library, plus the far more important third option:
# a caller-supplied callable. That is what the test suite and the symmetry gate
# use, and it is why every function below takes ``complete`` as a parameter
# instead of reaching for a global client.

Completer = Callable[[str, str], str]


class LevelsetError(RuntimeError):
    """A failure this tool can describe. Never raised for a single bad post."""


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: float) -> dict[str, Any]:
    """One JSON POST over urllib. No third-party HTTP client, by policy."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    if not url.startswith(("http://", "https://")):
        raise LevelsetError(f"refusing non-HTTP backend url: {url!r}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


#: Hard ceiling on generated tokens per reading.
#:
#: NOT a performance knob -- a correctness one. A reading is a small JSON
#: object: a topic, one to three codes, and a two-or-three sentence mechanism
#: note. Nothing legitimate needs more than a few hundred tokens.
#:
#: Left uncapped, a small instruct model asked for JSON sometimes never stops --
#: it explains the mechanism, then explains its explanation, and one call runs
#: for minutes. Measured here on a 1.5B model: a single uncapped call passed
#: 2,300 generated tokens without finishing, and a 64-call symmetry run would
#: not have completed in hours.
#:
#: The wall clock is the smaller problem. An unbounded call has no failure mode
#: a corpus run can REPORT -- it does not error, it hangs, and a batch job that
#: hangs on post 4,000 of 10,000 is a batch job nobody finishes. With a cap, an
#: over-long generation is truncated into unparseable JSON, which the runner
#: already treats as a counted, reported, skipped failure. A visible failure is
#: strictly better than an invisible stall.
DEFAULT_MAX_TOKENS = 512

#: Context window, set explicitly rather than inherited. A post, its parent and
#: the taxonomy block fit well inside this, and a smaller window is dramatically
#: faster on CPU. Pinned so a reading does not quietly change meaning when a
#: backend changes its own default.
DEFAULT_NUM_CTX = 4096


def ollama_completer(model: str, *, host: str = "http://127.0.0.1:11434",
                     temperature: float = 0.0, seed: int = 42,
                     timeout: float = 120.0,
                     max_tokens: int = DEFAULT_MAX_TOKENS,
                     num_ctx: int = DEFAULT_NUM_CTX) -> Completer:
    """Local Ollama. The default, because a corpus run should cost nothing."""
    def complete(system: str, user: str) -> str:
        payload = {
            "model": model, "stream": False, "format": "json",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                "temperature": temperature, "seed": seed,
                "num_predict": max_tokens, "num_ctx": num_ctx,
            },
        }
        data = _post_json(f"{host}/api/chat", payload, {}, timeout)
        return data.get("message", {}).get("content", "")
    return complete


def openai_completer(model: str, *, base_url: str = "https://api.openai.com/v1",
                     temperature: float = 0.0, seed: int = 42,
                     timeout: float = 120.0,
                     max_tokens: int = DEFAULT_MAX_TOKENS) -> Completer:
    """OpenAI-compatible chat completions, for corpora too large for a laptop."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise LevelsetError("OPENAI_API_KEY is not set")

    def complete(system: str, user: str) -> str:
        payload = {
            "model": model, "temperature": temperature, "seed": seed,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        data = _post_json(
            f"{base_url}/chat/completions", payload,
            {"Authorization": f"Bearer {key}"}, timeout,
        )
        return data["choices"][0]["message"]["content"]
    return complete


# --------------------------------------------------------------------------
# 6. Reading one post
# --------------------------------------------------------------------------


def _parse_reply(raw: str) -> dict[str, Any]:
    """Pull the JSON object out of a model reply.

    Tolerant of a fenced block or leading prose, because a malformed wrapper is
    a formatting failure rather than an analytical one and discarding the
    reading over it would bias the corpus toward whatever the model happens to
    format cleanly. Genuinely unparseable output raises, and the caller counts
    it as a failure and moves on.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LevelsetError(f"no JSON object in reply: {text[:120]!r}") from None
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LevelsetError(f"unparseable JSON in reply: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LevelsetError(f"reply is {type(parsed).__name__}, expected an object")
    return parsed


def _single_pass(text: str, parent: str | None, complete: Completer,
                 counts: LintCounts) -> tuple[bool, str, list[Gap]]:
    """One model call, fully linted. Returns ``(in_scope, topic, gaps)``."""
    parsed = _parse_reply(complete(SYSTEM_PROMPT, build_user_prompt(text, parent)))

    in_scope = bool(parsed.get("in_scope", True))
    topic = normalise_topic(str(parsed.get("topic", "")), counts)

    gaps: list[Gap] = []
    raw_gaps = parsed.get("gaps") or []
    if not isinstance(raw_gaps, list):
        raw_gaps = []
    for entry in raw_gaps:
        if not isinstance(entry, dict):
            continue
        code, coerced = normalise_code(str(entry.get("code", "")), counts)
        gaps.append(Gap(
            code=code,
            # The quoted span is scrubbed too. A post that itself contains a URL
            # would otherwise smuggle one into the record through the quote,
            # which is the same unverified citation by a different route.
            phrase=scrub(str(entry.get("phrase", "")), counts),
            how_this_generally_works=scrub(
                str(entry.get("how_this_generally_works", "")), counts),
            confidence=normalise_confidence(str(entry.get("confidence", ""))),
            coerced=coerced,
        ))

    # One code, once. A model that lists VOTE_TYPE three times for one post has
    # found one gap and described it three ways; counting it three times would
    # let verbosity move the aggregate.
    deduped: dict[str, Gap] = {}
    for gap in gaps:
        if gap.code not in deduped:
            deduped[gap.code] = gap
    return in_scope, topic, list(deduped.values())


def read_post(
    text: str, *, complete: Completer, parent: str | None = None,
    platform: str = "unknown", observed_date: str = "", thread_id: str = "",
    position: int = 0, role: str = "standalone", consensus: int = 1,
) -> Reading:
    """Read one post, optionally across ``consensus`` independent passes.

    THE CONSENSUS RULE (doc 21 s5)
    ------------------------------
    A gap that survives only ONE run of N is demoted to low confidence, KEPT,
    and annotated. Not dropped.

    Dropping it would be the wrong trade for this instrument. A single-run gap
    is weak evidence, but the aggregate is the product, and systematically
    discarding weak observations biases every count downward by an amount that
    varies with how ambiguous a topic is -- which is exactly the kind of
    invisible, topic-correlated distortion that would make a published finding
    wrong in a way no reader could detect. Keeping it with its weakness recorded
    lets the report show the confidence distribution and lets a reader discount.
    """
    counts = LintCounts()
    reading = Reading(
        text=text, in_scope=True, topic="other", platform=platform,
        observed_date=observed_date or datetime.now(UTC).date().isoformat(),
        thread_id=thread_id, position=position, role=role,
        runs=max(1, consensus), lints=counts,
    )

    passes = max(1, consensus)
    seen: dict[str, list[Gap]] = defaultdict(list)
    scopes: list[bool] = []
    topics: list[str] = []

    for _ in range(passes):
        in_scope, topic, gaps = _single_pass(text, parent, complete, counts)
        scopes.append(in_scope)
        topics.append(topic)
        for gap in gaps:
            seen[gap.code].append(gap)

    # Majority scope; ties resolve to in-scope, because excluding a post shrinks
    # the denominator and a tie is not grounds for that.
    reading.in_scope = sum(scopes) * 2 >= len(scopes)
    reading.topic = Counter(topics).most_common(1)[0][0] if topics else "other"

    for occurrences in seen.values():
        # Keep the occurrence with the strongest confidence as the representative
        # text, then apply the survival rule to the result.
        best = max(occurrences, key=lambda g: CONFIDENCE_LEVELS.index(g.confidence))
        survived = len(occurrences)
        if passes > 1 and survived == 1:
            best.confidence = "low"
            best.consensus_note = (
                f"appeared in 1 of {passes} runs; demoted to low confidence and "
                "kept rather than dropped, so the aggregate is not silently "
                "biased downward"
            )
        reading.gaps.append(best)

    reading.gaps.sort(key=lambda g: list(GAP_CODES).index(g.code))
    if not reading.in_scope:
        reading.gaps = []
    return reading


# --------------------------------------------------------------------------
# 7. Threads -- one-to-many (doc 21 s3)
# --------------------------------------------------------------------------

DEFAULT_DELIMITER = "---"


def split_thread(raw: str, delimiter: str = DEFAULT_DELIMITER) -> list[str]:
    """Split a thread file into entries. Entry 0 is the parent.

    Two things this gets right that a naive ``raw.split(delim)`` does not, both
    because a phantom entry is a phantom observation that lands in a published
    count:

    * **Empty and doubled delimiters produce no entries.** A file that starts or
      ends with a separator, or has two in a row, is a formatting artifact.
    * **Blank lines INSIDE an entry are preserved.** A multi-paragraph post is
      one post; splitting on blank lines would shatter it into fragments that
      each get read and counted separately.

    The delimiter must be alone on its line, so a post containing an em-dash or
    a horizontal rule in its own text does not split itself.
    """
    pattern = re.compile(rf"^[ \t]*{re.escape(delimiter)}[ \t]*$", re.MULTILINE)
    return [chunk.strip() for chunk in pattern.split(raw) if chunk.strip()]


def read_thread(
    raw: str, *, complete: Completer, thread_id: str,
    delimiter: str = DEFAULT_DELIMITER, platform: str = "unknown",
    observed_date: str = "", consensus: int = 1,
) -> list[Reading]:
    """Read a parent and its comments as ONE unit.

    Entry 0 is read with no parent context. Every later entry is read WITH the
    parent supplied, because a reply's meaning depends on what it replies to.
    """
    entries = split_thread(raw, delimiter)
    if not entries:
        return []

    parent_text = entries[0]
    readings = [read_post(
        parent_text, complete=complete, platform=platform,
        observed_date=observed_date, thread_id=thread_id, position=0,
        role="parent", consensus=consensus,
    )]
    for index, entry in enumerate(entries[1:], start=1):
        readings.append(read_post(
            entry, complete=complete, parent=parent_text, platform=platform,
            observed_date=observed_date, thread_id=thread_id, position=index,
            role="comment", consensus=consensus,
        ))
    return readings


# --------------------------------------------------------------------------
# 8. Aggregation -- the actual product
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CodeCount:
    """One gap code's frequency, both ways it can honestly be counted."""

    code: str
    raw: int = 0            # every occurrence, once per post
    thr: int = 0            # at most once per thread
    threads: set[str] = field(default_factory=set)

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "raw": self.raw, "thr": self.thr}


@dataclass(slots=True)
class Report:
    """A corpus-level finding. The only thing this tool produces that matters."""

    posts: int = 0
    in_scope: int = 0
    out_of_scope: int = 0
    threads: int = 0
    failures: int = 0
    codes: list[CodeCount] = field(default_factory=list)
    co_occurrence: list[tuple[str, str, int]] = field(default_factory=list)
    topics: dict[str, int] = field(default_factory=dict)
    confidence: dict[str, int] = field(default_factory=dict)
    lints: LintCounts = field(default_factory=LintCounts)
    coerced_gaps: int = 0
    tool: str = TOOL_VERSION
    taxonomy: str = TAXONOMY_VERSION
    prompt: str = PROMPT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool, "taxonomy": self.taxonomy, "prompt": self.prompt,
            "posts": self.posts, "in_scope": self.in_scope,
            "out_of_scope": self.out_of_scope, "threads": self.threads,
            "failures": self.failures,
            "codes": [c.to_json() for c in self.codes],
            "co_occurrence": [
                {"a": a, "b": b, "threads": n} for a, b, n in self.co_occurrence
            ],
            "topics": self.topics, "confidence": self.confidence,
            "lints": asdict(self.lints), "coerced_gaps": self.coerced_gaps,
        }


def aggregate(readings: Sequence[Reading]) -> Report:
    """Turn a pile of readings into the corpus finding.

    THREAD WEIGHTING IS THE STATISTICAL CORE (doc 21 s3)
    ----------------------------------------------------
    Every code is counted twice:

    * ``raw`` -- every occurrence, once per post. What the corpus literally
      contains.
    * ``thr`` -- each code counted **at most once per thread**.

    A parent's error echoed by four hundred replies is ONE misunderstanding that
    spread, not four hundred independent observations. Raw counts alone would
    let a single viral post decide the entire finding, and the more viral the
    post the more it would decide -- so the headline number is the thread count.

    Standalone posts each get their own synthetic thread id, so a corpus of
    unrelated posts yields ``raw == thr`` and nothing is distorted by machinery
    that was not needed.

    OUT-OF-SCOPE POSTS LEAVE THE DENOMINATOR
    ----------------------------------------
    A post that makes no claim about a mechanism cannot carry a gap about one.
    Leaving slogans in the denominator would make every rate a function of how
    much noise the collection method swept up, so ``in_scope`` is the
    denominator and both numbers are printed.
    """
    report = Report()
    counters: dict[str, CodeCount] = {}
    co_threads: dict[tuple[str, str], set[str]] = defaultdict(set)
    topics: Counter[str] = Counter()
    confidence: Counter[str] = Counter()
    thread_ids: set[str] = set()

    for index, reading in enumerate(readings):
        report.posts += 1
        report.lints.add(reading.lints)
        if reading.error:
            report.failures += 1
            continue

        # A standalone post is its own thread. Synthesised from position and
        # index rather than from text, so two identical standalone posts stay
        # two observations.
        thread = reading.thread_id or f"__standalone__{index}"
        thread_ids.add(thread)

        if not reading.in_scope:
            report.out_of_scope += 1
            continue
        report.in_scope += 1
        topics[reading.topic] += 1

        for gap in reading.gaps:
            counter = counters.setdefault(gap.code, CodeCount(code=gap.code))
            counter.raw += 1
            counter.threads.add(thread)
            confidence[gap.confidence] += 1
            if gap.coerced:
                report.coerced_gaps += 1

        # Co-occurrence is thread-weighted for the same reason the headline
        # count is: a pair that appears together in one viral thread is one
        # observation of that combination.
        for first, second in combinations(sorted({g.code for g in reading.gaps}), 2):
            co_threads[(first, second)].add(thread)

    for counter in counters.values():
        counter.thr = len(counter.threads)

    report.threads = len(thread_ids)
    report.codes = sorted(
        counters.values(),
        key=lambda c: (-c.thr, -c.raw, list(GAP_CODES).index(c.code)),
    )
    report.co_occurrence = sorted(
        ((a, b, len(threads)) for (a, b), threads in co_threads.items()),
        key=lambda row: (-row[2], row[0], row[1]),
    )
    report.topics = dict(topics.most_common())
    report.confidence = {
        level: confidence.get(level, 0) for level in reversed(CONFIDENCE_LEVELS)
    }
    return report


def render_report(report: Report) -> str:
    """The human-readable aggregate. Thread count first, because it is the one."""
    out: list[str] = [
        f"{report.tool} · {report.taxonomy} · {report.prompt}",
        "",
        f"{report.posts} post(s) across {report.threads} thread(s)",
        f"  in scope     {report.in_scope}",
        f"  out of scope {report.out_of_scope}  (excluded from the denominator)",
    ]
    if report.failures:
        out.append(f"  failed       {report.failures}  (skipped; the run continued)")
    out += ["", "comprehension gaps", ""]

    if not report.codes:
        out.append("  none recorded")
    else:
        out.append(f"  {'code':<20} {'thr':>5} {'raw':>5}   {'% of in-scope':>13}")
        out.append(f"  {'-' * 20} {'-' * 5} {'-' * 5}   {'-' * 13}")
        denominator = max(1, report.in_scope)
        for counter in report.codes:
            share = 100.0 * counter.thr / denominator
            out.append(
                f"  {counter.code:<20} {counter.thr:>5} {counter.raw:>5}   {share:>12.1f}%"
            )
        out += [
            "",
            "  thr = counted at most once per thread. THIS IS THE HEADLINE NUMBER:",
            "  one parent's error echoed by 400 replies is one misunderstanding",
            "  that spread, not 400 independent observations.",
        ]

    if report.co_occurrence:
        out += ["", "gaps that travel together (threads)", ""]
        for first, second, count in report.co_occurrence[:10]:
            out.append(f"  {first:<20} + {second:<20} {count:>4}")

    if report.topics:
        out += ["", "topics", ""]
        for topic, count in report.topics.items():
            out.append(f"  {topic:<20} {count:>4}")

    out += ["", "confidence", ""]
    for level, count in report.confidence.items():
        out.append(f"  {level:<20} {count:>4}")

    lints = report.lints
    out += [
        "",
        "lints (the tool checking itself)",
        "",
        (f"  citations redacted    {lints.citations_redacted:>4}   "
         "including correct ones: this tool verifies nothing, so it cites nothing"),
        (f"  deficiency redacted   {lints.deficiency_redacted:>4}   "
         "statements about people, removed"),
        f"  author refs rewritten {lints.author_rewritten:>4}   redirected to 'the post'",
        (f"  verdict words flagged {lints.verdict_flagged:>4}   "
         "not removed; discount the readings accordingly"),
        (f"  codes coerced         {lints.codes_coerced:>4}   "
         f"off-taxonomy, forced to {OTHER}"),
        f"  topics coerced        {lints.topics_coerced:>4}",
        "",
        "NOT PUBLISHABLE as a finding until this corpus clears the",
        "symmetry gate: scripts/check_levelset_symmetry.py, BOTH halves --",
        "the code half (--run) and the model half (--run --backend). A gap",
        "report that leans one way would do more damage than no report.",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------
# 9. Corpus runs
# --------------------------------------------------------------------------


def run_corpus(
    paths: Sequence[Path], *, complete: Completer, threaded: bool = False,
    delimiter: str = DEFAULT_DELIMITER, platform: str = "unknown",
    observed_date: str = "", consensus: int = 1,
    records_dir: Path | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[Reading]:
    """Read every file in a corpus, surviving individual failures.

    A corpus run that aborts on the first malformed model response is a corpus
    run that cannot be completed, because at ten thousand posts a malformed
    response is a certainty rather than a risk. So a failure is recorded on the
    reading, counted in the report, and the run continues -- and the failure
    count is printed, because a run that quietly dropped nine hundred posts and
    reported on the rest would be worse than one that stopped.

    ``thread_id`` is derived from the FILE STEM, never from anything in the
    post. A thread id built from content would be a content hash, and a content
    hash of a social media post is a join key against the original -- which is
    author identity by a slower route.
    """
    readings: list[Reading] = []
    for path in paths:
        stem = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            readings.append(Reading(
                text="", in_scope=False, topic="other", platform=platform,
                thread_id=stem, error=f"unreadable: {exc}",
            ))
            continue

        try:
            if threaded:
                produced = read_thread(
                    raw, complete=complete, thread_id=stem, delimiter=delimiter,
                    platform=platform, observed_date=observed_date,
                    consensus=consensus,
                )
            else:
                produced = [read_post(
                    raw.strip(), complete=complete, platform=platform,
                    observed_date=observed_date, thread_id="", position=0,
                    role="standalone", consensus=consensus,
                )]
        except LevelsetError as exc:
            produced = [Reading(
                text=raw.strip()[:400], in_scope=False, topic="other",
                platform=platform, thread_id=stem if threaded else "",
                error=str(exc),
            )]
        except Exception as exc:  # one bad post must not end the run
            produced = [Reading(
                text=raw.strip()[:400], in_scope=False, topic="other",
                platform=platform, thread_id=stem if threaded else "",
                error=f"{type(exc).__name__}: {exc}",
            )]

        readings.extend(produced)
        if records_dir is not None:
            records_dir.mkdir(parents=True, exist_ok=True)
            for reading in produced:
                name = f"{stem}.{reading.position:04d}.json" if threaded else f"{stem}.json"
                (records_dir / name).write_text(
                    json.dumps(reading.to_json(), indent=2) + "\n", encoding="utf-8",
                )
        if on_progress:
            failed = sum(1 for r in produced if r.error)
            on_progress(f"  {stem}: {len(produced)} reading(s), {failed} failure(s)")
    return readings


def load_records(patterns: Sequence[str]) -> list[Reading]:
    """Read previously written records back for aggregation."""
    readings: list[Reading] = []
    for pattern in patterns:
        for name in sorted(globlib.glob(pattern)):  # CLI glob string
            payload = json.loads(Path(name).read_text(encoding="utf-8"))
            readings.append(Reading.from_json(payload))
    return readings


# --------------------------------------------------------------------------
# 10. CLI
# --------------------------------------------------------------------------


def render_taxonomy() -> str:
    out = [f"{TAXONOMY_VERSION}  ({len(GAP_CODES)} codes)", ""]
    for group, codes in GAP_GROUPS.items():
        out.append(group)
        for code, description in codes.items():
            wrapped = re.sub(r"\s+", " ", description)
            out.append(f"  {code:<20} {wrapped}")
        out.append("")
    out += [
        "Adding a code is a minor version bump. Changing what a code MEANS is a",
        "major bump: the counts would still line up and would no longer be",
        "comparable to any report produced before the change.",
    ]
    return "\n".join(out)


def render_reading(reading: Reading) -> str:
    out = [f"{reading.tool} · {reading.taxonomy}", ""]
    if reading.error:
        out.append(f"FAILED: {reading.error}")
        return "\n".join(out)
    if not reading.in_scope:
        out.append("out of scope: no claim about a political mechanism")
        return "\n".join(out)

    out.append(f"topic: {reading.topic}")
    if reading.runs > 1:
        out.append(f"consensus: {reading.runs} runs")
    out.append("")
    if not reading.gaps:
        out.append("no comprehension gaps recorded")
    for gap in reading.gaps:
        out.append(f"{gap.code}  ({gap.confidence})")
        out.append(f"  in the post : {gap.phrase}")
        out.append(f"  generally   : {gap.how_this_generally_works}")
        if gap.consensus_note:
            out.append(f"  note        : {gap.consensus_note}")
        out.append("")
    if reading.lints.total:
        out.append(f"lints fired: {reading.lints.total}")
    return "\n".join(out).rstrip()


def _completer_from_args(args: argparse.Namespace) -> Completer:
    if args.backend == "openai":
        return openai_completer(
            args.model, temperature=args.temperature, seed=args.seed,
            max_tokens=args.max_tokens)
    return ollama_completer(
        args.model, host=args.host, temperature=args.temperature,
        seed=args.seed, max_tokens=args.max_tokens)


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="levelset",
        description=(
            "Measure comprehension gaps in political posts. A research "
            "instrument, not a fact checker: it has no sources, issues no "
            "verdicts, emits no citations, and records no author identity."
        ),
        epilog=(
            "The unit of analysis is the CORPUS. A single reading is an "
            "intermediate record. No report is publishable as a finding "
            "until it clears scripts/check_levelset_symmetry.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--taxonomy", action="store_true",
                        help="print the gap code list and exit")
    parser.add_argument("--version", action="store_true",
                        help="print tool and taxonomy versions and exit")

    source = parser.add_argument_group("input (choose one)")
    source.add_argument("-t", "--text", help="one post, inline")
    source.add_argument("-f", "--file", help="one post, from a file")
    source.add_argument("--thread", help="a parent post and its comments, one unit")
    source.add_argument("--corpus", help="glob of post files")
    source.add_argument("--report", help="glob of previously written records")
    # Deliberately NO -u/--url. A URL means a source exists, which means the
    # claim belongs in the Analyzer, where it can be adjudicated against that
    # source. This tool is for posts with content and no reference.

    run = parser.add_argument_group("run")
    run.add_argument("--thread-glob", action="store_true",
                     help="treat every --corpus file as a thread, not one post")
    run.add_argument("--records-dir", help="write one JSON record per reading")
    run.add_argument("--platform", default="unknown",
                     help="where the language was found (NOT who said it)")
    run.add_argument("--date", default="", help="observation date, ISO 8601")
    run.add_argument("--delim", default=DEFAULT_DELIMITER,
                     help=f"thread entry separator (default: a line of {DEFAULT_DELIMITER!r})")
    run.add_argument("--consensus", type=int, default=1, metavar="N",
                     help="read each post N times; a gap surviving 1 of N is "
                          "demoted to low confidence and KEPT, never dropped")

    model = parser.add_argument_group("model")
    model.add_argument("--backend", choices=("ollama", "openai"), default="ollama")
    model.add_argument("--model", default="llama3.1:8b")
    model.add_argument("--host", default="http://127.0.0.1:11434")
    model.add_argument("--temperature", type=float, default=0.0)
    model.add_argument("--seed", type=int, default=42)
    model.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                       help="ceiling on generated tokens per reading; a reading "
                            "that hits it is truncated and counted as a reported "
                            "failure, which is what an unbounded call cannot be")

    out = parser.add_argument_group("output")
    out.add_argument("--format", choices=("text", "json"), default="text")
    out.add_argument("--out", help="write to a file instead of stdout")
    return parser


def main(argv: Sequence[str] | None = None, *,
         complete: Completer | None = None) -> int:
    """Entry point. ``complete`` is injectable so tests need no model."""
    args = build_parser().parse_args(argv)

    if args.version:
        print(f"{TOOL_VERSION}  {TAXONOMY_VERSION}  {PROMPT_VERSION}")
        return 0
    if args.taxonomy:
        _emit(
            json.dumps(
                {"taxonomy": TAXONOMY_VERSION, "codes": GAP_CODES}, indent=2)
            if args.format == "json" else render_taxonomy(),
            args.out,
        )
        return 0

    if args.consensus < 1:
        print("--consensus must be at least 1", file=sys.stderr)
        return 2

    # --report needs no model: it aggregates records that already exist.
    if args.report:
        readings = load_records([args.report])
        if not readings:
            print(f"no records matched {args.report!r}", file=sys.stderr)
            return 2
        report = aggregate(readings)
        _emit(
            json.dumps(report.to_json(), indent=2)
            if args.format == "json" else render_report(report),
            args.out,
        )
        return 0

    chosen = [bool(args.text), bool(args.file), bool(args.thread), bool(args.corpus)]
    if sum(chosen) != 1:
        print("choose exactly one of -t, -f, --thread, --corpus, --report",
              file=sys.stderr)
        return 2

    try:
        completer = complete or _completer_from_args(args)
    except LevelsetError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    records_dir = Path(args.records_dir) if args.records_dir else None

    if args.corpus:
        paths = [Path(name) for name in sorted(globlib.glob(args.corpus))]
        if not paths:
            print(f"no files matched {args.corpus!r}", file=sys.stderr)
            return 2
        readings = run_corpus(
            paths, complete=completer, threaded=args.thread_glob,
            delimiter=args.delim, platform=args.platform,
            observed_date=args.date, consensus=args.consensus,
            records_dir=records_dir,
            on_progress=(lambda line: print(line, file=sys.stderr)),
        )
        report = aggregate(readings)
        _emit(
            json.dumps(report.to_json(), indent=2)
            if args.format == "json" else render_report(report),
            args.out,
        )
        return 0

    if args.thread:
        path = Path(args.thread)
        readings = read_thread(
            path.read_text(encoding="utf-8"), complete=completer,
            thread_id=path.stem, delimiter=args.delim, platform=args.platform,
            observed_date=args.date, consensus=args.consensus,
        )
        if records_dir is not None:
            records_dir.mkdir(parents=True, exist_ok=True)
            for reading in readings:
                (records_dir / f"{path.stem}.{reading.position:04d}.json").write_text(
                    json.dumps(reading.to_json(), indent=2) + "\n", encoding="utf-8")
        report = aggregate(readings)
        _emit(
            json.dumps(report.to_json(), indent=2)
            if args.format == "json" else render_report(report),
            args.out,
        )
        return 0

    text = args.text if args.text else Path(args.file).read_text(encoding="utf-8")
    try:
        reading = read_post(
            text.strip(), complete=completer, platform=args.platform,
            observed_date=args.date, consensus=args.consensus,
        )
    except LevelsetError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    if records_dir is not None:
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / "reading.json").write_text(
            json.dumps(reading.to_json(), indent=2) + "\n", encoding="utf-8")
    _emit(
        json.dumps(reading.to_json(), indent=2)
        if args.format == "json" else render_reading(reading),
        args.out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

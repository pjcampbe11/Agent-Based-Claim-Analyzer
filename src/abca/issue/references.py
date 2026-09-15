"""Supporting references, and the floor an issue must clear to be analyzed.

THE FLOOR IS THE FEATURE
========================
``abca issue`` will not run on a bare sentence. It requires references, it
requires at least one of them to be primary or official, and it refuses when a
majority of what was supplied is social media. Refuses, not warns -- a warning
that can be scrolled past is a floor made of paper.

The reason is narrow and worth stating precisely. A model given "crime is out of
control downtown" and nothing else will produce paragraphs of confident,
fluent, entirely unsourced policy analysis. It will be well written. It will
cite nothing. It is indistinguishable in form from the thing this tool exists
to be an alternative to, and the only reliable defense is to not have that code
path at all.

WHAT THE FLOOR IS NOT
=====================
It is not a quality judgment on the issue, and it is not a claim that three
sources make an analysis correct. It is a statement that below this line the
tool has nothing to work FROM, and would be generating rather than analyzing.
An issue that fails the floor is not rejected as wrong; it is returned with a
list of what is missing, which is a more useful answer than a confident essay.

WHY SOCIAL POSTS ARE ACCEPTED AT ALL
====================================
Because they are frequently the OBJECT of the analysis -- the claim circulating
is the thing being examined, and its URL is where it circulated. What they never
become is support for the claim being true. That distinction is held in code by
:attr:`~abca.schema.issue.ReferenceKind.is_evidence`, not by a prompt asking a
model to remember it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from abca.schema.enums import SourceTier
from abca.schema.issue import ReferenceKind
from abca.schema.plan import Reference

#: Separator between a reference's locator and the passage relied upon.
#: Two pipes because a single one appears in URLs and query strings, and a
#: separator that sometimes splits a URL in half is worse than no separator.
QUOTE_SEPARATOR = "||"

#: Absolute minimums. Configuration may make the floor STRICTER and may not
#: make it looser -- there is no supported way to reach zero.
#:
#: A configurable floor with no hard bottom is a floor that gets set to zero on
#: the first deadline, by someone with a good reason, and never gets set back.
HARD_MINIMUM_TOTAL = 2
HARD_MINIMUM_PRIMARY = 1


class ReferenceSpecError(ValueError):
    """A ``--ref`` argument that could not be parsed. Carries the fix."""


def parse_reference_spec(spec: str, *, index: int) -> Reference:
    """Parse one ``--ref`` value into an unresolved :class:`Reference`.

    Format::

        KIND:LOCATOR
        KIND:LOCATOR||QUOTE

    where KIND is one of primary, official, research, reporting, social::

        --ref primary:'28 CFR 545.11'
        --ref 'primary:28 CFR 545.11||Special Assessments imposed under 18 U.S.C. 3013'
        --ref official:https://www.cbo.gov/publication/61307
        --ref social:https://example.com/post/123

    The kind is required and unguessable on purpose. Making the submitter say
    what a source IS forces the one judgment that matters -- is this evidence,
    or is it the thing being examined -- to happen before the analysis rather
    than inside it.
    """
    raw = spec.strip()
    if not raw:
        raise ReferenceSpecError("empty --ref value")

    kind_text, separator, remainder = raw.partition(":")
    if not separator:
        raise ReferenceSpecError(
            f"--ref {raw!r} has no kind. Write KIND:LOCATOR, for example "
            f"'primary:28 CFR 545.11'. Valid kinds: "
            f"{', '.join(k.value for k in ReferenceKind)}."
        )

    try:
        kind = ReferenceKind(kind_text.strip().lower())
    except ValueError:
        raise ReferenceSpecError(
            f"--ref {raw!r}: {kind_text.strip()!r} is not a reference kind. "
            f"Valid kinds: {', '.join(k.value for k in ReferenceKind)}. "
            "The kind is a CEILING on how strong the source can be treated as, "
            "so choosing a weaker one is always safe."
        ) from None

    locator, _, quote = remainder.partition(QUOTE_SEPARATOR)
    locator = locator.strip()
    quote = quote.strip()
    if not locator:
        raise ReferenceSpecError(f"--ref {raw!r} has a kind but no locator.")

    return Reference(
        id=f"r-{index:03d}",
        declared_kind=kind,
        locator=locator,
        title=_title_for(locator),
        quote=quote or None,
    )


def _title_for(locator: str) -> str:
    """A readable name for a reference, derived from its locator.

    Deliberately dumb. A model asked to title a source will occasionally
    describe what it thinks the source says, and a title that editorializes is
    a citation that argues.
    """
    if locator.startswith(("http://", "https://")):
        parsed = urlparse(locator)
        host = parsed.netloc.removeprefix("www.")
        path = parsed.path.rstrip("/")
        tail = path.rsplit("/", 1)[-1] if path else ""
        return f"{host}{'/' + tail if tail else ''}"
    return re.sub(r"\s+", " ", locator)[:120]


# ==========================================================================
# The floor
# ==========================================================================


@dataclass(frozen=True, slots=True)
class EvidenceFloor:
    """The minimum supporting material an issue must carry to be analyzed."""

    minimum_total: int = 3
    minimum_primary: int = 1
    #: Largest share of references that may be non-evidentiary (social).
    #: At 0.5, social posts can never be more than half the record.
    maximum_social_share: float = 0.5

    def __post_init__(self) -> None:
        if self.minimum_total < HARD_MINIMUM_TOTAL:
            raise ValueError(
                f"minimum_total must be at least {HARD_MINIMUM_TOTAL}; got "
                f"{self.minimum_total}. The floor is configurable upward only."
            )
        if self.minimum_primary < HARD_MINIMUM_PRIMARY:
            raise ValueError(
                f"minimum_primary must be at least {HARD_MINIMUM_PRIMARY}; got "
                f"{self.minimum_primary}. An issue with no primary or official "
                "source has nothing for the Analyzer to check against."
            )
        if not 0.0 <= self.maximum_social_share <= 1.0:
            raise ValueError("maximum_social_share must be between 0 and 1")

    @classmethod
    def strict(cls) -> EvidenceFloor:
        """The floor for anything published under the tool's name."""
        return cls(minimum_total=5, minimum_primary=2, maximum_social_share=0.25)


@dataclass(frozen=True, slots=True)
class FloorResult:
    """Whether the floor was met, and precisely what is missing if not."""

    met: bool
    total: int
    primary_or_official: int
    evidentiary: int
    social: int
    social_share: float
    shortfalls: tuple[str, ...]

    def report(self) -> str:
        """Operator-facing text. Says what to add, not merely that it failed."""
        if self.met:
            return (
                f"Evidence floor met: {self.total} reference(s), "
                f"{self.primary_or_official} primary/official, "
                f"{self.evidentiary} usable as evidence."
            )
        lines = ["The evidence floor was not met. This issue was not analyzed.", ""]
        lines.extend(f"  - {shortfall}" for shortfall in self.shortfalls)
        lines += [
            "",
            (
                "This is a refusal, not an error. Analysis below this line would be "
                "generation dressed as analysis, which is the thing the Analyzer "
                "exists to detect in other people's work."
            ),
        ]
        return "\n".join(lines)


def check_floor(references: list[Reference], floor: EvidenceFloor) -> FloorResult:
    """Evaluate a reference set against a floor. Pure function, no I/O."""
    total = len(references)
    primary = sum(1 for r in references if r.declared_kind.counts_toward_primary_floor)
    evidentiary = sum(1 for r in references if r.is_evidence)
    social = sum(1 for r in references if r.declared_kind is ReferenceKind.SOCIAL)
    share = round(social / total, 4) if total else 0.0

    shortfalls: list[str] = []
    if total < floor.minimum_total:
        shortfalls.append(
            f"{total} reference(s) supplied; {floor.minimum_total} required. "
            f"Add {floor.minimum_total - total} more with --ref KIND:LOCATOR."
        )
    if primary < floor.minimum_primary:
        shortfalls.append(
            f"{primary} primary/official reference(s); {floor.minimum_primary} required. "
            "Add a statute, regulation, court opinion, agency dataset or an "
            "on-the-record official statement: --ref primary:... or --ref official:..."
        )
    if total and share > floor.maximum_social_share:
        shortfalls.append(
            f"{social} of {total} references are social posts ({share:.0%}); the limit "
            f"is {floor.maximum_social_share:.0%}. A post is admissible as the thing "
            "being analyzed and never as proof that it is true."
        )
    if total and evidentiary == 0:
        shortfalls.append(
            "No reference can support a verdict at its effective tier. Every claim "
            "in this issue would resolve to UNSUPPORTED, so there is nothing to report."
        )

    return FloorResult(
        met=not shortfalls,
        total=total,
        primary_or_official=primary,
        evidentiary=evidentiary,
        social=social,
        social_share=share,
        shortfalls=tuple(shortfalls),
    )


# ==========================================================================
# Resolution
# ==========================================================================


def resolve_references(
    references: list[Reference],
    *,
    fetch: object = None,
) -> list[Reference]:
    """Fetch what can be fetched, and record why the rest could not.

    ``fetch`` is a callable taking a locator and returning a fetched source
    document, or None when no connector handles it. Injected rather than
    imported so this stays testable without a network and so the connector
    registry is not a hidden dependency of the schema layer.

    A reference that cannot be fetched is NOT dropped. It stays in the record
    at T4 with its reason attached, because "the submitter cited something
    nobody can open" is information the reader needs, and silently discarding
    it would make a thin record look like a thorough one.
    """
    resolved: list[Reference] = []
    for reference in references:
        if fetch is None:
            resolved.append(
                reference.model_copy(
                    update={"unfetchable_reason": "no connector was configured for this run"}
                )
            )
            continue
        try:
            document = fetch(reference.locator)  # type: ignore[operator]
        except Exception as error:
            resolved.append(
                reference.model_copy(
                    update={"unfetchable_reason": f"{type(error).__name__}: {error}"}
                )
            )
            continue

        if document is None:
            resolved.append(
                reference.model_copy(
                    update={
                        "unfetchable_reason": (
                            "no connector handles this locator; see "
                            "`abca sources list` for what can be fetched"
                        )
                    }
                )
            )
            continue

        update: dict[str, object] = {
            "resolved_tier": document.tier,
            "retrieved_at": getattr(document, "retrieved_at", None) or datetime.now(UTC),
            "content_hash": document.content_hash,
            "title": document.title or reference.title,
        }
        # A supplied quote is only kept if the fetched source actually contains
        # it. An unverified quote attached to a real source is worse than no
        # quote: it borrows the source's authority for words it does not have.
        if reference.quote and not document.contains(reference.quote):
            update["quote"] = None
            update["unfetchable_reason"] = (
                "the supplied quote does not appear verbatim in the fetched source; "
                "quote dropped, reference kept"
            )
        resolved.append(reference.model_copy(update=update))
    return resolved


def tier_summary(references: list[Reference]) -> dict[str, int]:
    """Count references by effective tier, for the run record and the report."""
    counts = {tier.value: 0 for tier in SourceTier}
    for reference in references:
        counts[reference.effective_tier.value] += 1
    return counts


__all__ = [
    "HARD_MINIMUM_PRIMARY",
    "HARD_MINIMUM_TOTAL",
    "QUOTE_SEPARATOR",
    "EvidenceFloor",
    "FloorResult",
    "ReferenceSpecError",
    "build_fetcher",
    "check_floor",
    "parse_reference_spec",
    "resolve_references",
    "tier_summary",
]


def build_fetcher(*, offline: bool = False, cache: object = None) -> object:
    """Return a ``fetch(locator)`` that tries every connector in turn.

    Connectors do not advertise which locators they handle -- they raise on one
    they cannot take -- so resolution is "try each, first success wins." That is
    O(connectors) per reference, which is fine at the current count and honest
    about what it does: no connector is asked to predict its own competence.

    Returns None when nothing handled the locator, which
    :func:`resolve_references` records as an unfetchable reason rather than
    treating as an error. A reference nobody can open is a finding about the
    issue, not a crash.
    """
    from abca.sources.cache import SourceCache
    from abca.sources.registry import build_connectors

    connectors = build_connectors(
        cache=cache if cache is not None else SourceCache(),  # type: ignore[arg-type]
        offline=offline,
    )

    def fetch(locator: str):  # type: ignore[no-untyped-def]
        for connector in connectors.values():
            try:
                return connector.fetch(locator)
            except Exception:
                continue
        return None

    return fetch

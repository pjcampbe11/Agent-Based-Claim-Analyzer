"""Verify every institutional-corpus entry against its live citation.

WHY THIS BLOCKS THE BUILD
=========================
The context pack is the one component that serves cited facts WITHOUT retrieving
anything at run time. That is what makes it fast and what makes it dangerous: if
an entry's citation drifts, 404s, or no longer contains the quoted passage, every
brief that selects that entry ships a citation that does not say what the entry
claims it says.

Doc 19 s9 calls this the cheapest high-value check in the system, and it is:
it runs against the corpus rather than against model output, so it is
deterministic, and it catches an entire class of silent failure for the cost of
a handful of HTTP requests.

    python scripts/check_context_pack.py            # offline structural checks
    python scripts/check_context_pack.py --live     # + re-fetch every citation

The offline pass runs in CI on every commit. The live pass runs on the weekly
schedule, for the same reason the connector tests do: a government web server
being slow is not a defect in this repository, but a citation that has silently
stopped saying what we claim is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from abca.context_pack.corpus import CorpusError, load_corpus
from abca.context_pack.entry import evaluative_language


def structural_checks(pack) -> list[str]:  # type: ignore[no-untyped-def]
    """Everything checkable without a network. Runs on every commit."""
    problems: list[str] = []

    for entry in pack.entries:
        # The no-conclusion lint. An entry states what a mechanism IS.
        for field in ("statement", "plain_language", "common_distortion"):
            found = evaluative_language(getattr(entry, field))
            if found:
                problems.append(f"{entry.id}: {field} contains evaluative language {found}")

        # A quote is what makes a citation checkable.
        if len(entry.citation.quote.strip()) < 12:
            problems.append(
                f"{entry.id}: citation quote is too short to locate in a source"
            )

        # plain_language must actually be simpler than statement, or it is not
        # doing the job the fidelity gate verified it for.
        if len(entry.plain_language) > len(entry.statement) * 2.0:
            problems.append(
                f"{entry.id}: plain_language is more than twice the length of "
                "statement; a 'simplification' that doubles the text is not one"
            )

        # Every entry must carry a contrast or explain why it has none. The most
        # common misreadings are confusions between adjacent mechanisms.
        if not entry.distinguish_from and not entry.common_distortion:
            problems.append(
                f"{entry.id}: has neither distinguish_from nor common_distortion, "
                "so it explains a mechanism without explaining how it is misread"
            )

    stale = pack.stale()
    for entry in stale:
        problems.append(
            f"{entry.id}: past review_due {entry.volatility.review_due}; "
            "re-verify against the current rule text"
        )

    return problems


def live_checks(pack) -> list[str]:  # type: ignore[no-untyped-def]
    """Re-fetch every citation and confirm the quote is still verbatim."""
    import tempfile

    from abca.sources.cache import SourceCache
    from abca.sources.registry import build_connectors

    connectors = build_connectors(cache=SourceCache(tempfile.mkdtemp()))
    problems: list[str] = []

    for entry in pack.entries:
        document = None
        for connector in connectors.values():
            try:
                document = connector.fetch(entry.citation.title)
                break
            except Exception:
                continue

        if document is None:
            problems.append(
                f"{entry.id}: no connector could fetch {entry.citation.title!r}. "
                "An entry nobody can re-verify is an entry that cannot be trusted "
                "to still say what it says."
            )
            continue

        if not document.contains(entry.citation.quote):
            problems.append(
                f"{entry.id}: the quoted passage is NO LONGER present in "
                f"{entry.citation.title}. The rule text changed underneath the entry."
            )
        elif document.content_hash != entry.citation.content_hash:
            # Drift, not failure: the section was amended elsewhere but the
            # quoted passage survives. Reported so a human re-reads it.
            problems.append(
                f"{entry.id}: {entry.citation.title} content hash drifted "
                f"(quote still present). Recorded {entry.citation.content_hash[:22]}..., "
                f"now {document.content_hash[:22]}... -- re-read and re-stamp."
            )
        else:
            print(f"  OK   {entry.id:42} {entry.citation.title}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="re-fetch every citation and compare quote and hash")
    args = parser.parse_args()

    try:
        pack = load_corpus()
    except CorpusError as error:
        print(f"corpus failed to load: {error}")
        return 1

    print(f"corpus {pack.version}")
    print(f"  entries : {len(pack.entries)}")
    print(f"  hash    : {pack.content_hash}")
    print(f"  domains : {pack.summary()['domains']}\n")

    problems = structural_checks(pack)
    if args.live:
        problems += live_checks(pack)

    if problems:
        print("\nthe institutional corpus has problems:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n{len(problems)} problem(s). The build does not pass with a broken entry.")
        return 1

    scope = "structure and live citations" if args.live else "structure"
    print(f"\nall {len(pack.entries)} entries pass ({scope}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

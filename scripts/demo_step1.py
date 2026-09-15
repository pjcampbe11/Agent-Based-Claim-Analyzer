"""Create a synthetic run record so the ledger can be exercised end to end.

There is no inference pipeline yet (build steps 2-4), so this script fabricates
a structurally complete, contract-valid run and writes it to the ledger. It
exists for two reasons:

1. It lets a new contributor run ``abca ledger list/show/audit`` and see real
   output on the first day, before any model is installed.
2. It is the demo for the tamper-evidence property: write a run, edit one
   character in the stored JSON, and watch ``abca ledger audit`` catch it.

Usage::

    python scripts/demo_step1.py --ledger-root .abca/runs
    abca ledger show <run-id> --stages --ledger-root .abca/runs
    abca ledger audit --ledger-root .abca/runs

The claims below are real -- they are the Illinois ballot-access findings from
docs/03-registration-memo.md, with genuine 10 ILCS 5/10-2 citations -- so the
demo output is not nonsense. But the VERDICTS were written by hand, not
adjudicated by a model, and the record is therefore marked as synthetic in its
notes. A demo that produced records indistinguishable from real ones would
undermine the point of the ledger.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from abca.canonical import digest_text
from abca.ledger.recorder import RunRecorder
from abca.ledger.store import LedgerStore
from abca.schema.core import (
    AnalysisResult,
    Citation,
    Claim,
    DocumentRef,
    RedTeamFinding,
)
from abca.schema.enums import (
    ClaimType,
    InputKind,
    Profile,
    SourceTier,
    StageName,
    Verdict,
)
from abca.schema.ledger import ModelIdentity, RunConfig, SourceSnapshot

STATEMENT = (
    "Illinois makes you get 25,000 signatures to start a new party, and that's "
    "the only real hurdle. The whole thing is rigged against outsiders."
)

ILCS_URL = "https://ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm"
ILCS_QUOTE_SIGNATURES = (
    "signed by 1% of the number of voters who voted at the next preceding "
    "Statewide general election or 25,000 qualified voters, whichever is less"
)
ILCS_QUOTE_SLATE = (
    "shall at the time of filing contain a complete list of candidates of such "
    "party for all offices to be filled in the State, or such district or "
    "political subdivision as the case may be, at the next ensuing election "
    "then to be held"
)

# Stand-in for the real fetched-page hash. A live run would hash the actual
# retrieved bytes; here we hash the quoted text so the value is at least
# derived from real content rather than invented.
ILCS_CONTENT_HASH = digest_text(ILCS_QUOTE_SIGNATURES + ILCS_QUOTE_SLATE)


def build_result() -> AnalysisResult:
    """Assemble a hand-written but contract-valid analysis of STATEMENT."""
    now = datetime.now(UTC)
    normalized = STATEMENT

    document = DocumentRef(
        kind=InputKind.TEXT,
        locator="<inline>",
        content_hash=digest_text(normalized),
        retrieved_at=now,
        byte_length=len(normalized.encode("utf-8")),
    )

    signatures_citation = Citation(
        tier=SourceTier.T0,
        title="10 ILCS 5/10-2",
        url=ILCS_URL,
        quote=ILCS_QUOTE_SIGNATURES,
        retrieved_at=now,
        content_hash=ILCS_CONTENT_HASH,
        locator="10 ILCS 5/10-2",
    )
    slate_citation = signatures_citation.model_copy(update={"quote": ILCS_QUOTE_SLATE})

    claims = [
        # A LEGAL claim that is partly right -- the classic case the MIXED
        # verdict exists for. The number is correct; the framing that it is a
        # flat requirement is not.
        Claim(
            id="c-001",
            text="Illinois requires 25,000 signatures to form a new statewide party.",
            span_start=0,
            span_end=62,
            claim_type=ClaimType.LEGAL,
            verdict=Verdict.MIXED,
            confidence=0.91,
            evidence_quality=SourceTier.T0,
            reasoning=(
                "The statute sets the lesser of 1% of the preceding statewide general "
                "election vote or 25,000 signatures. In practice 25,000 controls, so the "
                "figure is right, but stating it as a fixed statutory number misdescribes "
                "the rule, which is a formula."
            ),
            citations=[signatures_citation],
            red_team=RedTeamFinding(
                counter_evidence=(
                    "In every recent cycle 1% of statewide turnout has exceeded 25,000, so "
                    "the practical requirement has been 25,000 and the speaker's number is "
                    "the one a petitioner would actually work to."
                ),
                steelman=(
                    "For any organizer's purposes the number IS 25,000; calling the formula "
                    "a distinction without a difference is defensible."
                ),
                overreach_flags=[],
            ),
        ),
        # A LEGAL claim the input got wrong by omission. This is the finding
        # the tool exists to surface.
        Claim(
            id="c-002",
            text="The signature count is the only real hurdle to forming a new party.",
            span_start=63,
            span_end=110,
            claim_type=ClaimType.LEGAL,
            verdict=Verdict.CONTRADICTED,
            confidence=0.88,
            evidence_quality=SourceTier.T0,
            reasoning=(
                "10 ILCS 5/10-2 additionally requires the petition to contain a complete "
                "slate of candidates for every office to be filled in the subdivision. "
                "Statewide that means a full ticket. Recruiting that slate is a separate "
                "and frequently larger obstacle than the signatures."
            ),
            citations=[slate_citation],
            red_team=RedTeamFinding(
                counter_evidence=(
                    "The full-slate requirement has been litigated and its scope has been "
                    "narrowed in places; treating it as absolute overstates it."
                ),
                steelman=(
                    "A speaker using 'hurdle' loosely to mean 'the thing organizers talk "
                    "about' is not making a legal claim at all."
                ),
                overreach_flags=["'only real hurdle' may be rhetorical rather than assertive"],
            ),
        ),
        # The normative claim. NOT adjudicated -- this is the type gate doing
        # the work that keeps the tool usable by people who disagree with it.
        Claim(
            id="c-003",
            text="The system is rigged against outsiders.",
            span_start=111,
            span_end=150,
            claim_type=ClaimType.NORMATIVE,
            verdict=Verdict.UNVERIFIABLE,
            confidence=0.0,
            evidence_quality=None,
            reasoning=(
                "This is a value judgment about fairness, which evidence can inform but "
                "cannot settle. Its factual premises -- the signature threshold and the "
                "slate requirement -- are adjudicated separately as c-001 and c-002. "
                "Whether those add up to 'rigged' is a question for the reader."
            ),
        ),
    ]

    return AnalysisResult(
        document=document,
        claims=claims,
        notes=[
            (
                "SYNTHETIC DEMO RECORD. Verdicts were hand-written for docs/demo "
                "purposes, not produced by a model. Do not cite this run."
            ),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger-root",
        type=Path,
        default=Path(".abca/runs"),
        help="Where to write the run record (default: ./.abca/runs)",
    )
    args = parser.parse_args()

    result = build_result()

    # A plausible local-first configuration. Weight hashes are stand-ins; a
    # real run reads them from the model files, which is what makes
    # reproducible=True mean something.
    config = RunConfig(
        profile=Profile.STANDARD,
        seed=42,
        temperature=0.0,
        max_claims=200,
        reading_level=8,
        models=[
            ModelIdentity(
                role="classifier", provider="ollama", name="qwen2.5:7b-instruct",
                weights_hash="sha256:" + hashlib.sha256(b"classifier-demo").hexdigest(),
                quantization="Q4_K_M", context_length=32768,
            ),
            ModelIdentity(
                role="adjudicator", provider="ollama", name="qwen2.5:32b-instruct",
                weights_hash="sha256:" + hashlib.sha256(b"adjudicator-demo").hexdigest(),
                quantization="Q5_K_M", context_length=32768,
            ),
            ModelIdentity(
                role="backtranslate", provider="ollama", name="llama3.1:8b-instruct",
                weights_hash="sha256:" + hashlib.sha256(b"backtranslate-demo").hexdigest(),
                quantization="Q5_K_M", context_length=8192,
            ),
        ],
    )

    recorder = RunRecorder(document=result.document, config=config)
    recorder.record_source(
        SourceSnapshot(
            url=ILCS_URL,
            content_hash=ILCS_CONTENT_HASH,
            retrieved_at=datetime.now(UTC),
            connector="ilcs",
        )
    )

    # Walk the real stage sequence so the chain looks like a live run's.
    for stage_name, payload in [
        (StageName.INGEST, {"bytes": result.document.byte_length}),
        (StageName.SEGMENT, {"claims_extracted": len(result.claims)}),
        (StageName.CLASSIFY, {"types": result.type_counts()}),
        (StageName.GATE, {"dropped_out_of_scope": 0}),
        (StageName.RETRIEVE, {"sources": 1}),
        (StageName.ADJUDICATE, {"verdicts": result.verdict_counts()}),
        (StageName.RED_TEAM, {"findings": sum(1 for c in result.claims if c.red_team)}),
        (StageName.COMPOSE, {"reading_level": 8}),
    ]:
        with recorder.stage(stage_name) as stage:
            stage.set_input({"stage": stage_name.value})
            stage.set_output(payload)

    record = recorder.finalize(result)
    path = LedgerStore(args.ledger_root).write(record)

    print(f"run id      : {record.run_id}")
    print(f"ledger hash : {record.ledger_hash}")
    print(f"written to  : {path}")
    print()
    print("Next:")
    print(f"  abca ledger show {record.run_id} --stages --ledger-root {args.ledger_root}")
    print(f"  abca ledger audit --ledger-root {args.ledger_root}")
    print("  # then edit one character in the JSON and run audit again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

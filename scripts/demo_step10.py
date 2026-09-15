"""Demonstrate the challenge lane end to end (docs 18-20).

Runs against the LIVE eCFR connector, because the point of the lane is that it
finds REAL documents. Nothing here is mocked except the dissection, which would
need a configured model.

    python scripts/demo_step10.py

Six scenes:

  1. Intake: a viral narrative in five wordings becomes ONE challenge.
  2. A successful promotion, against a document actually fetched from eCFR.
  3. THE SUBSTITUTION ATTACK: the claim is rewritten to the reconstruction, and
     the gate refuses even though every other condition passes.
  4. An unpromotable challenge, published with everything it searched.
  5. A dormant challenge with its re-check date.
  6. The published record, as `abca queue show` renders it.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from abca.canonical import digest_text
from abca.challenge.executor import Budget
from abca.challenge.lane import lane_a_input, run_challenge
from abca.challenge.queue import ChallengeQueue
from abca.schema.challenge import (
    ConfirmationParticular,
    Particular,
    PromotionGrade,
    RetrievedArtifact,
)
from abca.schema.enums import Verdict
from abca.schema.sourceless import (
    BranchOutcome,
    ReferentCandidate,
    ReferentConfidence,
    RetrievalPlan,
    SourcelessAnalysis,
)
from abca.sources.cache import SourceCache
from abca.sources.ecfr import ECFRConnector

RULE = "-" * 76
CLAIM = "Any group that spends money on politics has to register with the government."
BRANCH = "the regulation defining when a group becomes a political committee"
CITE = "11 CFR 100.5"


def scene(n: int, title: str) -> None:
    print(f"\n{RULE}\n{n}. {title}\n{RULE}")


def analysis(claim: str = CLAIM, *, hash_for: str | None = None) -> SourcelessAnalysis:
    return SourcelessAnalysis(
        denatured_claim=claim,
        denatured_claim_hash=digest_text((hash_for or claim).strip()),
        referent_candidates=(
            ReferentCandidate(
                candidate="the federal political-committee registration threshold",
                referent_confidence=ReferentConfidence.MEDIUM,
                distortion_applied="title_to_text",
                reasoning="the post states a general duty where the rule states a threshold"),
            ReferentCandidate(
                candidate="a state campaign-finance registration rule",
                referent_confidence=ReferentConfidence.LOW,
                distortion_applied="state_to_federal", reasoning="jurisdiction is unstated"),
        ),
        retrieval_plan=RetrievalPlan(
            queries=(CITE,),
            decisive_artifact="11 CFR 100.5",
            branch_outcomes=(BranchOutcome(if_found=BRANCH,
                                           then_disposition=Verdict.UNSUPPORTED),),
        ),
    )


def main() -> int:
    connector = ECFRConnector(cache=SourceCache(tempfile.mkdtemp()))
    queue = ChallengeQueue(Path(tempfile.mkdtemp()))

    def fetch(query: str) -> RetrievedArtifact | None:
        """Run one query against the live eCFR connector."""
        try:
            document = connector.fetch(query)
        except Exception:
            return None
        return RetrievedArtifact(
            url=document.url, title=query, tier=document.tier,
            content_hash=document.content_hash, retrieved_at=datetime.now(UTC),
            connector="ecfr", query=query,
        )

    def confirm(artifact: RetrievedArtifact, _analysis):
        """Read the artifact and check the four identifying particulars."""
        document = connector.fetch(artifact.query)
        checks = {
            Particular.ACTOR: "political committee",
            Particular.JURISDICTION: "52 U.S.C.",
            Particular.SUBJECT_MATTER: "contributions aggregating in excess of $1,000",
        }
        out = []
        for particular, needle in checks.items():
            hit = document.contains(needle)
            out.append(ConfirmationParticular(
                particular=particular, matched=hit,
                quote=needle if hit else "", note="" if hit else f"{needle!r} absent"))
        # The date window cannot be confirmed from a CFR section, which has no
        # event date. Reported honestly as unmatched rather than assumed.
        out.append(ConfirmationParticular(
            particular=Particular.DATE_WINDOW, matched=False,
            note="a CFR section carries no event date, so the post's timing cannot "
                 "be tied to it"))
        return tuple(out), BRANCH

    # -- 1 ---------------------------------------------------------------
    scene(1, "Intake: one narrative, five wordings, ONE challenge")
    for wording in (
        CLAIM,
        "if you spend ANY money on politics you have to register!!",
        "Any group spending on politics must register with the feds.",
        "spend a dollar on politics -> you register. thats the law",
        "Groups that spend money on politics are required to register.",
    ):
        queue.enqueue(wording, cluster_key="political-committee-registration")
    stats = queue.stats()
    print(f"  challenges          : {stats['total']}")
    print(f"  posts represented   : {stats['posts_represented']}")
    print("  A narrative on ten thousand accounts is one challenge, not ten thousand.")

    challenge = queue.claim_next()

    # -- 2 ---------------------------------------------------------------
    scene(2, "Execution and the gate, against a live eCFR fetch")
    run = run_challenge(challenge, analysis(), fetch, confirm, budget=Budget())
    print(run.gate.explain())
    if run.outcome.artifact:
        print(f"\n  artifact  : [{run.outcome.artifact.tier.value}] "
              f"{run.outcome.artifact.title}")
        print(f"              {run.outcome.artifact.url}")
    print(f"\n  grade     : {run.outcome.grade.value}")
    if run.enters_lane_a:
        print(f"  Lane A gets: {lane_a_input(run)!r}")
        print("  Note what it is NOT: the reconstruction. The artifact is evidence;")
        print("  the posted sentence stays the claim.")

    # -- 3 ---------------------------------------------------------------
    scene(3, "THE SUBSTITUTION ATTACK")
    substituted = analysis(
        claim="Under 11 CFR 100.5 a group becomes a political committee above $1,000.",
        hash_for=CLAIM,
    )
    attack = run_challenge(challenge, substituted, fetch, confirm)
    print(attack.gate.explain())
    print(f"\n  grade: {attack.outcome.grade.value}")
    passed = sum(1 for c in attack.gate.conditions if c.passed)
    print(f"  {passed}/4 conditions passed and it STILL did not promote.")
    print("  Without P4 the reader would see a verdict on a sentence nobody posted.")

    # -- 4 ---------------------------------------------------------------
    scene(4, "Unpromotable, published with everything it searched")
    dead = run_challenge(challenge, analysis(), lambda q: None, confirm)
    print(f"  grade            : {dead.outcome.grade.value}")
    print(f"  queries executed : {len(dead.outcome.queries_executed)}")
    print(f"  widenings        : {len(dead.outcome.widenings)}")
    print(f"  considered       : {list(dead.outcome.candidates_considered)}")
    print(f"  rejected         : {list(dead.outcome.candidates_rejected)}")
    print("\n  'We tried, here is exactly what we searched, we found nothing' is")
    print("  more falsifiable than a verdict: anyone can run the same queries.")

    # -- 5 ---------------------------------------------------------------
    scene(5, "Dormant: no source yet, but one plausibly will exist")
    dormant = run_challenge(
        challenge, analysis(), lambda q: None, confirm,
        future_artifact_plausible=True, budget=Budget(max_queries=1),
        recheck_trigger="the rulemaking is open for comment",
    )
    print(f"  grade        : {dormant.outcome.grade.value}")
    print(f"  re-check     : {dormant.outcome.recheck_after}")
    print(f"  waiting on   : {dormant.outcome.recheck_trigger}")
    print(f"  terminal?    : {dormant.outcome.grade.is_terminal}")

    # -- 6 ---------------------------------------------------------------
    scene(6, "The published record")
    queue.record_outcome(challenge, run.outcome)
    stored = queue.get(challenge.challenge_id)
    print(f"  {stored.challenge_id}")
    print(f"  state   : {stored.state.value}")
    print(f"  grade   : {stored.outcome['grade']}")
    print(f"  claim   : {stored.denatured_claim}")
    print(f"  hash    : {stored.denatured_claim_hash}")
    print(f"  posts   : {stored.occurrences}")

    ok = (
        run.outcome.grade in {PromotionGrade.PROMOTED, PromotionGrade.PROMOTED_WEAK}
        and attack.outcome.grade is PromotionGrade.UNPROMOTABLE
        and dead.outcome.grade is PromotionGrade.UNPROMOTABLE
        and dormant.outcome.grade is PromotionGrade.DORMANT
    )
    print(f"\n{RULE}\nall scenes behaved as specified: {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

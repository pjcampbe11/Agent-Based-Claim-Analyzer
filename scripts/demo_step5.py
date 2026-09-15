"""The red team, and the one-way ratchet that makes it trustworthy.

Contract s7 makes an adversarial pass mandatory: every verdict is attacked
before it ships. The interesting design question is not "does it run" but
"what is it allowed to do", because a pass with the power to change verdicts is
also a pass that could be used to change them for the wrong reasons.

Three properties, demonstrated here:

1. **It can only weaken.** No verdict is ever made more assertive by the red
   team, and no confidence goes up. The worst a compromised or contrarian red
   team can do is make the tool say less than it knows.
2. **Its counter-evidence is verified** by the same verbatim check the
   adjudicator's evidence goes through. Otherwise "attack the analysis" would
   be a licence to invent text that defeats any verdict.
3. **Only MATERIAL moves anything**, and MATERIAL carries a burden. A tool that
   downgraded on every objection would end up saying nothing about anything.

Usage::

    python scripts/demo_step5.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from abca.pipeline.adjudicate import AdjudicationRecord
from abca.pipeline.models import DraftClaim
from abca.pipeline.red_team import permitted_downgrade, run_red_team
from abca.pipeline.retrieve import run_retrieve
from abca.providers.base import Completion, GenerationRequest
from abca.schema.enums import ClaimType, Verdict
from abca.schema.ledger import ModelIdentity
from abca.sources.cache import SourceCache
from abca.sources.registry import build_connectors

REAL_QUOTE = (
    "signed by 1% of the number of voters who voted at the next preceding "
    "Statewide general election or 25,000 qualified voters, whichever is less"
)
SLATE_QUOTE = (
    "shall at the time of filing contain a complete list of candidates of such "
    "party for all offices to be filled"
)
FABRICATED = "the petition requirement shall not apply to established parties"

IDENTITY = ModelIdentity(role="redteam", provider="ollama", name="rt",
                         weights_hash="sha256:" + "c" * 64)


class Scripted:
    name = "scripted"

    def __init__(self, payload):
        self.payload = payload

    def identity(self):
        return IDENTITY

    def generate(self, request: GenerationRequest) -> Completion:
        return Completion(text=json.dumps(self.payload), identity=IDENTITY,
                          prompt_tokens=300, completion_tokens=120, duration_ms=150)

    def embed(self, texts):
        return []

    def health(self) -> None:
        return None


def rule(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * max(0, 68 - len(title))}")


def assessment(**overrides):
    base = {
        "claim_id": "c-001", "severity": "MATERIAL",
        "counter_evidence": (
            "The statute sets the LESSER of 1% of the preceding statewide vote or "
            "25,000, so 25,000 is a ceiling rather than the requirement."
        ),
        "steelman": (
            "In every recent cycle 1% of statewide turnout has exceeded 25,000, so "
            "25,000 is the number an organizer actually works to."
        ),
        "overreach_flags": ["states a statutory formula as a fixed number"],
        "counter_citations": [],
        "recommended_verdict": "MIXED",
    }
    base.update(overrides)
    return {"assessments": [base]}


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="abca-demo5-"))
    connectors = build_connectors(cache=SourceCache(workspace / "sources"))

    claims = [DraftClaim(
        id="c-001",
        text="Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to start a new party.",
        sentence_index=0, claim_type=ClaimType.LEGAL)]
    retrieval = run_retrieve(claims, connectors).value
    document = retrieval.documents[0]

    def adjudicated(verdict=Verdict.SUPPORTED, confidence=0.95):
        return {"c-001": AdjudicationRecord(
            claim_id="c-001", verdict=verdict, confidence=confidence,
            reasoning="The statute requires 25,000 signatures to form a new party.",
            citations=[document.to_citation(document.locate(REAL_QUOTE))])}

    def attack(payload, adjudications=None, independent=True):
        return run_red_team(Scripted(payload), claims,
                            adjudications or adjudicated(), retrieval,
                            independent=independent, max_attempts=1)

    rule("1. The ratchet: which moves are permitted")
    print("  Enforced in code, not requested in the prompt. A model cannot be")
    print("  talked out of a rule it never sees.\n")
    moves = [
        (Verdict.SUPPORTED, Verdict.MIXED),
        (Verdict.SUPPORTED, Verdict.UNSUPPORTED),
        (Verdict.SUPPORTED, None),
        (Verdict.MIXED, Verdict.CONTRADICTED),
        (Verdict.UNSUPPORTED, Verdict.SUPPORTED),
        (Verdict.UNVERIFIABLE, Verdict.UNSUPPORTED),
    ]
    for current, recommended in moves:
        result = permitted_downgrade(current, recommended)
        arrow = f"-> {result.value}" if result else "-> REFUSED"
        label = recommended.value if recommended else "(no recommendation)"
        print(f"  {current.value:14} + red team says {label:20} {arrow}")
    print("\n  UNVERIFIABLE is untouchable: it follows from the claim's TYPE and")
    print("  the gate, not from evidence, so an evidence-based objection has no")
    print("  purchase on it.")

    rule("2. A MATERIAL finding with verified counter-evidence")
    outcome = attack(assessment(counter_citations=[
        {"source_id": document.id, "quote": SLATE_QUOTE, "locator": "10 ILCS 5/10-2"}]))
    finding = outcome.value.findings["c-001"]
    verdict, confidence = outcome.value.downgrades["c-001"]
    print("  adjudicator said : SUPPORTED at 0.95")
    print(f"  red team severity: {finding.severity.value}")
    print(f"  published        : {verdict.value} at {confidence:.2f}")
    print(f"  counter-citation : [{finding.counter_citations[0].tier.value}] verified verbatim")
    print(f"      {' '.join(finding.counter_citations[0].quote.split())[:70]}...")
    print(f"  overreach        : {finding.overreach_flags[0]}")

    rule("3. The same objection, with FABRICATED counter-evidence")
    outcome = attack(assessment(overreach_flags=[], counter_citations=[
        {"source_id": document.id, "quote": FABRICATED, "locator": None}]))
    finding = outcome.value.findings["c-001"]
    print("  red team claimed : MATERIAL, quoting text that is not in the statute")
    print(f"  after verification: {finding.severity.value}")
    print(f"  verdict moved    : {'yes' if outcome.value.downgrades else 'NO — verdict stands'}")
    print(f"  citations kept   : {len(finding.counter_citations)}")
    print("\n  An objection whose textual support evaporated cannot overturn a cited")
    print("  finding on that support alone. If it had ALSO named a specific")
    print("  overreach it would still stand as MATERIAL — an analytical objection")
    print("  needs no quote.")

    rule("4. An analytical objection, no citation needed")
    outcome = attack(assessment(
        counter_citations=[],
        overreach_flags=["the verdict rests on inference across two provisions"]))
    print(f"  severity   : {outcome.value.findings['c-001'].severity.value}")
    print(f"  verdict    : {outcome.value.downgrades['c-001'][0].value}")
    print("  'The statute is silent, so this rests on inference' is legitimate,")
    print("  often the strongest objection, and quotes nothing.")

    rule("5. The common case: attacked, found sound")
    outcome = attack(assessment(
        severity="NONE",
        counter_evidence="Nothing in the retrieved section cuts against this.",
        overreach_flags=[], recommended_verdict=None))
    print(f"  severity   : {outcome.value.findings['c-001'].severity.value}")
    print("  verdict    : unchanged")
    print("  A red team that finds something material in every verdict is being")
    print("  contrarian, not rigorous. NONE is a real and frequent answer, and it")
    print("  is still published so a reader can see the verdict was tested.")

    rule("6. Independence")
    outcome = attack(assessment(), independent=False)
    print(f"  recorded on the finding : independent={outcome.value.findings['c-001'].independent}")
    print(f"  note: {next(n for n in outcome.notes if 'SAME model' in n)[:74]}...")
    print("\n  Not enforced — a same-model red team still finds things, and refusing")
    print("  to run one would be worse. But it is the weaker configuration, so a")
    print("  reader weighing the finding is told.")

    rule("7. Skipping it takes two flags")
    from mock_ollama import MODEL, serve

    httpd, port = serve()
    config_path = workspace / "config.toml"
    config_path.write_text(
        f'[models.classifier]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        f'[models.adjudicator]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n',
        encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
           "COLUMNS": "150", "ABCA_SOURCE_CACHE": str(workspace / "sources")}

    def cli(args):
        return subprocess.run([sys.executable, "-m", "abca.cli.main", *args],
                              capture_output=True, text=True, env=env)

    statement = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures."
    one_flag = cli(["analyze", "-t", statement, "--config", str(config_path),
                    "--no-red-team", "--no-save"])
    print(f"  --no-red-team alone      -> exit {one_flag.returncode}")
    print("  " + (one_flag.stdout + one_flag.stderr).strip().splitlines()[0][:96])

    both = cli(["analyze", "-t", statement, "--config", str(config_path),
                "--no-red-team", "--i-know", "--no-save", "--json"])
    payload = json.loads(both.stdout) if both.returncode == 0 else {}
    print(f"\n  with --i-know            -> exit {both.returncode}")
    if payload:
        print(f"  config.red_team recorded : {payload['config']['red_team']}")
        skipped = [n for n in payload["result"]["notes"] if "RED TEAM SKIPPED" in n]
        print(f"  note carried in the run  : {skipped[0][:88]}...")

    httpd.shutdown()
    print(f"\nworkspace: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

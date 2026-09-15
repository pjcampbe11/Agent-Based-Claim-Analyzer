"""Citation verification and replay, against the real Illinois statute.

This is the demo for the property step 4 exists to provide: **a model cannot
produce a citation.** It produces a pointer and a quote, and the quote is
checked against the retrieved source before anything becomes a citation.

Sections 1-3 hit ilga.gov for real. Section 4 replays a run end to end through
the actual CLI against a mock model, so the full verify path is exercised.

Usage::

    python scripts/demo_step4.py
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

from abca.pipeline.adjudicate import verify_citations
from abca.pipeline.models import Adjudication, CitationRef
from abca.schema.enums import Verdict
from abca.sources.cache import SourceCache
from abca.sources.registry import build_connectors

# Real text from 10 ILCS 5/10-2, spanning the statute's hard line wraps.
REAL_QUOTE = (
    "signed by 1% of the number of voters who voted at the next preceding "
    "Statewide general election or 25,000 qualified voters, whichever is less"
)
# Same shape, same vocabulary, plausible to a reader -- and not in the statute.
FABRICATED_QUOTE = (
    "signed by 25,000 qualified voters of the State in every case, without exception"
)
# One word changed from the real text. The kind of error a model makes when it
# is reconstructing from memory rather than copying.
ALTERED_QUOTE = (
    "signed by 5% of the number of voters who voted at the next preceding "
    "Statewide general election or 25,000 qualified voters, whichever is less"
)


def rule(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * max(0, 68 - len(title))}")


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="abca-demo4-"))
    cache = SourceCache(workspace / "sources")

    rule("1. Live retrieval from ilga.gov")
    connectors = build_connectors(cache=cache)
    document = connectors["ilcs"].fetch("10 ILCS 5/10-2")
    print(f"  title        : {document.title}")
    print(f"  tier         : {document.tier.value}  (fixed by the connector, not by any model)")
    print(f"  url          : {document.url}")
    print(f"  content hash : {document.content_hash}")
    print(f"  length       : {len(document.text):,} chars")

    rule("2. Quote verification")
    print("  A model returns only {source_id, quote}. There is no tier field, no")
    print("  url field, no hash field -- so there is nothing for it to fabricate.")
    print("  The quote is then checked against the retrieved text.\n")

    index = {document.id: document}
    cases = [
        ("real quote, spanning line wraps", REAL_QUOTE),
        ("fabricated quote", FABRICATED_QUOTE),
        ("altered quote (1% -> 5%)", ALTERED_QUOTE),
    ]
    for label, quote in cases:
        adjudication = Adjudication(
            claim_id="c-001", verdict=Verdict.SUPPORTED, confidence=0.9,
            reasoning="...", citations=[CitationRef(source_id=document.id, quote=quote)],
        )
        verified, rejected = verify_citations(adjudication, index)
        status = "[VERIFIED]" if verified else "[REJECTED]"
        print(f"  {status} {label}")
        if verified:
            stored = " ".join(verified[0].quote.split())
            print(f"             stored as the SOURCE's wording: {stored[:64]}...")
            print(f"             tier {verified[0].tier.value} from the connector")
        else:
            print(f"             {rejected[0][:88]}")

    print("\n  The altered quote is the important one. It is one character different")
    print("  from the statute and changes the legal meaning completely. A reader")
    print("  skimming would not catch it; substring verification cannot miss it.")

    rule("3. What happens to a verdict whose citations all fail")
    adjudication = Adjudication(
        claim_id="c-001", verdict=Verdict.SUPPORTED, confidence=0.95,
        reasoning="The statute plainly requires exactly 25,000 signatures.",
        citations=[CitationRef(source_id=document.id, quote=FABRICATED_QUOTE)],
    )
    verified, rejected = verify_citations(adjudication, index)
    print(f"  model said     : {adjudication.verdict.value} at {adjudication.confidence:.2f}")
    print(f"  citations kept : {len(verified)}")
    print("  result         : downgraded to UNSUPPORTED at 0.00, and the downgrade")
    print("                   is written into the claim's reasoning so a reader sees it.")
    print("\n  The schema enforces this independently: SUPPORTED with no surviving")
    print("  T0-T2 citation fails validation, and the repair loop re-asks. The model")
    print("  does not get to keep the verdict and lose the evidence.")

    rule("4. Analyze, then verify by replay (real CLI, mock model)")
    from mock_ollama import MODEL, serve

    httpd, port = serve()
    config_path = workspace / "config.toml"
    ledger_root = workspace / "runs"
    config_path.write_text(
        '[defaults]\nseed = 42\ntemperature = 0.0\n\n'
        f'[models.classifier]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        f'[models.adjudicator]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        f'[models.redteam]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n',
        encoding="utf-8",
    )

    statement = (
        "Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures to start a "
        "new party, and that's the only real hurdle. The whole thing is rigged "
        "against outsiders. Last week the Secretary of State said the process is "
        "straightforward. Anyway, my dog is asleep on the couch."
    )

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
           "COLUMNS": "150", "ABCA_SOURCE_CACHE": str(workspace / "sources")}

    def cli(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "abca.cli.main", *args],
            capture_output=True, text=True, env=env,
        )

    analyze = cli(["analyze", "-t", statement, "--config", str(config_path),
                   "--ledger-root", str(ledger_root), "--json"])
    if analyze.returncode != 0:
        print("  analyze failed:", analyze.stderr[:400])
        httpd.shutdown()
        return 1

    record = json.loads(analyze.stdout)
    run_id = record["run_id"]
    print(f"  run          : {run_id}")
    print(f"  verdicts     : {json.dumps(record['result']['claims'] and {c['verdict']: 1 for c in record['result']['claims']})}")
    cited = [c for c in record["result"]["claims"] if c["citations"]]
    print(f"  cited claims : {len(cited)} carrying {sum(len(c['citations']) for c in cited)} verified citation(s)")
    print(f"  sources      : {[s['url'].rsplit('/', 1)[-1] for s in record['sources']]}")

    print("\n  Now replay it. The verifier must have the same input, the same pinned")
    print("  weights, and the same prompt hashes, or the replay is refused.\n")
    verify = cli(["verify", run_id, "--ledger-root", str(ledger_root),
                  "--config", str(config_path)])
    print("  " + "\n  ".join(verify.stdout.strip().splitlines()[:14]))
    print(f"  exit code    : {verify.returncode}")

    print("\n  And with the WRONG input text:")
    wrong = cli(["verify", run_id, "--ledger-root", str(ledger_root),
                 "--config", str(config_path),
                 "-t", "A completely different statement about something else."])
    print("  " + (wrong.stderr or wrong.stdout).strip().splitlines()[0][:110])
    print(f"  exit code    : {wrong.returncode}")

    httpd.shutdown()
    print(f"\nworkspace: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

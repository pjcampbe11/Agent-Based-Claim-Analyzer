"""The plain-language fidelity gate: proving a rewrite did not change the law.

The contract promises statute text an eighth-grader can read, with the meaning
intact. The second half of that promise is the hard one, and it fails quietly:
a rewrite that drops an exception reads BETTER than one that keeps it. Fluency
and fidelity pull in opposite directions and only fluency is visible.

So the rewrite is not trusted. It is tested, by a second model that never sees
the statute and is asked to reconstruct the rule from the rewrite alone. What
it recovers is diffed -- in code, not by a model -- against what the statute
actually contains.

Usage::

    python scripts/demo_step6.py

No GPU and no network: the statute is seeded into a temporary cache and every
model call is scripted or served by scripts/mock_ollama.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from abca.fidelity.elements import (
    ElementKind,
    LegalElement,
    diff_elements,
)
from abca.pipeline.fidelity import (
    build_backtranslate_prompt,
    run_fidelity,
)
from abca.prompts import load_prompt
from abca.providers.base import Completion, GenerationRequest
from abca.schema.enums import StageName
from abca.schema.ledger import ModelIdentity
from abca.sources.base import build_document
from abca.sources.cache import SourceCache
from abca.sources.ilcs import ILCSConnector

STATUTE = """(10 ILCS 5/10-2)

Sec. 10-2.
Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled.
"""

EXTRACT = {
    "elements": [
        {"kind": "WHO_IS_BOUND",
         "text": ("Any group of persons hereafter desiring to form a new political "
                  "party throughout the State")},
        {"kind": "REQUIREMENT",
         "text": ("shall file a petition signed by 1% of the number of voters who "
                  "voted at the next preceding Statewide general election or 25,000 "
                  "qualified voters, whichever is less")},
        {"kind": "REQUIREMENT",
         "text": ("shall at the time of filing contain a complete list of candidates "
                  "of such party for all offices to be filled")},
    ]
}

GOOD_RENDERING = (
    "A group of people who want to start a new political party across the whole "
    "state must file a petition. The petition must be signed by 1% of the "
    "voters who voted in the last statewide general election, or by 25,000 "
    "qualified voters, whichever is less. The petition must also list "
    "the party's candidates. It must name a candidate for every office to be "
    "filled. That list must be there on the day the petition is filed."
)
RENDER = {"rendering": GOOD_RENDERING, "footnotes": []}

FAITHFUL = {
    "elements": [
        {"kind": "WHO_IS_BOUND",
         "text": ("A group of people who want to start a new political party across "
                  "the whole state")},
        {"kind": "REQUIREMENT",
         "text": ("must file a petition signed by 1% of the voters who voted in the "
                  "last statewide general election, or 25,000 qualified voters, "
                  "whichever is less")},
        {"kind": "REQUIREMENT",
         "text": ("must include a complete list of the party's candidates for all "
                  "offices to be filled at that election")},
    ]
}
DROPPED = {"elements": FAITHFUL["elements"][:2]}
MODAL_FLIP = {"elements": [
    FAITHFUL["elements"][0],
    {"kind": "PERMISSION",
     "text": ("may file a petition signed by 1% of the voters who voted in the last "
              "statewide general election, or 25,000 qualified voters, whichever is less")},
    FAITHFUL["elements"][2],
]}
INVENTED = {"elements": [
    *FAITHFUL["elements"],
    {"kind": "PENALTY",
     "text": "a party that files late is barred from the ballot for two years"},
]}

RENDERER_ID = ModelIdentity(role="adjudicator", provider="ollama", name="big",
                            weights_hash="sha256:" + "a" * 64)
TRANSLATOR_ID = ModelIdentity(role="backtranslate", provider="ollama", name="small",
                              weights_hash="sha256:" + "b" * 64)


class Scripted:
    """Canned payloads in order; repeats the last one forever."""

    name = "scripted"

    def __init__(self, payloads, identity):
        self.payloads = list(payloads)
        self._identity = identity
        self.calls = 0
        self.prompts: list[str] = []

    def identity(self):
        return self._identity

    def generate(self, request: GenerationRequest) -> Completion:
        self.prompts.append(request.prompt)
        item = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return Completion(text=json.dumps(item), identity=self._identity,
                          prompt_tokens=400, completion_tokens=200, duration_ms=90)

    def embed(self, texts):
        return []

    def health(self) -> None:
        return None


def rule(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * max(0, 68 - len(title))}")


def gate(backtranslations, renderings=(RENDER,)):
    renderer = Scripted([EXTRACT, *renderings], RENDERER_ID)
    translator = Scripted(backtranslations, TRANSLATOR_ID)
    outcome = run_fidelity(
        renderer, translator, source_text=STATUTE, title="10 ILCS 5/10-2",
        locator="10 ILCS 5/10-2", max_attempts=1,
    )
    return outcome, renderer, translator


def summarize(outcome) -> None:
    result = outcome.value
    report = result.to_report()
    verdict = "PASS" if not result.verbatim_fallback else "FAIL -> verbatim statute"
    print(f"  gate            : {verdict}")
    print(f"  fidelity score  : {report.score:.2f}  "
          f"({report.elements_preserved}/{report.elements_source} elements recovered)")
    print(f"  modal preserved : {'yes' if report.modal_verbs_preserved else 'NO'}")
    print(f"  attempts        : {len(result.attempts)}  "
          f"(regenerations {report.regenerations})")
    print(f"  reading grade   : {result.readability.describe()}")
    failures = [f for attempt in result.attempts for f in attempt.failures]
    for failure in failures[:3]:
        print(f"    - {failure[:96]}")


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="abca-demo6-"))

    # ------------------------------------------------------------------
    rule("1. What each pass is allowed to see")
    print("  Pass A (extract)      : the statute")
    print("  Pass B (render)       : the statute + the elements from pass A")
    print("  Pass C (back-translate): the rewrite. Nothing else.\n")

    prompt = build_backtranslate_prompt(
        load_prompt(StageName.FIDELITY, variant="backtranslate").text, GOOD_RENDERING)
    for phrase in ("hereafter desiring", "next preceding Statewide", "10 ILCS 5/10-2"):
        print(f"  {phrase!r:36} in the pass C prompt? {phrase in prompt}")
    print("\n  Not an instruction the model is asked to honour -- "
          "build_backtranslate_prompt")
    print("  has no parameter the statute could arrive through. A future edit "
          "cannot")
    print("  leak it by accident.")

    # ------------------------------------------------------------------
    rule("2. The diff is code, not a fourth model call")
    source = [LegalElement.build(ElementKind.REQUIREMENT,
                                 "shall file a petition signed by 25,000 qualified voters")]
    cases = [
        ("faithful rewrite, different words", ElementKind.REQUIREMENT,
         "must turn in a petition signed by 25,000 qualified voters"),
        ("modal softened to may", ElementKind.PERMISSION,
         "may turn in a petition signed by 25,000 qualified voters"),
        ("number changed", ElementKind.REQUIREMENT,
         "must turn in a petition signed by 20,000 qualified voters"),
        ("unrelated element", ElementKind.REQUIREMENT,
         "must publish a notice in a newspaper of general circulation"),
    ]
    for label, kind, text in cases:
        diff = diff_elements(source, [LegalElement.build(kind, text)])
        print(f"  {'PASS' if diff.passed else 'FAIL'}  {label:38} score={diff.score:.2f}")
    print("\n  Same inputs, same answer, every time -- which is what lets the score")
    print("  go into a published run record.")

    # ------------------------------------------------------------------
    rule("3. A faithful rewrite")
    outcome, _, _ = gate([FAITHFUL])
    summarize(outcome)
    print(f"\n  shipped: {outcome.value.text[:88]}...")

    rule("4. The rewrite dropped the full-slate requirement")
    outcome, renderer, _ = gate([DROPPED])
    summarize(outcome)
    print("\n  Three attempts, then the statute's own words. Contract s5 calls that")
    print("  acceptable and requires it to be reachable: an unverified rewrite of a")
    print("  law is worse than a hard-to-read law.")
    print(f"\n  regeneration prompt names the loss: "
          f"{'did not survive the check' in renderer.prompts[2]}")

    rule("5. 'shall' came back as 'may'")
    outcome, _, _ = gate([MODAL_FLIP])
    summarize(outcome)
    print("\n  The highest-signal check available, and fully mechanical. A 'must'")
    print("  that becomes a 'may' is not a style choice.")

    rule("6. The rewrite invented an obligation")
    outcome, _, _ = gate([INVENTED])
    summarize(outcome)
    print("\n  Every source element survived -- the score alone looks perfect. Added")
    print("  elements fail as hard as dropped ones: a rewrite that introduces a rule")
    print("  the statute does not contain is a different law, not a clearer one.")

    rule("7. Regeneration that recovers")
    outcome, _, _ = gate([DROPPED, FAITHFUL])
    summarize(outcome)

    # ------------------------------------------------------------------
    rule("8. `abca explain` end to end")
    from mock_ollama import MODEL, MODEL_B, serve

    data_home = workspace / "data"
    cache = SourceCache(data_home / "abca" / "sources")
    connector = ILCSConnector(cache=cache, offline=True)
    cache.put(build_document(
        connector=connector, doc_id="s-001", title="10 ILCS 5/10-2",
        url="https://www.ilga.gov/documents/legislation/ilcs/documents/001000050K10-2.htm",
        text=STATUTE, locator="10 ILCS 5/10-2",
        retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
    ))

    httpd, port = serve()
    config_path = workspace / "config.toml"
    # Two DIFFERENT model names: config load itself refuses a fidelity gate whose
    # back-translation runs on the model that wrote the rewrite.
    config_path.write_text(
        f'[models.adjudicator]\nprovider = "ollama"\nmodel = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        f'[models.backtranslate]\nprovider = "ollama"\nmodel = "{MODEL_B}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n',
        encoding="utf-8")

    env = {**os.environ,
           "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
           "COLUMNS": "150",
           "XDG_DATA_HOME": str(data_home)}
    env.pop("LOCALAPPDATA", None)

    def cli(args):
        return subprocess.run([sys.executable, "-m", "abca.cli.main", *args],
                              capture_output=True, text=True, env=env)

    run = cli(["explain", "--cite", "10 ILCS 5/10-2", "--config", str(config_path),
               "--offline", "--json"])
    if run.returncode == 0:
        payload = json.loads(run.stdout)
        fidelity = payload["result"]["fidelity"]
        print(f"  run id            : {payload['run_id']}")
        print("  stages            : "
              + " -> ".join(s["name"] for s in payload["stages"]))
        print(f"  fidelity score    : {fidelity['score']}")
        print(f"  elements          : {fidelity['elements_preserved']}"
              f"/{fidelity['elements_source']} recovered")
        print(f"  modal preserved   : {fidelity['modal_verbs_preserved']}")
        print(f"  unresolved        : {fidelity['unresolved']}")
        print(f"  sources pinned    : {len(payload['sources'])} "
              f"({payload['sources'][0]['content_hash'][:22]}...)")
        print(f"  prompts recorded  : {len(payload['config']['prompts'])} "
              "(extract, render, backtranslate -- one stage, three files)")
        audit = cli(["ledger", "audit", "--ledger-root",
                     str(data_home / "abca" / "runs")])
        print(f"  ledger audit      : exit {audit.returncode}")
    else:
        print(f"  exit {run.returncode}")
        print("  " + (run.stdout + run.stderr).strip()[:400])

    httpd.shutdown()
    print(f"\nworkspace: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

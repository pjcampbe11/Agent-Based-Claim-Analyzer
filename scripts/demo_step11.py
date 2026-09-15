"""Step 11 -- levelset, end to end, with no model and no network (doc 21).

Runs the instrument the way a researcher would: a thread, then a corpus of
standalone posts, then the aggregate. The point of the demo is the CONTRAST
between the two count columns, because that contrast is the tool's entire
statistical claim.

The stub model below is scripted, not clever. Everything interesting here --
thread weighting, the lints, out-of-scope exclusion, consensus demotion -- is
levelset's own code, which is what a demo should exercise.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_levelset():
    """By path. levelset is a standalone file and must not gain a package."""
    path = ROOT / "tools" / "levelset.py"
    spec = importlib.util.spec_from_file_location("levelset_demo", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ls = load_levelset()

# A scripted model. Keyed off a marker the demo puts in each post so the output
# is deterministic and the demo makes the same point every time it runs.
SCRIPT = {
    "[vote]": ("VOTE_TYPE", "elections"),
    "[fund]": ("FUNDING", "budget"),
    "[stat]": ("STAT_DENOMINATOR", "economy"),
    "[rule]": ("RULEMAKING", "environment"),
}


def complete(_system: str, user: str) -> str:
    post = user.split("--- post ---", 1)[-1]
    if "[none]" in post:
        return json.dumps({"in_scope": False, "topic": "other", "gaps": []})
    for marker, (code, topic) in SCRIPT.items():
        if marker in post:
            return json.dumps({
                "in_scope": True, "topic": topic,
                "gaps": [{
                    "code": code,
                    # Deliberately dirty: a URL, a bill number and a statement
                    # about a person, so the demo shows the lints working.
                    "phrase": "the poster linked https://example.gov/x about H.R. 12",
                    "how_this_generally_works": (
                        "These are two separate decisions, made at different "
                        "times, and the first one does not settle the second."
                    ),
                    "confidence": "high",
                }],
            })
    return json.dumps({"in_scope": True, "topic": "other", "gaps": []})


def rule(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> int:
    rule("1. the taxonomy the counts are built on")
    print(ls.render_taxonomy().split("\n\n")[0])
    print(f"  {len(ls.GAP_CODES)} codes, versioned separately from the tool")

    rule("2. one post")
    reading = ls.read_post("[vote] they voted it down, it is dead",
                           complete=complete, platform="demo")
    print(ls.render_reading(reading))
    print("\n  note what the lints did to the quoted span: the URL and the bill")
    print("  number are gone, and 'the poster' became 'the post'.")

    rule("3. a thread -- one parent, four replies echoing it")
    thread = "\n---\n".join([
        "[vote] they voted it down, it is dead",
        "[vote] exactly, they killed it",
        "[vote] this is what I keep saying",
        "[vote] same thing happened last year",
        "[vote] and nobody covered it",
    ])
    readings = ls.read_thread(thread, complete=complete, thread_id="viral-thread",
                              platform="demo")
    report = ls.aggregate(readings)
    counter = report.codes[0]
    print(f"  VOTE_TYPE   raw={counter.raw}   thr={counter.thr}")
    print()
    print("  THIS IS THE WHOLE POINT. Five posts carry the gap; ONE misunderstanding")
    print("  spread. Raw counts would let a single viral thread decide the finding,")
    print("  and the more viral the thread the more it would decide.")

    rule("4. a corpus of unrelated standalone posts")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        posts = {
            "a": "[vote] the senate voted it down so it is over",
            "b": "[fund] they authorized the money years ago, where is it",
            "c": "[stat] it went up 300 percent, that is the whole story",
            "d": "[rule] the agency announced it so it applies now",
            "e": "[none] lol",
            "f": "[stat] another 300 percent, nobody explains from what",
        }
        for name, text in posts.items():
            (root / f"{name}.txt").write_text(text, encoding="utf-8")

        records = root / "records"
        corpus = ls.run_corpus(
            sorted(root.glob("*.txt")), complete=complete, platform="demo",
            records_dir=records)
        print(f"  {len(corpus)} reading(s) written to records/")
        print("  standalone posts each get their own thread id, so raw == thr")
        print("  and nothing is distorted by machinery that was not needed.")

        rule("5. the aggregate -- the only thing this tool actually produces")
        reloaded = ls.load_records([str(records / "*.json")])
        print(ls.render_report(ls.aggregate(reloaded)))

    rule("6. consensus: a gap seen once in three runs is demoted, not dropped")
    calls = iter([
        json.dumps({"in_scope": True, "topic": "budget", "gaps": [
            {"code": "FUNDING", "phrase": "a", "how_this_generally_works": "b",
             "confidence": "high"},
            {"code": "SCORING", "phrase": "c", "how_this_generally_works": "d",
             "confidence": "high"}]}),
        json.dumps({"in_scope": True, "topic": "budget", "gaps": [
            {"code": "FUNDING", "phrase": "a", "how_this_generally_works": "b",
             "confidence": "high"}]}),
        json.dumps({"in_scope": True, "topic": "budget", "gaps": [
            {"code": "FUNDING", "phrase": "a", "how_this_generally_works": "b",
             "confidence": "high"}]}),
    ])
    reading = ls.read_post("x", complete=lambda _s, _u: next(calls), consensus=3)
    for entry in reading.gaps:
        note = entry.consensus_note or "seen in every run"
        print(f"  {entry.code:<20} {entry.confidence:<7} {note}")
    print()
    print("  dropping the weak one would bias every count downward by an amount")
    print("  that varies with how ambiguous the topic is -- invisible, and")
    print("  correlated with subject matter. Keeping it with its weakness")
    print("  recorded lets a reader discount it themselves.")

    rule("7. and none of it is publishable yet")
    print("  scripts/check_levelset_symmetry.py --run   (code half)")
    print("  scripts/check_levelset_symmetry.py --run --backend ollama  (model half)")
    print()
    print("  A gap report that leans one way would do more damage than no report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

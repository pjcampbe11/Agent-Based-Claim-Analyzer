"""The levelset corpus symmetry gate (doc 21 s8, item 1) -- THE BLOCKER.

WHY THIS IS A GATE AND NOT A FOLLOW-UP
======================================
levelset's product is an aggregate finding about political language: which
mechanisms Americans most often get wrong, and how often. Doc 21 s8 lists a
corpus symmetry check as the **highest-priority open item**, and the reason is
blunt: if the instrument records more comprehension gaps in posts of one
political valence than the other, the published report says one side of the
country understands its own government worse than the other.

That claim would be enormous, it is far likelier to be an artifact of the model
than a fact about the population, and **no reader could tell which from the
report alone**. A partisan-looking gap report would do more damage than no
report. So the report is not publishable until this passes, and the tool ships
saying so in its own footer.

WHAT IS MEASURED
================
Seven measures over 24 matched post pairs -- same comprehension gap, same
grammatical shape, comparable length, opposite political valence:

    in scope                 does one side get read as "making a claim" more often?
    any gap recorded         does one side get coded as carrying a gap more often?
    expected code recorded   is the instrument MORE ACCURATE on one side?
    gaps per post            the headline rate itself
    high-confidence gaps     does the instrument believe one side more readily?
    off-taxonomy coercions   does the taxonomy fit one side better?
    lints fired              does one side draw more citations, more statements
                             about people -- i.e. does the tool behave worse on it?

The last four are the ones that would be missed by anyone eyeballing readings.
Equal gap COUNTS with unequal confidence, or unequal accuracy, or unequal
taxonomy fit, is still a report that leans -- and it leans in a way that only
shows up in aggregate.

WHAT --run ACTUALLY PROVES, AND WHAT IT DOES NOT
================================================
By default the run uses a deterministic stub whose behaviour depends ONLY on the
hash of the post text -- never on its wording, its party names, or its side. A
stub that keyed off the text would be testing the stub. So the default run
measures the symmetry of levelset's own CODE: scope handling, deduplication, the
consensus survival rule, every lint, and the thread-weighted aggregation.

That is a real and necessary result, and it is not the whole gate. **Model
symmetry is the other half**, it needs a configured backend, and it is run with
``--backend``. Until a corpus has cleared BOTH halves, its report is not a
publishable finding. This script says which half it ran, every time, so that a green line in
CI can never be mistaken for the full clearance.

USAGE
=====
    python scripts/check_levelset_symmetry.py          # structural: are pairs matched?
    python scripts/check_levelset_symmetry.py --run    # + code symmetry, stub
    python scripts/check_levelset_symmetry.py --run --backend ollama --model llama3.1:8b

EXIT CODES
==========
    0  passed
    1  asymmetry detected
    2  indeterminate, or a malformed pair set -- neither is evidence of symmetry
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from abca.evals.stats import MINIMUM_DISCORDANT
from abca.evals.suites import EvalSetError, levelset_symmetry_path, load_pairs
from abca.evals.symmetry import SymmetryVerdict, compare

EXIT_ASYMMETRY = 1
EXIT_INDETERMINATE = 2

#: Above this share of unreadable replies the run is not a measurement.
#: Deliberately generous -- a real corpus run tolerates failures and reports
#: them -- but not unbounded: comparing two piles of errors and finding them
#: alike is not evidence that a model treats both sides alike.
MAX_FAILURE_RATE = 0.25

#: Below this share of in-scope readings the run measured nothing.
#:
#: Every pair in this corpus is a political claim about a mechanism -- that is
#: the entire selection criterion -- so a model finding almost none of them in
#: scope is not reporting on the corpus, it is failing to read it.
MIN_IN_SCOPE_RATE = 0.50


def load_levelset() -> Any:
    """Import ``tools/levelset.py`` by PATH, not as a package.

    Deliberate. levelset is a single standalone file that imports nothing from
    ``src/abca`` (doc 21 s7), and giving it a package identity here would be the
    first step toward it acquiring one. The CHECKER may import both worlds; the
    TOOL may not.
    """
    path = ROOT / "tools" / "levelset.py"
    spec = importlib.util.spec_from_file_location("levelset_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec_module: @dataclass resolves string annotations via
    # ``sys.modules[cls.__module__].__dict__``, so a module that is not yet in
    # sys.modules fails on its first dataclass with a bare AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def paired_completer(levelset: Any, pair: Any) -> Any:
    """A model stand-in that returns the SAME reply for both sides of a pair.

    WHY THIS IS PAIRED AND NOT INDEPENDENTLY RANDOM
    -----------------------------------------------
    The first version of this gate gave each side its own hash-seeded reply. It
    was valence-blind and it was still wrong, for a reason worth recording so
    nobody rebuilds it: two independent random draws per pair manufacture real
    random discordance, so every measure becomes a coin flip against noise that
    the harness itself injected. Re-salting the hash twelve times made the suite
    pass once and flag five different measures across the other eleven runs. A
    gate that fails eleven times in twelve for no reason gets switched off, and
    a switched-off gate is worse than none.

    The fix is to hold the model constant, because levelset's code is
    deterministic given a model reply. The structural half of the reply --
    in-scope, topic, which codes, how many, at what confidence -- is seeded off
    the PAIR (its ``structure_key`` and ``pair_id``), which is by construction
    identical for both sides and therefore carries no valence whatsoever.

    What is left varying between the two sides is exactly one thing: **the post's
    own words**, echoed into ``phrase`` the way a real model quotes the span it
    is describing. And the post's own words are the only input levelset's code
    ever reads directly -- through the lints, and through the scrubbing they do.

    So this run answers a precise question: *given identical model behaviour,
    does levelset's own code treat the two sides differently?* Every structural
    measure should come back with zero discordance, which the rule-of-three
    branch reports as the strongest available symmetry result. Any lint measure
    that moves is a regex that chews one side's ordinary wording and leaves the
    other's -- a real, invisible partisan effect, and the thing this gate exists
    to find.
    """
    codes = list(levelset.GAP_CODES)
    topics = list(levelset.TOPICS)

    # Seeded off pair identity only. Both sides of the pair hash to this.
    seed = int(hashlib.sha256(
        f"{pair.structure_key}|{pair.pair_id}".encode()).hexdigest()[:16], 16)

    # ~1 pair in 8 reads as out of scope, so the in-scope measure has variance
    # ACROSS the corpus while having none WITHIN a pair -- which is precisely
    # the shape a correct result should have.
    in_scope = (seed % 8) != 0

    # The pair's own gap code is always recorded, so "expected code recorded"
    # measures something. Extra codes are drawn from the seed, and two slots
    # past the end of the taxonomy produce off-taxonomy codes on purpose, so the
    # coercion path is exercised rather than assumed.
    extra = 1 + (seed >> 4) % 2
    picked: list[str] = [pair.structure_key]
    for index in range(extra):
        slot = (seed >> (8 + index * 5)) % (len(codes) + 2)
        picked.append(codes[slot] if slot < len(codes) else f"NOT_A_CODE_{slot}")

    confidences = [
        ["low", "medium", "high"][(seed >> (17 + i)) % 3] for i in range(len(picked))
    ]
    topic = topics[(seed >> 29) % len(topics)]

    def complete(_system: str, user: str) -> str:
        if not in_scope:
            return json.dumps({"in_scope": False, "topic": "other", "gaps": []})
        # The post text, as a real model would quote it. This is the ONLY thing
        # that differs between the two sides of a pair.
        post = user.split("--- post ---", 1)[-1].strip()
        return json.dumps({
            "in_scope": True,
            "topic": topic,
            "gaps": [
                {
                    "code": code,
                    "phrase": post,
                    "how_this_generally_works": (
                        "A step in this process happens before the one the "
                        "wording describes, and the two are decided separately."
                    ),
                    "confidence": confidence,
                }
                for code, confidence in zip(picked, confidences, strict=True)
            ],
        })

    return complete


def backend_completer(levelset: Any, args: argparse.Namespace) -> Any:
    """A real model, for the half of the gate a stub cannot cover."""
    if args.backend == "openai":
        return levelset.openai_completer(
            args.model, temperature=args.temperature, seed=args.seed,
            max_tokens=args.max_tokens)
    return levelset.ollama_completer(
        args.model, host=args.host, temperature=args.temperature,
        seed=args.seed, max_tokens=args.max_tokens)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="store_true",
                        help="read both sides of every pair and compare")
    parser.add_argument("--backend", choices=("stub", "ollama", "openai"),
                        default="stub",
                        help="stub measures levelset's CODE; a real backend "
                             "measures the MODEL. Both halves are required "
                             "before a report is publishable.")
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--consensus", type=int, default=1)
    parser.add_argument("--records-dir", metavar="DIR",
                        help="write every reading this gate made, as JSON. A "
                             "model-half PASS costs real inference and cannot be "
                             "reproduced by re-reading a number; writing the "
                             "readings makes the verdict auditable by hand.")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="use only the first N pairs. A SMOKE TEST, never a "
                             "gate: below the minimum pair count no result can "
                             "be significant, and the run says so and refuses to "
                             "report PASS.")
    args = parser.parse_args()

    path = levelset_symmetry_path()
    print("pair set\n")
    try:
        suite, pairs = load_pairs(path)
    except EvalSetError as error:
        print(f"  FAIL  {path.stem}")
        print(f"        {error}")
        print("\nA symmetry test is only as good as its pairs.")
        return EXIT_INDETERMINATE
    print(f"  OK    {suite:24} {len(pairs)} matched pair(s)")

    smoke = False
    if args.limit is not None:
        if args.limit < 1:
            print("  FAIL  --limit must be at least 1")
            return EXIT_INDETERMINATE
        pairs = pairs[:args.limit]
        smoke = len(pairs) < MINIMUM_DISCORDANT
        print(f"  NOTE  --limit {args.limit}: using {len(pairs)} pair(s)"
              + ("  *** SMOKE TEST, NOT A GATE ***" if smoke else ""))

    levelset = load_levelset()
    keys = sorted({p.structure_key for p in pairs})
    unknown = [key for key in keys if key not in levelset.GAP_CODES]
    if unknown:
        # A pair set whose structure keys are not taxonomy codes cannot support
        # the accuracy measure, and silently skipping it would hide that.
        print(f"  FAIL  structure keys not in {levelset.TAXONOMY_VERSION}: {unknown}")
        return EXIT_INDETERMINATE
    print(f"  OK    {len(keys)} distinct gap code(s) exercised, all in "
          f"{levelset.TAXONOMY_VERSION}")

    if not args.run:
        print(f"\nStructural checks passed. Run with --run to read both sides.\n"
              f"(Reminder: fewer than {MINIMUM_DISCORDANT} discordant pairs can "
              "never be significant at alpha=0.05.)")
        return 0

    half = ("levelset's CODE (paired stub: identical model reply per pair)"
            if args.backend == "stub"
            else f"the MODEL ({args.backend}/{args.model})")
    print(f"\nreading both sides of every pair · measuring {half}\n")

    shared = args.backend != "stub"
    backend = backend_completer(levelset, args) if shared else None

    def read(pair: Any, text: str) -> Any:
        """One reading, and a FAILED reading is a result rather than a crash.

        The corpus runner already survives a malformed model reply, because at
        ten thousand posts a malformed reply is a certainty. This gate calls
        read_post directly and used to inherit none of that -- so one truncated
        JSON object 40 minutes into a 64-call run against a real model ended the
        run with a traceback and no verdict. A gate that cannot survive the
        thing it is guaranteed to meet is a gate that never runs.

        Worse, discarding failures would bias the measurement: if the model
        fails more often on one side, dropping those readings would quietly
        remove the evidence of exactly the asymmetry being looked for. So a
        failure is recorded, counted, and measured as its own dimension.
        """
        complete = backend if shared else paired_completer(levelset, pair)
        try:
            return levelset.read_post(
                text, complete=complete, platform="eval", consensus=args.consensus)
        except levelset.LevelsetError as error:
            return levelset.Reading(
                text=text, in_scope=False, topic="other", platform="eval",
                error=str(error))
        except Exception as error:  # a bad reply must not end the run
            return levelset.Reading(
                text=text, in_scope=False, topic="other", platform="eval",
                error=f"{type(error).__name__}: {error}")

    records = Path(args.records_dir) if args.records_dir else None
    if records is not None:
        records.mkdir(parents=True, exist_ok=True)

    def read_side(pair: Any, text: str, side: str) -> Any:
        reading = read(pair, text)
        if records is not None:
            payload = reading.to_json()
            # The pair's identity travels with the reading so a human auditing
            # the file can line the two sides up. No author identity is added,
            # because levelset records none and this must not be where one
            # enters through the back door.
            payload["eval_pair_id"] = pair.pair_id
            payload["eval_side"] = side
            payload["eval_expected_code"] = pair.structure_key
            (records / f"{pair.pair_id}.{side}.json").write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return reading

    left = [read_side(p, p.left_claim, "left") for p in pairs]
    right = [read_side(p, p.right_claim, "right") for p in pairs]

    # Accuracy needs the pair's expected code, and compare() extracts from a
    # single output, so the pair's own code is zipped in here rather than
    # smuggled through a closure over a loop index.
    left_scored = [(r, p.structure_key in r.codes, len(p.left_claim))
                   for p, r in zip(pairs, left, strict=True)]
    right_scored = [(r, p.structure_key in r.codes, len(p.right_claim))
                    for p, r in zip(pairs, right, strict=True)]

    def redacted_chars(row: Any) -> int:
        """How much of the post the destructive lints actually removed.

        Counted separately from the number of lints because one redaction can
        remove three characters or eighty. A gate that watched only the COUNT
        would miss a regex that fires equally often on both sides and eats far
        more of one side's sentence -- which shows up in the published record as
        one side's language being systematically mangled.
        """
        reading, _hit, original = row
        kept = sum(len(g.phrase) for g in reading.gaps[:1])
        return max(0, original - kept) if reading.gaps else 0

    report = compare(
        suite, pairs, left_scored, right_scored,
        binary_measures={
            # First, because a difference here would confound every measure
            # below it: an unreadable post contributes no gaps, so a model that
            # fails more often on one side looks, to every other measure, like a
            # model that simply finds fewer gaps there.
            "reading failed": lambda row: bool(row[0].error),
            "in scope": lambda row: row[0].in_scope,
            "any gap recorded": lambda row: bool(row[0].gaps),
            "expected code recorded": lambda row: row[1],
        },
        count_measures={
            "gaps per post": lambda row: len(row[0].gaps),
            "high-confidence gaps": lambda row: sum(
                1 for g in row[0].gaps if g.confidence == "high"),
            "off-taxonomy coercions": lambda row: row[0].lints.codes_coerced,
            "lints fired": lambda row: row[0].lints.total,
            "characters redacted": redacted_chars,
        },
    )
    print(report.render())

    # A run in which the model mostly failed measured almost nothing, and its
    # PASS would be "we compared two piles of errors and they matched".
    failures = sum(1 for r in left + right if r.error)
    total_readings = 2 * len(pairs)
    print(f"\n  {failures} of {total_readings} reading(s) failed "
          f"({100.0 * failures / max(1, total_readings):.0f}%)")
    if failures:
        for reading in (r for r in left + right if r.error):
            print(f"    {reading.error[:100]}")
            break
    if failures > total_readings * MAX_FAILURE_RATE:
        print(f"\n  TOO MANY FAILED READINGS (over {MAX_FAILURE_RATE:.0%}). This "
              "model could not produce\n  usable readings for this corpus, so the "
              "comparison is between two piles of\n  errors. NOT evidence of "
              "symmetry. Raise --max-tokens or use a stronger model.")
        return EXIT_INDETERMINATE

    # A model that marks the corpus out of scope measured nothing either, and
    # this failure is quieter than the last one: every reading parses, every
    # measure returns a number, and the numbers agree because they are all
    # zero. Comparing two piles of empty readings and finding them alike is the
    # single most plausible way this gate could report a false PASS.
    #
    # Found by a real run: a 1.5B model marked 46 of 52 readable posts as making
    # no claim about a political mechanism. Every post in this corpus makes one
    # -- that is what the corpus is -- so the finding was about the model, and
    # nothing about symmetry.
    readings = [r for r in left + right if not r.error]
    in_scope = sum(1 for r in readings if r.in_scope)
    with_gaps = sum(1 for r in readings if r.gaps)
    print(f"  {in_scope} of {len(readings)} readable post(s) judged in scope; "
          f"{with_gaps} carried at least one gap")
    if readings and in_scope < len(readings) * MIN_IN_SCOPE_RATE:
        print(f"\n  TOO FEW POSTS IN SCOPE (under {MIN_IN_SCOPE_RATE:.0%}). Every "
              "post in this corpus\n  makes a claim about a political mechanism, "
              "so this is a finding about the\n  MODEL, not about symmetry: it "
              "produced almost no readings to compare.\n  NOT evidence of "
              "symmetry. Use a stronger model.")
        return EXIT_INDETERMINATE

    # The lints must have actually fired, or several measures tested nothing.
    fired = sum(r.lints.total for r in left) + sum(r.lints.total for r in right)
    print(f"\n  {fired} lint(s) fired across {2 * len(pairs)} readings; a gate in "
          "which no lint fires\n  is a gate that did not test the lints.")
    if fired == 0:
        print("\n  NO LINTS FIRED. The lint measures are vacuous and this run is "
              "not evidence.")
        return EXIT_INDETERMINATE

    print()
    if report.verdict is SymmetryVerdict.FAIL:
        print("ASYMMETRY DETECTED. A gap report from this configuration would say "
              "one side\nunderstands government worse than the other. It is not "
              "publishable.")
        return EXIT_ASYMMETRY
    if report.verdict is SymmetryVerdict.INDETERMINATE:
        print("INDETERMINATE. Not a failure, and NOT evidence of symmetry. Grow "
              "the pair set.")
        return EXIT_INDETERMINATE

    if smoke:
        print(f"Smoke test completed for {half}, and it is NOT a pass: "
              f"{len(pairs)} pair(s)\nis below the {MINIMUM_DISCORDANT} needed "
              "for any result to be significant. Drop --limit to gate.")
        return EXIT_INDETERMINATE

    print(f"PASSED for {half}.")
    if records is not None:
        print(f"{2 * len(pairs)} reading(s) written to {records}/ — a model-half "
              "verdict\nshould be spot-checked by hand, not taken on the exit code.")
    if args.backend == "stub":
        print("\nThis clears the CODE half only. A corpus is not publishable until "
              "it also\nclears --backend with the model that produced it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""levelset (doc 21) -- the tool, its lints, its aggregation, and its gate.

Doc 21 s6 lists five behaviours that must be verified against synthetic data
with no model required. All five are here, plus the two things the doc states as
constraints rather than behaviours -- isolation from the pipeline (s7) and the
absence of author identity (s4) -- because a constraint nobody tests is a
comment.

The most important test in this file is
``test_the_symmetry_gate_catches_a_lint_that_favours_one_side``. A control that
has never been shown to fail is not a control, it is decoration.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LEVELSET_PATH = ROOT / "tools" / "levelset.py"


def _load():
    spec = importlib.util.spec_from_file_location("levelset_under_test", LEVELSET_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ls = _load()


# ---------------------------------------------------------------- helpers


def replying(payload: dict) -> object:
    """A completer that always returns one fixed reply."""
    def complete(_system: str, _user: str) -> str:
        return json.dumps(payload)
    return complete


def gap(code: str, phrase: str = "a span", how: str = "how it works",
        confidence: str = "high") -> dict:
    return {"code": code, "phrase": phrase, "how_this_generally_works": how,
            "confidence": confidence}


def scoped(*codes: str, topic: str = "elections") -> dict:
    return {"in_scope": True, "topic": topic, "gaps": [gap(c) for c in codes]}


# ------------------------------------------- s7: isolation from the pipeline


def test_levelset_imports_nothing_from_the_pipeline():
    """Doc 21 s7. The provenance chain depends on this and nothing else does.

    Parsed from the source rather than checked at runtime, because a lazy import
    inside a function would pass a runtime check and still break the isolation.
    """
    tree = ast.parse(LEVELSET_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [name for name in imported if name.split(".")[0] == "abca"]
    assert not offenders, (
        f"levelset imports {offenders} from the pipeline. A component producing "
        "unhashed, uncited readings must not feed the component producing "
        "hashed, cited verdicts."
    )


def test_levelset_runs_with_the_pipeline_entirely_absent():
    """Stronger than the import lint: it must work with src/ off sys.path."""
    result = subprocess.run(
        [sys.executable, str(LEVELSET_PATH), "--taxonomy"],
        capture_output=True, text=True, cwd=str(ROOT.parent),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""}, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "gaps/1.0.0" in result.stdout


def test_the_taxonomy_has_exactly_the_twenty_two_specified_codes():
    assert len(ls.GAP_CODES) == 22
    assert ls.OTHER in ls.GAP_CODES
    # Grouping must not lose or duplicate a code.
    flattened = [c for group in ls.GAP_GROUPS.values() for c in group]
    assert len(flattened) == len(set(flattened)) == 22


# --------------------------------------------------- s6.1: thread splitting


def test_doubled_and_edge_delimiters_produce_no_phantom_entries():
    raw = "---\nparent\n---\n---\nfirst comment\n---\n\n---\n"
    assert ls.split_thread(raw) == ["parent", "first comment"]


def test_blank_lines_inside_an_entry_are_preserved():
    """A multi-paragraph post is ONE post. Splitting it would multiply it."""
    raw = "line one\n\nline two\n---\nreply"
    entries = ls.split_thread(raw)
    assert entries == ["line one\n\nline two", "reply"]


def test_a_delimiter_inside_a_line_does_not_split():
    """A post containing an em-dash must not shatter itself."""
    assert ls.split_thread("they said --- and then left") == [
        "they said --- and then left"]


def test_a_comment_is_read_with_its_parent_and_a_parent_is_not():
    seen: list[str] = []

    def complete(_system: str, user: str) -> str:
        seen.append(user)
        return json.dumps(scoped("VOTE_TYPE"))

    readings = ls.read_thread(
        "the parent post\n---\nfirst reply\n---\nsecond reply",
        complete=complete, thread_id="t1")
    assert [r.role for r in readings] == ["parent", "comment", "comment"]
    assert [r.position for r in readings] == [0, 1, 2]
    assert "parent post" in seen[0] and "COMMENT" not in seen[0]
    assert "COMMENT" in seen[1] and "the parent post" in seen[1]


# --------------------------------------------- s6.2: taxonomy normalization


@pytest.mark.parametrize("raw", ["vote_type", "Vote_Type", " VOTE_TYPE ", "vote-type"])
def test_lowercase_and_spaced_codes_map_to_the_taxonomy(raw):
    counts = ls.LintCounts()
    assert ls.normalise_code(raw, counts) == ("VOTE_TYPE", False)
    assert counts.codes_coerced == 0


@pytest.mark.parametrize("raw", ["", "   ", "NOT_A_CODE", "confusion", "42"])
def test_unknown_and_blank_codes_coerce_to_other_and_are_flagged(raw):
    counts = ls.LintCounts()
    assert ls.normalise_code(raw, counts) == (ls.OTHER, True)
    assert counts.codes_coerced == 1


def test_an_unparseable_confidence_defaults_to_the_weakest():
    """Defaulting down: an absence of information must not strengthen a finding."""
    assert ls.normalise_confidence("very high") == "low"
    assert ls.normalise_confidence("") == "low"
    assert ls.normalise_confidence("HIGH") == "high"


def test_an_off_list_topic_coerces_and_is_flagged():
    counts = ls.LintCounts()
    assert ls.normalise_topic("kittens", counts) == "other"
    assert counts.topics_coerced == 1


# ---------------------------------------------------------- s6.3: the lints


@pytest.mark.parametrize("text", [
    "see https://www.congress.gov/bill/117 for the text",
    "under 5 U.S.C. 552 it is required",
    "24 CFR 570.208 already says so",
    "10 ILCS 5/7-10 controls this",
    "Pub. L. 117-169 did it",
    "H.R. 1234 is the bill",
    "Roll Call 316 shows the vote",
    "597 U.S. 215 settled it",
])
def test_every_citation_form_is_redacted_including_correct_ones(text):
    """The point of the rule: an ACCURATE citation is worse than none here.

    A reader cannot distinguish one this tool retrieved from one it invented --
    it retrieved neither -- so a correct one teaches trust in a channel that
    will eventually hand them a fabrication.
    """
    counts = ls.LintCounts()
    out = ls.scrub(text, counts)
    assert counts.citations_redacted >= 1
    assert ls.CITATION_REPLACEMENT in out


def test_deficiency_attributions_are_removed():
    counts = ls.LintCounts()
    out = ls.scrub("the post doesn't understand cloture", counts)
    assert counts.deficiency_redacted == 1
    assert "doesn't understand" not in out
    assert ls.DEFICIENCY_REPLACEMENT in out


def test_the_poster_becomes_the_post_and_the_sentence_survives():
    """Doc 21 s4's worked example. Redaction here would destroy meaning."""
    counts = ls.LintCounts()
    out = ls.scrub("That is not the poster's fault.", counts)
    assert out == "That is not the post's fault."
    assert counts.author_rewritten == 1


@pytest.mark.parametrize("text,expected", [
    ("OP said it first", "the post said it first"),
    ("this person is angry", "this post is angry"),
    ("whoever wrote this is upset", "this post is upset"),
    ("the author means well", "the post means well"),
])
def test_bare_author_references_are_redirected_not_deleted(text, expected):
    assert ls.scrub(text, ls.LintCounts()) == expected


def test_verdict_vocabulary_is_flagged_but_left_in_place():
    """Flagged, not redacted: sometimes it describes the POST's stance."""
    counts = ls.LintCounts()
    out = ls.scrub("the post treats the claim as false and misleading", counts)
    assert counts.verdict_flagged == 2
    assert "false" in out and "misleading" in out


def test_clean_text_passes_through_untouched():
    text = "The wording assumes a procedural vote decides the policy."
    counts = ls.LintCounts()
    assert ls.scrub(text, counts) == text
    assert counts.total == 0


def test_ordinary_political_prose_does_not_trip_the_lints():
    """Over-eager regexes would mangle the corpus, which is its own bias."""
    for text in [
        "Every single senator voted for it and prices went up 300 percent.",
        "Gas was two dollars in January and four dollars in June.",
        "They authorized eight hundred million and none of it was spent.",
    ]:
        counts = ls.LintCounts()
        assert ls.scrub(text, counts) == text
        assert counts.total == 0, text


def test_a_url_in_the_quoted_span_is_redacted_too():
    """Otherwise a post smuggles an unverified citation in through the quote."""
    reading = ls.read_post(
        "look at this", complete=replying({
            "in_scope": True, "topic": "elections",
            "gaps": [gap("VOTE_TYPE", phrase="see https://example.gov/x")]}))
    assert "https://" not in reading.gaps[0].phrase
    assert reading.lints.citations_redacted == 1


def test_redaction_cannot_be_disabled():
    """Doc 21 s4: 'Redaction is destructive on purpose. There is no flag.'"""
    flags = ls.build_parser().format_help()
    for word in ("--no-redact", "--keep-citations", "--raw", "--disable-lint"):
        assert word not in flags


# ------------------------------------------------ no author identity, ever


def test_no_record_field_can_hold_an_author():
    fields = set(ls.Reading.__slots__) | set(ls.Gap.__slots__)
    for banned in ("author", "username", "handle", "user_id", "account",
                   "display_name", "profile", "name"):
        assert banned not in fields, f"{banned} would make this a file on people"


def test_a_thread_id_comes_from_the_filename_not_the_content(tmp_path):
    """A content hash of a post is a join key back to its author."""
    post = tmp_path / "some-thread.txt"
    post.write_text("parent\n---\nreply", encoding="utf-8")
    readings = ls.run_corpus(
        [post], complete=replying(scoped("VOTE_TYPE")), threaded=True)
    assert {r.thread_id for r in readings} == {"some-thread"}
    digest = hashlib.sha256(b"parent").hexdigest()
    assert digest not in json.dumps([r.to_json() for r in readings])


# ------------------------------------------------------- s6.4: aggregation


def test_a_four_post_thread_repeating_one_code_collapses_to_one_thread():
    """Doc 21 s3 and s6.4, the whole reason ``thr`` exists.

    A parent's error echoed by replies is one misunderstanding that spread.
    """
    readings = ls.read_thread(
        "parent\n---\nme too\n---\nsame\n---\nexactly",
        complete=replying(scoped("VOTE_TYPE")), thread_id="t1")
    report = ls.aggregate(readings)
    assert [(c.code, c.raw, c.thr) for c in report.codes] == [("VOTE_TYPE", 4, 1)]
    assert report.threads == 1


def test_standalone_posts_give_raw_equal_to_thr():
    """The machinery must not distort a corpus that never needed it."""
    readings = [
        ls.read_post(f"post {i}", complete=replying(scoped("FUNDING")))
        for i in range(5)
    ]
    report = ls.aggregate(readings)
    assert [(c.raw, c.thr) for c in report.codes] == [(5, 5)]
    assert report.threads == 5


def test_out_of_scope_posts_leave_the_denominator():
    readings = [
        ls.read_post("a slogan", complete=replying(
            {"in_scope": False, "topic": "other", "gaps": []})),
        ls.read_post("a claim", complete=replying(scoped("VOTE_TYPE"))),
    ]
    report = ls.aggregate(readings)
    assert (report.posts, report.in_scope, report.out_of_scope) == (2, 1, 1)
    # 1 of 1 in-scope, not 1 of 2.
    assert "100.0%" in ls.render_report(report)


def test_an_out_of_scope_post_carries_no_gaps_even_if_the_model_offered_some():
    reading = ls.read_post("lol", complete=replying(
        {"in_scope": False, "topic": "other", "gaps": [gap("VOTE_TYPE")]}))
    assert reading.gaps == []


def test_one_code_listed_three_ways_is_one_gap():
    """Verbosity must not move the aggregate."""
    reading = ls.read_post("x", complete=replying({
        "in_scope": True, "topic": "economy",
        "gaps": [gap("VOTE_TYPE", "a"), gap("VOTE_TYPE", "b"), gap("VOTE_TYPE", "c")]}))
    assert reading.codes == ["VOTE_TYPE"]


def test_co_occurrence_is_thread_weighted_like_the_headline_count():
    readings = ls.read_thread(
        "parent\n---\nsame\n---\nsame again",
        complete=replying(scoped("VOTE_TYPE", "FUNDING")), thread_id="t1")
    report = ls.aggregate(readings)
    assert report.co_occurrence == [("VOTE_TYPE", "FUNDING", 1)] or \
           report.co_occurrence == [("FUNDING", "VOTE_TYPE", 1)]


# ------------------------------------------------------------- consensus


def test_a_gap_surviving_one_run_of_three_is_demoted_and_kept():
    """Doc 21 s5. Dropping it would bias every count downward, unevenly."""
    replies = [
        json.dumps(scoped("VOTE_TYPE", "FUNDING")),
        json.dumps(scoped("VOTE_TYPE")),
        json.dumps(scoped("VOTE_TYPE")),
    ]
    calls = iter(replies)

    def complete(_s, _u):
        return next(calls)

    reading = ls.read_post("x", complete=complete, consensus=3)
    by_code = {g.code: g for g in reading.gaps}
    assert set(by_code) == {"VOTE_TYPE", "FUNDING"}
    assert by_code["FUNDING"].confidence == "low"
    assert "1 of 3 runs" in by_code["FUNDING"].consensus_note
    assert by_code["VOTE_TYPE"].consensus_note == ""


# ------------------------------------------------- failures and robustness


def test_a_malformed_reply_fails_one_post_and_not_the_run(tmp_path):
    good, bad = tmp_path / "a.txt", tmp_path / "b.txt"
    good.write_text("a claim", encoding="utf-8")
    bad.write_text("another claim", encoding="utf-8")
    calls = {"n": 0}

    def complete(_s, _u):
        calls["n"] += 1
        return "not json at all" if calls["n"] == 1 else json.dumps(scoped("VOTE_TYPE"))

    readings = ls.run_corpus(sorted([good, bad]), complete=complete)
    report = ls.aggregate(readings)
    assert report.failures == 1
    assert report.in_scope == 1
    assert "failed" in ls.render_report(report)


def test_json_wrapped_in_a_fence_or_prose_is_still_parsed():
    for wrapper in ["```json\n{}\n```", "Here you go:\n{}", "{}"]:
        payload = wrapper.replace("{}", json.dumps(scoped("VOTE_TYPE")))
        reading = ls.read_post("x", complete=lambda _s, _u, p=payload: p)
        assert reading.codes == ["VOTE_TYPE"]


# ---------------------------------------------------------- s6.5: report


def test_the_report_renders_every_section_doc_21_requires():
    readings = ls.read_thread(
        "parent\n---\nreply", complete=replying(scoped("VOTE_TYPE", "FUNDING")),
        thread_id="t1")
    out = ls.render_report(ls.aggregate(readings))
    for section in ("comprehension gaps", "thr", "raw",
                    "gaps that travel together", "topics", "confidence",
                    "lints", "NOT PUBLISHABLE"):
        assert section in out, section


def test_the_report_says_it_is_not_publishable_until_the_gate_passes():
    out = ls.render_report(ls.aggregate([]))
    assert "symmetry gate" in out
    assert "check_levelset_symmetry.py" in out


def test_the_json_report_is_serialisable_and_carries_its_versions():
    report = ls.aggregate([ls.read_post("x", complete=replying(scoped("VOTE_TYPE")))])
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["tool"] == ls.TOOL_VERSION
    assert payload["taxonomy"] == ls.TAXONOMY_VERSION
    assert payload["prompt"] == ls.PROMPT_VERSION


def test_records_round_trip_through_disk(tmp_path):
    post = tmp_path / "p.txt"
    post.write_text("a claim", encoding="utf-8")
    records = tmp_path / "records"
    ls.run_corpus([post], complete=replying(scoped("VOTE_TYPE")),
                  records_dir=records)
    reloaded = ls.load_records([str(records / "*.json")])
    assert [r.codes for r in reloaded] == [["VOTE_TYPE"]]
    assert ls.aggregate(reloaded).in_scope == 1


# ------------------------------------------------------------------- CLI


def test_the_cli_offers_no_url_input():
    """Deliberate. A URL means a source exists, so the claim is Lane A's."""
    help_text = ls.build_parser().format_help()
    assert "--url" not in help_text
    assert " -u " not in help_text


def test_taxonomy_and_version_need_no_model():
    assert ls.main(["--version"]) == 0
    assert ls.main(["--taxonomy"]) == 0


def test_choosing_two_inputs_is_an_error():
    assert ls.main(["-t", "a", "--corpus", "b"]) == 2


def test_a_single_post_renders(capsys):
    code = ls.main(["-t", "they voted it down"],
                   complete=replying(scoped("VOTE_TYPE")))
    assert code == 0
    assert "VOTE_TYPE" in capsys.readouterr().out


def test_a_post_from_a_file_renders(tmp_path, capsys):
    post = tmp_path / "p.txt"
    post.write_text("they voted it down", encoding="utf-8")
    assert ls.main(["-f", str(post)], complete=replying(scoped("FUNDING"))) == 0
    assert "FUNDING" in capsys.readouterr().out


def test_a_thread_run_writes_one_record_per_entry(tmp_path):
    thread = tmp_path / "t.txt"
    thread.write_text("parent\n---\nreply", encoding="utf-8")
    records = tmp_path / "rec"
    out = tmp_path / "report.json"
    code = ls.main(
        ["--thread", str(thread), "--records-dir", str(records),
         "--format", "json", "--out", str(out)],
        complete=replying(scoped("VOTE_TYPE")))
    assert code == 0
    assert sorted(p.name for p in records.glob("*.json")) == [
        "t.0000.json", "t.0001.json"]
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["threads"] == 1
    assert payload["codes"][0] == {"code": "VOTE_TYPE", "raw": 2, "thr": 1}


def test_a_corpus_run_then_a_report_run_agree(tmp_path, capsys):
    for name in "abc":
        (tmp_path / f"{name}.txt").write_text("a claim", encoding="utf-8")
    records = tmp_path / "rec"
    assert ls.main(
        ["--corpus", str(tmp_path / "*.txt"), "--records-dir", str(records)],
        complete=replying(scoped("VOTE_TYPE"))) == 0
    capsys.readouterr()
    # --report needs no model at all: it aggregates records already on disk.
    assert ls.main(["--report", str(records / "*.json"), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["posts"] == 3
    assert payload["codes"][0]["thr"] == 3


def test_a_corpus_of_threads_uses_the_thread_glob_flag(tmp_path, capsys):
    (tmp_path / "one.txt").write_text("p\n---\nr\n---\nr2", encoding="utf-8")
    (tmp_path / "two.txt").write_text("p\n---\nr", encoding="utf-8")
    assert ls.main(
        ["--corpus", str(tmp_path / "*.txt"), "--thread-glob", "--format", "json"],
        complete=replying(scoped("VOTE_TYPE"))) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["threads"] == 2
    assert payload["codes"][0] == {"code": "VOTE_TYPE", "raw": 5, "thr": 2}


def test_a_corpus_that_matches_nothing_is_an_error():
    assert ls.main(["--corpus", "/nonexistent/*.txt"],
                   complete=replying(scoped("VOTE_TYPE"))) == 2


def test_a_report_that_matches_nothing_is_an_error():
    assert ls.main(["--report", "/nonexistent/*.json"]) == 2


def test_consensus_below_one_is_rejected():
    assert ls.main(["-t", "x", "--consensus", "0"],
                   complete=replying(scoped("VOTE_TYPE"))) == 2


def test_the_taxonomy_can_be_emitted_as_json(capsys):
    assert ls.main(["--taxonomy", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["taxonomy"] == ls.TAXONOMY_VERSION
    assert len(payload["codes"]) == 22


def test_the_openai_backend_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ls.LevelsetError, match="OPENAI_API_KEY"):
        ls.openai_completer("gpt-x")


def test_a_non_http_backend_url_is_refused():
    """Belt and braces around the one place this tool opens a socket."""
    with pytest.raises(ls.LevelsetError, match="non-HTTP"):
        ls._post_json("file:///etc/passwd", {}, {}, 1.0)


# ------------------------------------------- THE GATE ITSELF MUST BE ABLE TO FAIL


def _gate():
    spec = importlib.util.spec_from_file_location(
        "gate_under_test", ROOT / "scripts" / "check_levelset_symmetry.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_symmetry_gate_passes_on_the_shipped_corpus():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_levelset_symmetry.py"), "--run"],
        capture_output=True, text=True, cwd=str(ROOT), check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "levelset/symmetry: PASS" in result.stdout


def test_the_symmetry_gate_catches_a_lint_that_favours_one_side():
    """The control, controlled.

    A lint whose vocabulary happens to align with one side of the debate is the
    exact failure this gate exists to find. It is invisible in any single
    reading -- every redaction it makes is a redaction the rule permits -- and
    in aggregate it eats one side's language and leaves the other's intact.

    Here one is injected: a redaction pattern built from policy words that this
    corpus's left-hand claims use and its right-hand claims mostly do not. It
    touches 22 of 32 pairs, and the gate must fail.
    """
    import re as _re

    from abca.evals.suites import levelset_symmetry_path, load_pairs
    from abca.evals.symmetry import SymmetryVerdict, compare

    gate = _gate()
    suite, pairs = load_pairs(levelset_symmetry_path())

    biased = _re.compile(
        r"\b(?:Democrats|student|climate|transit|insulin|abortion|EPA|broadband"
        r"|Inflation|Social Security|minimum wage|tenant|overtime|police reform"
        r"|medical debt|assault weapons|schools|wages|crime|military)\b",
        _re.IGNORECASE,
    )
    original = list(ls.CITATION_PATTERNS)
    ls.CITATION_PATTERNS.append(biased)
    try:
        left, right = [], []
        for pair in pairs:
            complete = gate.paired_completer(ls, pair)
            left.append(ls.read_post(pair.left_claim, complete=complete))
            right.append(ls.read_post(pair.right_claim, complete=complete))
        report = compare(
            suite, pairs, left, right,
            count_measures={"lints fired": lambda r: r.lints.total},
        )
    finally:
        ls.CITATION_PATTERNS[:] = original

    assert report.verdict is SymmetryVerdict.FAIL, (
        "a lint biased toward one side's vocabulary was NOT caught; the gate "
        "would pass a partisan instrument:\n" + report.render()
    )


def test_the_symmetry_gate_catches_a_model_that_reads_one_side_harder():
    """The other half of the failure space: an even tool, a leaning model.

    Nothing in levelset's code is asymmetric here. The model simply finds one
    more gap in every left-hand post than in its matched right-hand twin --
    which is what a real partisan model looks like, and which no per-reading
    review would flag, because finding a second gap in a post is never wrong on
    its own.
    """
    from abca.evals.suites import levelset_symmetry_path, load_pairs
    from abca.evals.symmetry import SymmetryVerdict, compare

    _suite, pairs = load_pairs(levelset_symmetry_path())

    def leaning(pair):
        def complete(_system: str, user: str) -> str:
            post = user.split("--- post ---", 1)[-1].strip()
            codes = [pair.structure_key]
            if post == pair.left_claim.strip():
                codes.append("CAUSAL" if pair.structure_key != "CAUSAL" else "TIMING")
            return json.dumps({
                "in_scope": True, "topic": "elections",
                "gaps": [gap(c) for c in codes]})
        return complete

    left, right = [], []
    for pair in pairs:
        complete = leaning(pair)
        left.append(ls.read_post(pair.left_claim, complete=complete))
        right.append(ls.read_post(pair.right_claim, complete=complete))

    report = compare(
        "leaning-model", pairs, left, right,
        count_measures={"gaps per post": lambda r: len(r.gaps)},
    )
    assert report.verdict is SymmetryVerdict.FAIL, report.render()


def test_a_bias_too_small_to_detect_is_reported_indeterminate_and_never_pass():
    """The gate's sensitivity limit, asserted rather than discovered later.

    A bias touching only a handful of pairs cannot reach significance at
    alpha=0.05 with a corpus this size. The failure mode to guard against is not
    that the gate misses it -- it must -- but that it reports the miss as
    evidence of symmetry. It must say INDETERMINATE, which is a refusal to
    conclude, and never PASS, which would be a false claim of verification.
    """
    import re as _re

    from abca.evals.suites import levelset_symmetry_path, load_pairs
    from abca.evals.symmetry import SymmetryVerdict, compare

    gate = _gate()
    _suite, pairs = load_pairs(levelset_symmetry_path())

    narrow = _re.compile(r"\b(?:insulin|broadband|overtime)\b", _re.IGNORECASE)
    original = list(ls.CITATION_PATTERNS)
    ls.CITATION_PATTERNS.append(narrow)
    try:
        left, right = [], []
        for pair in pairs:
            complete = gate.paired_completer(ls, pair)
            left.append(ls.read_post(pair.left_claim, complete=complete))
            right.append(ls.read_post(pair.right_claim, complete=complete))
        report = compare(
            "narrow-bias", pairs, left, right,
            count_measures={"lints fired": lambda r: r.lints.total},
        )
    finally:
        ls.CITATION_PATTERNS[:] = original

    assert report.verdict is SymmetryVerdict.INDETERMINATE, report.render()
    assert not report.publishable
    assert "NOT evidence of symmetry" in report.render()


def test_the_gate_refuses_to_call_a_vacuous_run_evidence():
    """If no lint ever fires, the lint measures tested nothing and must not PASS."""
    source = (ROOT / "scripts" / "check_levelset_symmetry.py").read_text(encoding="utf-8")
    assert "NO LINTS FIRED" in source
    assert "not evidence" in source


def test_every_structure_key_in_the_corpus_is_a_real_taxonomy_code():
    from abca.evals.suites import levelset_symmetry_path, load_pairs

    _suite, pairs = load_pairs(levelset_symmetry_path())
    unknown = sorted({p.structure_key for p in pairs} - set(ls.GAP_CODES))
    assert not unknown, unknown

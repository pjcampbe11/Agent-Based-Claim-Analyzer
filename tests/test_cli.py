"""CLI tests.

Exercised through typer's CliRunner so the tests cover argument parsing,
exit codes and output shape -- the parts a user actually touches.

Exit codes are asserted explicitly because CI will gate on them.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from abca.cli.main import app
from abca.ledger.store import LedgerStore

runner = CliRunner()


def _invoke(args, ledger_root=None, columns: int = 200):
    """Run the CLI with the ledger pointed at a scratch directory.

    ``columns`` widens the virtual terminal so table assertions test CONTENT
    rather than rich's truncation behavior at an arbitrary default width. The
    narrow-terminal case is covered separately by
    ``test_narrow_terminal_keeps_run_id_and_repro``.
    """
    full = list(args)
    if ledger_root is not None:
        full += ["--ledger-root", str(ledger_root)]
    return runner.invoke(app, full, env={"COLUMNS": str(columns)})


class TestVersion:
    def test_reports_all_three_versions(self):
        """Tool, schema and contract answer different questions; all are shown."""
        result = runner.invoke(app, ["version", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert set(payload) == {"tool_version", "schema_version", "prompt_contract_version"}


class TestLedgerCommands:
    def test_list_empty(self, tmp_path):
        result = _invoke(["ledger", "list"], tmp_path)
        assert result.exit_code == 0
        assert "No runs" in result.stdout

    def test_list_after_write(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "list", "--json"], tmp_path)
        assert result.exit_code == 0
        rows = json.loads(result.stdout)
        assert rows[0]["run_id"] == record.run_id
        assert rows[0]["reproducible"] is True

    def test_show_json_round_trips(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "show", record.run_id, "--json"], tmp_path)
        assert result.exit_code == 0
        assert json.loads(result.stdout)["ledger_hash"] == record.ledger_hash

    def test_show_missing_run_exits_1(self, tmp_path):
        result = _invoke(["ledger", "show", "01M1MSX6D1ND36Q6ZPYC5CYWWP"], tmp_path)
        assert result.exit_code == 1

    def test_show_displays_a_damaged_record(self, tmp_path, record):
        """Hiding a corrupt record behind an exception defeats the investigation."""
        store = LedgerStore(tmp_path)
        path = store.write(record)
        payload = json.loads(path.read_text())
        payload["result"]["claims"][0]["verdict"] = "SUPPORTED"
        path.write_text(json.dumps(payload))

        result = _invoke(["ledger", "show", record.run_id], tmp_path)
        assert result.exit_code == 0
        assert "Integrity problems" in result.stdout

    def test_audit_clean_exits_0(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "audit", "--json"], tmp_path)
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["failures"] == 0
        assert payload["results"][0]["status"] == "intact"

    def test_audit_tampered_exits_6(self, tmp_path, record):
        """Exit 6 is what a CI gate on ledger integrity keys off."""
        store = LedgerStore(tmp_path)
        path = store.write(record)
        payload = json.loads(path.read_text())
        payload["result"]["claims"][0]["confidence"] = 0.01
        path.write_text(json.dumps(payload))

        result = _invoke(["ledger", "audit", "--json"], tmp_path)
        assert result.exit_code == 6
        assert json.loads(result.stdout)["results"][0]["status"] == "tampered"

    def test_path_prints_the_store_root(self, tmp_path):
        result = _invoke(["ledger", "path"], tmp_path)
        assert result.exit_code == 0
        assert str(tmp_path) in result.stdout


class TestVerifyCommand:
    def test_integrity_only_exits_0_on_an_intact_record(self, tmp_path, record):
        """Exit code 7 is gone: replay is implemented, so there is no half-answer."""
        LedgerStore(tmp_path).write(record)
        result = _invoke(["verify", record.run_id, "--integrity-only", "--json"], tmp_path)
        assert result.exit_code == 0
        assert json.loads(result.stdout)["integrity"] == "intact"

    def test_replay_without_the_input_is_unreplayable_not_a_pass(self, tmp_path, record):
        """A run whose input is unavailable cannot be certified.

        Records store the input's hash, not its text, so a third party has to
        bring the statement. Exit 4 (UNREPLAYABLE) rather than 0 is the whole
        point: no comparison happened.
        """
        LedgerStore(tmp_path).write(record)
        result = _invoke(["verify", record.run_id], tmp_path)
        assert result.exit_code == 4
        assert "not available locally" in result.stdout + result.stderr

    def test_tampered_record_exits_6(self, tmp_path, record):
        store = LedgerStore(tmp_path)
        path = store.write(record)
        payload = json.loads(path.read_text())
        payload["input_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(payload))

        result = _invoke(["verify", record.run_id, "--json"], tmp_path)
        assert result.exit_code == 6
        assert json.loads(result.stdout)["integrity"] == "tampered"

    def test_missing_run_exits_1(self, tmp_path):
        assert _invoke(["verify", "01M1MSX6D1ND36Q6ZPYC5CYWWP"], tmp_path).exit_code == 1


class TestSchemaExport:
    def test_emits_valid_json_schema(self):
        """Published so a third party can validate a record without this tool."""
        result = runner.invoke(app, ["schema"])
        assert result.exit_code == 0
        document = json.loads(result.stdout)
        assert document["$schema"].startswith("https://json-schema.org/")
        assert "properties" in document
        assert "ledger_hash" in document["properties"]

    def test_writes_to_file(self, tmp_path):
        target = tmp_path / "run-record.schema.json"
        result = runner.invoke(app, ["schema", "--out", str(target)])
        assert result.exit_code == 0
        assert json.loads(target.read_text())["properties"]["run_id"]


class TestAnalyzeIsAbsent:
    def test_analyze_is_not_registered_yet(self):
        """A command that pretends to work is worse than one that is absent."""
        result = runner.invoke(app, ["analyze", "-t", "anything"])
        assert result.exit_code != 0


class TestHumanReadableOutput:
    """The rendered (non-JSON) paths are what a user actually sees.

    Asserted on content, not layout, so a cosmetic table change does not
    break the suite.
    """

    def test_version_table(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "prompt contract" in result.stdout

    def test_list_table_shows_reproducibility(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "list"], tmp_path)
        assert record.run_id in result.stdout
        assert "yes" in result.stdout

    def test_show_table_reports_verdict_histogram(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "show", record.run_id], tmp_path)
        assert "MIXED" in result.stdout
        assert "intact" in result.stdout

    def test_show_stages_renders_the_chain(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "show", record.run_id, "--stages"], tmp_path)
        assert "stage chain" in result.stdout
        assert "adjudicate" in result.stdout

    def test_audit_human_output_says_all_intact(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "audit"], tmp_path)
        assert "all intact" in result.stdout

    def test_audit_human_output_lists_problems(self, tmp_path, record):
        store = LedgerStore(tmp_path)
        path = store.write(record)
        payload = json.loads(path.read_text())
        payload["output_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(payload))

        result = _invoke(["ledger", "audit"], tmp_path)
        assert result.exit_code == 6
        assert "TAMPERED" in result.stdout
        assert "output_digest mismatch" in result.stdout.replace("\n", "")

    def test_audit_on_empty_store(self, tmp_path):
        result = _invoke(["ledger", "audit"], tmp_path)
        assert result.exit_code == 0
        assert "No runs" in result.stdout

    def test_verify_human_output_explains_why_replay_was_refused(self, tmp_path, record):
        LedgerStore(tmp_path).write(record)
        result = _invoke(["verify", record.run_id], tmp_path)
        assert result.exit_code == 4
        assert "--input" in result.stdout + result.stderr

    def test_audit_named_missing_run_exits_6(self, tmp_path):
        result = _invoke(["ledger", "audit", "01M1MSX6D1ND36Q6ZPYC5CYWWP"], tmp_path)
        assert result.exit_code == 6
        assert "MISSING" in result.stdout


class TestNarrowTerminal:
    def test_narrow_terminal_keeps_run_id_and_repro(self, tmp_path, record):
        """On an 80-column terminal, `profile` truncates -- never the run id.

        A truncated run id is a run id nobody can copy into a bug report, and
        a truncated reproducibility flag is a status nobody can read.
        """
        LedgerStore(tmp_path).write(record)
        result = _invoke(["ledger", "list"], tmp_path, columns=80)
        assert record.run_id in result.stdout
        assert "yes" in result.stdout


# ==========================================================================
# Step 2: models and config commands
# ==========================================================================


class TestConfigCommands:
    def test_path_prints_a_location(self):
        result = runner.invoke(app, ["config", "path"])
        assert result.exit_code == 0
        assert "abca" in result.stdout

    def test_init_writes_a_documented_starter(self, tmp_path):
        target = tmp_path / "config.toml"
        result = runner.invoke(app, ["config", "init", "--config", str(target)])
        assert result.exit_code == 0
        body = target.read_text()
        # The starter file must answer "where does my API key go?" on its own.
        assert "OPENAI_API_KEY" in body
        assert "ANTHROPIC_API_KEY" in body
        assert "backtranslate" in body

    def test_init_refuses_to_clobber_without_force(self, tmp_path):
        target = tmp_path / "config.toml"
        runner.invoke(app, ["config", "init", "--config", str(target)])
        assert runner.invoke(app, ["config", "init", "--config", str(target)]).exit_code == 8
        assert runner.invoke(
            app, ["config", "init", "--config", str(target), "--force"]
        ).exit_code == 0

    def test_show_redacts_inline_keys(self, tmp_path):
        target = tmp_path / "config.toml"
        target.write_text(
            '[models.adjudicator]\nprovider="openai"\nmodel="gpt-4o"\napi_key="sk-LEAK"\n'
        )
        result = runner.invoke(app, ["config", "show", "--config", str(target), "--json"])
        assert result.exit_code == 0
        assert "sk-LEAK" not in result.stdout
        assert "inline (config file)" in result.stdout

    def test_show_reports_a_bad_config_cleanly(self, tmp_path):
        target = tmp_path / "config.toml"
        target.write_text('[models.adjudicater]\nmodel="x"\n')
        result = runner.invoke(app, ["config", "show", "--config", str(target)])
        assert result.exit_code == 8


class TestModelsCommands:
    def test_check_reports_failures_and_exits_9(self, tmp_path):
        """Every backend failure at once, so fixing them is not a game of whack-a-mole."""
        target = tmp_path / "config.toml"
        target.write_text('[models.adjudicator]\nprovider="ollama"\nmodel="nope"\n'
                          'base_url="http://127.0.0.1:59999"\n')
        result = runner.invoke(app, ["models", "check", "--config", str(target), "--json"])
        assert result.exit_code == 9
        payload = json.loads(result.stdout)
        assert payload["failures"] == ["adjudicator"]

    def test_grammar_emits_gbnf(self):
        result = runner.invoke(app, ["models", "grammar", "claim"])
        assert result.exit_code == 0
        assert result.stdout.startswith("root ::=")

    def test_grammar_writes_to_a_file(self, tmp_path):
        target = tmp_path / "claim.gbnf"
        result = runner.invoke(app, ["models", "grammar", "analysis", "--out", str(target)])
        assert result.exit_code == 0
        assert target.read_text().startswith("root ::=")

    def test_grammar_rejects_an_unknown_schema(self):
        assert runner.invoke(app, ["models", "grammar", "nonsense"]).exit_code == 1

    def test_show_reports_a_missing_role_cleanly(self, tmp_path):
        target = tmp_path / "config.toml"
        target.write_text('[models.adjudicator]\nmodel="x"\n')
        result = runner.invoke(app, ["models", "show", "embedder", "--config", str(target)])
        assert result.exit_code == 9


# ==========================================================================
# Step 3: the analyze command
# ==========================================================================


#: Real excerpts from 10 ILCS 5/10-2, used so the mock's quotes verify against
#: genuine statutory text rather than against a fixture written to match them.
STATUTE_FIXTURE = """(10 ILCS 5/10-2) (from Ch. 46, par. 10-2)

Sec. 10-2.
The term "political party", as hereinafter used in this
Article 10, shall mean any "established political party".

A political party which, at the last general election for State and
county officers, polled for its candidate for Governor more than 5% of
the entire vote cast for Governor, is hereby declared to be an
"established political party".

Any group of persons hereafter desiring to form a new political party
throughout the State shall file a petition signed by 1% of the number
of voters who voted at the next preceding Statewide general election or
25,000 qualified voters, whichever is less, and shall at the time of
filing contain a complete list of candidates of such party for all
offices to be filled in the State at the next ensuing election.
"""


class _AnalyzeHarness:
    """Shared fixtures for every test that drives `abca analyze`.

    Exercised through a mock Ollama on a loopback port.

    The mock is threaded deliberately: abCA builds one provider per role and
    each holds its own keep-alive connection, so a single-threaded server
    deadlocks on the second one. Real Ollama is concurrent.
    """

    @pytest.fixture
    def backend(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from mock_ollama import MODEL, serve

        httpd, port = serve()
        yield MODEL, port, httpd
        httpd.shutdown()

    @pytest.fixture
    def config_file(self, tmp_path, backend):
        model, port, _ = backend
        path = tmp_path / "config.toml"
        path.write_text(
            f'[models.classifier]\nprovider="ollama"\nmodel="{model}"\n'
            f'base_url="http://127.0.0.1:{port}"\n\n'
            f'[models.adjudicator]\nprovider="ollama"\nmodel="{model}"\n'
            f'base_url="http://127.0.0.1:{port}"\n',
            encoding="utf-8",
        )
        return path

    STATEMENT = (
        "Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures to start a new "
        "party, and that's the only real hurdle. The whole thing is rigged against "
        "outsiders. Last week the Secretary of State said the process is "
        "straightforward. Anyway, my dog is asleep on the couch."
    )

    @pytest.fixture(autouse=True)
    def offline_sources(self, tmp_path, monkeypatch):
        """Seed a source cache and force --offline, so tests never hit ilga.gov.

        A unit test that reaches a state legislature's web server is slow,
        flaky, and rude. The ILCS connector's live behaviour is covered by a
        separate, explicitly-marked network test.
        """
        from datetime import UTC, datetime

        from abca.sources.base import build_document
        from abca.sources.cache import SourceCache
        from abca.sources.ilcs import ILCSConnector

        cache_root = tmp_path / "sources"
        cache = SourceCache(cache_root)
        cache.put(build_document(
            connector=ILCSConnector(cache=cache, offline=True),
            doc_id="s-001",
            title="10 ILCS 5/10-2",
            url=("https://www.ilga.gov/documents/legislation/ilcs/documents/"
                 "001000050K10-2.htm"),
            text=STATUTE_FIXTURE,
            locator="10 ILCS 5/10-2",
            retrieved_at=datetime(2026, 9, 1, tzinfo=UTC),
        ))
        monkeypatch.setenv("ABCA_SOURCE_CACHE", str(cache_root))
        return cache_root

    def _run_analyze(self, config_file, ledger_root, *extra):
        return _invoke(
            ["analyze", "-t", self.STATEMENT, "--config", str(config_file),
             "--ledger-root", str(ledger_root), "--offline", *extra]
        )

    # Kept as the historical name used by the step-3 tests below.
    _run = _run_analyze


class TestAnalyzeCommand(_AnalyzeHarness):
    """End-to-end behaviour of the analyze command."""

    def test_end_to_end_writes_an_intact_record(self, config_file, tmp_path):
        from abca.ledger.store import LedgerStore

        ledger_root = tmp_path / "runs"
        result = self._run(config_file, ledger_root)
        assert result.exit_code == 0, result.stdout + result.stderr

        store = LedgerStore(ledger_root)
        run_ids = store.list_ids()
        assert len(run_ids) == 1
        # read() audits by default: this asserts every digest and chain link.
        record = store.read(run_ids[0])
        assert record.is_intact()
        assert len(record.result.claims) == 5

    def test_output_distinguishes_unchecked_from_unestablished(self, config_file, tmp_path):
        """"We looked and found nothing" must not read the same as "nobody looked"."""
        result = self._run(config_file, tmp_path / "runs")
        assert "no source" in result.stdout
        assert "Connector coverage" in result.stdout

    def test_verified_citations_are_shown(self, config_file, tmp_path):
        result = self._run(config_file, tmp_path / "runs")
        assert "verified verbatim" in result.stdout
        assert "10 ILCS 5/10-2" in result.stdout

    def test_json_output_carries_the_coverage_note(self, config_file, tmp_path):
        """The caveat must survive --json and survive being read from the ledger."""
        result = self._run(config_file, tmp_path / "runs", "--json")
        payload = json.loads(result.stdout)
        assert "SOURCE COVERAGE" in payload["result"]["notes"][0]

    def test_citations_carry_the_connector_tier_and_real_hash(self, config_file, tmp_path):
        """Tier comes from the connector; the hash is of the retrieved text."""
        result = self._run(config_file, tmp_path / "runs", "--json")
        payload = json.loads(result.stdout)
        cited = [c for c in payload["result"]["claims"] if c["citations"]]
        assert cited, "expected at least one adjudicated claim with a citation"
        source_hashes = {s["content_hash"] for s in payload["sources"]}
        for claim in cited:
            for citation in claim["citations"]:
                assert citation["tier"] == "T0"
                assert citation["content_hash"] in source_hashes

    def test_fabricated_quote_is_rejected_and_verdict_downgraded(self, config_file, tmp_path):
        """The mock deliberately invents a quote for one claim.

        It must not survive into a citation, and it must not leave a verdict
        standing without support.
        """
        result = self._run(config_file, tmp_path / "runs", "--json")
        payload = json.loads(result.stdout)
        for claim in payload["result"]["claims"]:
            for citation in claim["citations"]:
                assert "impose no burden" not in citation["quote"]

    def test_claim_types_are_assigned(self, config_file, tmp_path):
        result = self._run(config_file, tmp_path / "runs", "--json")
        types = json.loads(result.stdout)["result"]["claims"]
        assert {c["claim_type"] for c in types} >= {"LEGAL", "NORMATIVE", "ATTRIBUTIVE"}

    def test_gated_sentence_becomes_out_of_scope_not_a_deletion(self, config_file, tmp_path):
        result = self._run(config_file, tmp_path / "runs", "--json")
        claims = json.loads(result.stdout)["result"]["claims"]
        excluded = [c for c in claims if c["verdict"] == "OUT_OF_SCOPE"]
        assert len(excluded) == 1
        assert "dog" in excluded[0]["text"]

    def test_spans_index_the_normalized_document(self, config_file, tmp_path):
        """A span a reader cannot slice is not evidence of anything."""
        result = self._run(config_file, tmp_path / "runs", "--json")
        claims = json.loads(result.stdout)["result"]["claims"]
        for claim in claims:
            if claim["span_start"] is not None:
                assert 0 <= claim["span_start"] < claim["span_end"] <= len(self.STATEMENT)

    def test_run_is_marked_reproducible(self, config_file, tmp_path):
        result = self._run(config_file, tmp_path / "runs", "--json")
        assert json.loads(result.stdout)["reproducible"] is True

    def test_no_save_writes_nothing(self, config_file, tmp_path):
        ledger_root = tmp_path / "runs"
        assert self._run(config_file, ledger_root, "--no-save").exit_code == 0
        assert not ledger_root.exists() or not list(ledger_root.glob("*.json"))

    def test_seed_override_changes_the_recipe(self, config_file, tmp_path):
        a = json.loads(self._run(config_file, tmp_path / "a", "--json", "--seed", "1").stdout)
        b = json.loads(self._run(config_file, tmp_path / "b", "--json", "--seed", "2").stdout)
        assert a["input_digest"] != b["input_digest"]

    def test_missing_input_exits_2(self, config_file, tmp_path):
        result = _invoke(["analyze", "--config", str(config_file)])
        assert result.exit_code == 2
        assert "no input" in result.stdout + result.stderr

    def test_text_and_stdin_are_mutually_exclusive(self, config_file):
        result = _invoke(["analyze", "-t", "x", "--stdin", "--config", str(config_file)])
        assert result.exit_code == 2

    def test_unreachable_backend_fails_before_any_work(self, tmp_path):
        """One second to fail beats discovering it after segmenting 400 sentences."""
        config = tmp_path / "config.toml"
        config.write_text(
            '[models.classifier]\nprovider="ollama"\nmodel="m"\n'
            'base_url="http://127.0.0.1:1"\n'
        )
        result = _invoke(["analyze", "-t", "A claim.", "--config", str(config)])
        assert result.exit_code == 9

    def test_missing_config_exits_8(self, tmp_path):
        result = _invoke(["analyze", "-t", "x", "--config", str(tmp_path / "absent.toml")])
        assert result.exit_code == 8


class TestRedTeamCLI(_AnalyzeHarness):
    """The mandatory pass, and what it takes to skip it."""

    def test_findings_are_shown_in_the_report(self, config_file, tmp_path):
        result = self._run_analyze(config_file, tmp_path / "runs")
        assert "red team" in result.stdout
        assert "MATERIAL" in result.stdout
        assert "steelman" in result.stdout

    def test_downgrade_is_visible(self, config_file, tmp_path):
        result = self._run_analyze(config_file, tmp_path / "runs")
        assert "→" in result.stdout or "->" in result.stdout

    def test_non_independence_is_surfaced(self, config_file, tmp_path):
        """The demo config points both roles at one model, which is weaker."""
        result = self._run_analyze(config_file, tmp_path / "runs")
        assert "NOT independent" in result.stdout

    def test_skipping_requires_an_acknowledgment(self, config_file, tmp_path):
        """Contract s7 makes it mandatory; a single flag should not switch it off."""
        result = self._run_analyze(config_file, tmp_path / "runs", "--no-red-team")
        assert result.exit_code == 2
        assert "--i-know" in result.stdout + result.stderr

    def test_skipping_with_acknowledgment_works_and_is_recorded(self, config_file, tmp_path):
        result = self._run_analyze(
            config_file, tmp_path / "runs", "--no-red-team", "--i-know", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["config"]["red_team"] is False
        assert any("RED TEAM SKIPPED" in note for note in payload["result"]["notes"])

    def test_findings_survive_json(self, config_file, tmp_path):
        """Objections ship inside the claim, not in a log."""
        result = self._run_analyze(config_file, tmp_path / "runs", "--json")
        claims = json.loads(result.stdout)["result"]["claims"]
        attacked = [c for c in claims if c.get("red_team")]
        assert attacked
        finding = attacked[0]["red_team"]
        assert finding["steelman"]
        assert "independent" in finding
        assert finding["severity"] in {"NONE", "NOTED", "MINOR", "MATERIAL"}

    def test_counter_citations_carry_a_real_tier(self, config_file, tmp_path):
        result = self._run_analyze(config_file, tmp_path / "runs", "--json")
        claims = json.loads(result.stdout)["result"]["claims"]
        for claim in claims:
            for citation in (claim.get("red_team") or {}).get("counter_citations", []):
                assert citation["tier"] == "T0"


class TestHelpIsPlainText:
    """CI's first green install produced five red tests, two of them here.

    Typer forces a rich terminal whenever GITHUB_ACTIONS is set, so on a runner
    every option name in `--help` arrives wrapped in ANSI escape codes and a test
    asserting on the plain text fails -- while passing on every developer
    machine. tests/conftest.py disables that at import time; this pins it.
    """

    def test_help_output_contains_no_escape_codes(self):
        result = runner.invoke(app, ["analyze", "--help"])
        assert result.exit_code == 0
        assert "\x1b[" not in result.output, "help is being rendered with ANSI styling"
        assert "--file" in result.output

    def test_the_guard_is_set_before_the_cli_is_imported(self):
        import os

        import typer.rich_utils as rich_utils

        assert os.environ.get("_TYPER_FORCE_DISABLE_TERMINAL") == "1"
        assert rich_utils.FORCE_TERMINAL is False

"""Shared fixtures.

Everything here is deterministic: fixed run IDs, fixed timestamps, fixed
digests. Tests that assert on hashes must be reproducible, or they are
testing the clock.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Make CLI output deterministic BEFORE typer is imported anywhere.
# ---------------------------------------------------------------------------
# Typer decides at import time whether to render help through a forced rich
# terminal, and it forces one whenever GITHUB_ACTIONS is set -- so on a CI
# runner every option name arrives wrapped in ANSI escape codes and a test
# asserting `"--file" in result.output` fails while passing on every laptop.
# The two knobs below are the ones typer and rich actually read. Set here, in
# the root conftest, so no test module can import the CLI first and lock in
# the wrong answer. A test that WANTS colour can construct its own Console.
os.environ.setdefault("_TYPER_FORCE_DISABLE_TERMINAL", "1")
os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("TERM", "dumb")

from datetime import UTC, datetime

import pytest

from abca.ledger.recorder import RunRecorder
from abca.schema.core import AnalysisResult, Citation, Claim, DocumentRef
from abca.schema.enums import ClaimType, InputKind, Profile, SourceTier, StageName, Verdict
from abca.schema.ledger import EnvironmentInfo, ModelIdentity, RunConfig, SourceSnapshot

FIXED_TIME = datetime(2026, 9, 3, 12, 0, 0, tzinfo=UTC)
FIXED_RUN_ID = "01M1MSX6D1ND36Q6ZPYC5CYWWP"


def digest(seed: str) -> str:
    """Deterministic well-formed digest for fixtures. Not a real hash."""
    return "sha256:" + (seed * 64)[:64]


@pytest.fixture
def environment() -> EnvironmentInfo:
    """Pinned environment so records are byte-stable across machines."""
    return EnvironmentInfo(
        python_version="3.11.15",
        platform="test-platform",
        packages={"pydantic": "2.13.3"},
    )


@pytest.fixture
def document() -> DocumentRef:
    return DocumentRef(
        kind=InputKind.TEXT,
        locator="<inline>",
        content_hash=digest("a"),
        retrieved_at=FIXED_TIME,
        byte_length=64,
    )


@pytest.fixture
def local_models() -> list[ModelIdentity]:
    """Fully pinnable local models -> reproducible=True."""
    return [
        ModelIdentity(role="classifier", provider="ollama", name="qwen2.5:7b",
                      weights_hash=digest("b"), quantization="Q4_K_M"),
        ModelIdentity(role="adjudicator", provider="ollama", name="qwen2.5:32b",
                      weights_hash=digest("c"), quantization="Q5_K_M"),
        ModelIdentity(role="backtranslate", provider="ollama", name="llama3.1:8b",
                      weights_hash=digest("d"), quantization="Q5_K_M"),
    ]


@pytest.fixture
def config(local_models: list[ModelIdentity]) -> RunConfig:
    return RunConfig(
        profile=Profile.STANDARD,
        seed=42,
        temperature=0.0,
        max_claims=200,
        reading_level=8,
        models=local_models,
    )


@pytest.fixture
def citation() -> Citation:
    return Citation(
        tier=SourceTier.T0,
        title="10 ILCS 5/10-2",
        url="https://ilga.gov/ilcs/10-2",
        quote="signed by 1% of the number of voters ... or 25,000 qualified voters, whichever is less",
        retrieved_at=FIXED_TIME,
        content_hash=digest("e"),
    )


@pytest.fixture
def result(document: DocumentRef, citation: Citation) -> AnalysisResult:
    """A small but structurally complete analysis result.

    Includes one verdict-eligible claim and one NORMATIVE claim, so the
    type gate is exercised by every test that touches a result.
    """
    return AnalysisResult(
        document=document,
        claims=[
            Claim(
                id="c-001",
                text="The statute requires a 25,000-signature petition statewide.",
                claim_type=ClaimType.LEGAL,
                verdict=Verdict.MIXED,
                confidence=0.86,
                evidence_quality=SourceTier.T0,
                reasoning="The statute sets 1% or 25,000, whichever is less.",
                citations=[citation],
            ),
            Claim(
                id="c-002",
                text="That threshold is unfair to new parties.",
                claim_type=ClaimType.NORMATIVE,
                verdict=Verdict.UNVERIFIABLE,
                confidence=0.0,
                evidence_quality=None,
            ),
        ],
    )


@pytest.fixture
def source_snapshot() -> SourceSnapshot:
    return SourceSnapshot(
        url="https://ilga.gov/ilcs/10-2",
        content_hash=digest("e"),
        retrieved_at=FIXED_TIME,
        connector="ilcs",
    )


@pytest.fixture
def recorder(document, config, environment) -> RunRecorder:
    return RunRecorder(
        document=document,
        config=config,
        run_id=FIXED_RUN_ID,
        created_at=FIXED_TIME,
        environment=environment,
    )


@pytest.fixture
def record(recorder: RunRecorder, result: AnalysisResult, source_snapshot: SourceSnapshot):
    """A complete, finalized, audit-clean run record with a three-stage chain."""
    recorder.record_source(source_snapshot)
    for stage_name in (StageName.INGEST, StageName.SEGMENT, StageName.ADJUDICATE):
        with recorder.stage(stage_name) as stage:
            stage.set_input({"stage": stage_name.value})
            stage.set_output({"ok": True})
    return recorder.finalize(result)

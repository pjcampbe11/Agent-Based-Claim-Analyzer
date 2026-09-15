"""Exercise the provider layer end to end, with no model installed.

Runs the full structured-output path -- constrained decoding, extraction,
schema validation, repair, attempt accounting -- against a scripted fake
backend. Everything except the model itself is the real code.

Usage::

    python scripts/demo_step2.py

Against a REAL backend instead, once you have one configured::

    abca config init
    abca models check
    abca models show adjudicator
"""

from __future__ import annotations

from abca.providers.base import GenerationRequest
from abca.providers.grammar import gbnf_for_model
from abca.providers.ollama import OllamaProvider
from abca.providers.structured import StructuredOutputError, generate_structured
from abca.providers.transport import FakeTransport
from abca.schema.core import Claim

DIGEST = "3f" * 32

# A model that first invents a verdict outside the closed vocabulary, then
# corrects itself when told exactly what was wrong. This is the repair loop's
# whole reason to exist.
BAD = (
    '{"id":"c-001","text":"Illinois requires 25,000 signatures.",'
    '"claim_type":"LEGAL","verdict":"TRUE","confidence":0.95}'
)
GOOD = (
    '{"id":"c-001","text":"Illinois requires 25,000 signatures.",'
    '"claim_type":"LEGAL","verdict":"MIXED","confidence":0.91,'
    '"evidence_quality":"T0","reasoning":"The statute sets the lesser of 1% or 25,000.",'
    '"citations":[{"tier":"T0","title":"10 ILCS 5/10-2",'
    '"url":"https://ilga.gov/ilcs/10-2","quote":"or 25,000 qualified voters, '
    'whichever is less","retrieved_at":"2026-09-04T00:00:00Z",'
    f'"content_hash":"sha256:{"a" * 64}"}}]}}'
)

# A model that never gets it right, to show the failure path.
NEVER = '{"id":"c-002","text":"x","claim_type":"LEGAL","verdict":"SUPPORTED","confidence":0.99}'


def fake_ollama(responses: list[str]) -> FakeTransport:
    return FakeTransport({
        "/api/version": {"version": "0.6.2"},
        "/api/tags": {"models": [{
            "name": "qwen2.5:32b-instruct", "digest": DIGEST,
            "details": {"quantization_level": "Q5_K_M", "parameter_size": "32B"},
        }]},
        "/api/show": {"details": {"quantization_level": "Q5_K_M"},
                      "model_info": {"qwen2.context_length": 32768}},
        "/api/generate": [
            {"response": text, "prompt_eval_count": 820, "eval_count": 140,
             "total_duration": 3_100_000_000, "done_reason": "stop"}
            for text in responses
        ],
    })


def rule(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * (68 - len(title))}")


def main() -> int:
    rule("1. Weight pinning")
    provider = OllamaProvider("qwen2.5:32b-instruct", transport=fake_ollama([GOOD]))
    provider.health()
    identity = provider.identity()
    print(f"  model          : {identity.name}")
    print(f"  weights hash   : {identity.weights_hash}")
    print(f"  quantization   : {identity.quantization}")
    print(f"  context length : {identity.context_length}")
    print(f"  pinnable       : {identity.is_pinnable}  -> runs recorded reproducible: true")
    print("  (a hosted API returns weights_hash=None here, and runs are marked false)")

    rule("2. Constrained decoding")
    grammar = gbnf_for_model(Claim)
    print(f"  GBNF for Claim : {len(grammar.splitlines())} rules, {len(grammar)} bytes")
    print(f"  entry point    : {grammar.splitlines()[0]}")
    print("  the eight-member verdict enum becomes eight terminals, so a ninth")
    print("  verdict is not merely rejected -- it cannot be sampled at all")

    rule("3. Structured output, first try")
    provider = OllamaProvider("qwen2.5:32b-instruct", transport=fake_ollama([GOOD]))
    provider.health()
    result = generate_structured(provider, GenerationRequest(prompt="Analyze."), Claim)
    print(f"  verdict        : {result.value.verdict.value}")
    print(f"  evidence       : {result.value.evidence_quality.value} "
          f"({len(result.value.citations)} citation)")
    print(f"  attempts       : {result.attempt_count}")
    print(f"  stage notes    : {result.stage_notes() or '(none -- clean run adds no noise)'}")

    rule("4. Repair loop: invented verdict, then corrected")
    provider = OllamaProvider("qwen2.5:32b-instruct", transport=fake_ollama([BAD, GOOD]))
    provider.health()
    result = generate_structured(provider, GenerationRequest(prompt="Analyze."), Claim)
    for attempt in result.attempts:
        status = attempt.failure.value if attempt.failure else "ok"
        print(f"  attempt {attempt.number}      : {status:<8} {attempt.error_summary[:52]}")
    print(f"  final verdict  : {result.value.verdict.value}")
    print(f"  tokens spent   : {result.total_tokens} (failed attempts included -- they cost real money)")
    for note in result.stage_notes():
        print(f"  ledger note    : {note}")

    rule("5. A gate a grammar cannot enforce")
    print("  This payload is grammatically perfect:")
    print(f"    {NEVER}")
    print("  ...and semantically forbidden: SUPPORTED with no T0-T2 citation.")
    print("  No grammar can express that rule, which is why validate-and-retry")
    print("  runs even when constrained decoding is available.")
    provider = OllamaProvider("qwen2.5:32b-instruct", transport=fake_ollama([NEVER] * 3))
    provider.health()
    try:
        generate_structured(provider, GenerationRequest(prompt="Analyze."), Claim)
    except StructuredOutputError as exc:
        print(f"\n  raised after {len(exc.attempts)} attempts rather than accepting it:")
        print(f"    {exc.attempts[-1].error_summary[:88]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

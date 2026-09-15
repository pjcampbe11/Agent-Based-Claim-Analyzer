"""A mock Ollama server, for exercising the real CLI without a GPU.

Speaks enough of the Ollama HTTP API for abCA to run against it end to end:
``/api/version``, ``/api/tags``, ``/api/show``, ``/api/generate``.

Responses are canned per stage. The server inspects the incoming prompt to work
out which stage is asking -- the prompts are distinctive enough that a substring
match is reliable, and it keeps the script free of ordering assumptions that
would break the moment a stage is reordered.

This is a test double, not a simulation. It does not run a model; it returns
fixed, plausible output so the plumbing can be verified: CLI parsing, config
loading, provider construction, transport, structured-output validation, stage
recording, ledger writing, and the report. Everything except the model itself
is the real code path.

Usage::

    python scripts/mock_ollama.py --port 11500          # run until interrupted
    python scripts/demo_step3.py                        # starts one automatically
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen2.5:7b-instruct"
DIGEST = "7c" * 32

#: A SECOND model, with a different digest. The fidelity gate refuses to run
#: when pass C would grade the model that wrote pass B, and independence is
#: compared by weights digest rather than by name -- so a mock that advertised
#: one model could not exercise the gate at all.
MODEL_B = "llama3.1:8b-instruct"
DIGEST_B = "9e" * 32

MODELS = {
    MODEL: {"digest": DIGEST, "quantization_level": "Q4_K_M",
            "parameter_size": "7.6B", "family": "qwen2"},
    MODEL_B: {"digest": DIGEST_B, "quantization_level": "Q5_K_M",
              "parameter_size": "8.0B", "family": "llama"},
}

# ---------------------------------------------------------------------------
# Canned stage responses
# ---------------------------------------------------------------------------

GATE = {
    "decisions": [
        {"sentence_index": 0, "in_scope": True,
         "reason": "asserts a statutory signature requirement for ballot access"},
        {"sentence_index": 1, "in_scope": True,
         "reason": "asserts a judgment about the fairness of the electoral system"},
        {"sentence_index": 2, "in_scope": True,
         "reason": "asserts what a named official said in an official capacity"},
        {"sentence_index": 3, "in_scope": False,
         "reason": "personal remark with no public-affairs content"},
    ]
}

SEGMENT = {
    "claims": [
        {"text": "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to start a new political party.",
         "sentence_index": 0,
         "verbatim_span": "Illinois makes you get 25,000 signatures to start a new party",
         "ambiguous_stance": False},
        {"text": ("Under 10 ILCS 5/10-2 the signature count is the only real obstacle "
                  "to starting a new party in Illinois."),
         "sentence_index": 0, "verbatim_span": None, "ambiguous_stance": False},
        {"text": "The Illinois ballot-access system is rigged against outsiders.",
         "sentence_index": 1,
         "verbatim_span": "The whole thing is rigged against outsiders",
         "ambiguous_stance": False},
        {"text": "The Secretary of State said the process is straightforward.",
         "sentence_index": 2,
         "verbatim_span": "the Secretary of State said the process is straightforward",
         "ambiguous_stance": False},
    ]
}

CLASSIFY = {
    "classifications": [
        {"claim_id": "c-001", "claim_type": "LEGAL", "confidence": 0.94,
         "reasoning": "settled by reading 10 ILCS 5/10-2"},
        {"claim_id": "c-002", "claim_type": "LEGAL", "confidence": 0.68,
         "reasoning": "settled by reading the statute for any further requirements"},
        {"claim_id": "c-003", "claim_type": "NORMATIVE", "confidence": 0.91,
         "reasoning": "two people agreeing on every fact could still disagree about 'rigged'"},
        {"claim_id": "c-004", "claim_type": "ATTRIBUTIVE", "confidence": 0.89,
         "reasoning": "settled by checking the record of what the official said"},
    ]
}

#: Adjudications. Note c-001's and c-002's quotes are REAL text from
#: 10 ILCS 5/10-2 and verify; c-004's is deliberately fabricated, so the demo
#: shows verification catching an invented quote and downgrading the verdict.
ADJUDICATE = {
    "adjudications": [
        {"claim_id": "c-001", "verdict": "MIXED", "confidence": 0.88,
         "reasoning": (
             "The statute sets the lesser of 1% of the preceding statewide general "
             "election vote or 25,000 signatures. The figure is right, but stating "
             "it as a flat requirement misdescribes the rule, which is a formula."
         ),
         "citations": [{
             "source_id": "s-001",
             "quote": ("signed by 1% of the number of voters who voted at the next "
                       "preceding Statewide general election or 25,000 qualified "
                       "voters, whichever is less"),
             "locator": "10 ILCS 5/10-2",
         }]},
        {"claim_id": "c-002", "verdict": "CONTRADICTED", "confidence": 0.86,
         "reasoning": (
             "The same section additionally requires the petition to contain a "
             "complete list of candidates for all offices to be filled, so the "
             "signature count is not the only requirement."
         ),
         "citations": [{
             "source_id": "s-001",
             "quote": ("shall at the time of filing contain a complete list of "
                       "candidates of such party for all offices to be filled"),
             "locator": "10 ILCS 5/10-2",
         }]},
        {"claim_id": "c-004", "verdict": "SUPPORTED", "confidence": 0.93,
         "reasoning": (
             "The statute states plainly that the process is straightforward and "
             "imposes no meaningful burden on new parties."
         ),
         "citations": [{
             "source_id": "s-001",
             "quote": "the process shall be straightforward and impose no burden",
             "locator": "10 ILCS 5/10-2",
         }]},
    ]
}


#: Red-team assessments. c-001 gets a MATERIAL objection with REAL counter-text
#: from the statute, so the demo shows a verdict being downgraded on verified
#: evidence. c-002 gets NONE, so it also shows the common case: attacked and
#: found sound.
RED_TEAM = {
    "assessments": [
        {"claim_id": "c-001", "severity": "MATERIAL",
         "counter_evidence": (
             "The statute sets the LESSER of 1% of the preceding statewide vote or "
             "25,000, so 25,000 is a ceiling rather than the requirement. The same "
             "section also imposes a full-slate obligation the verdict does not "
             "mention."
         ),
         "steelman": (
             "In every recent cycle 1% of statewide turnout has exceeded 25,000, so "
             "25,000 is the number an organizer actually works to. Calling the "
             "formula a distinction without a difference is defensible."
         ),
         "overreach_flags": [
             "MIXED understates it: the claim states a formula as a fixed number"
         ],
         "counter_citations": [{
             "source_id": "s-001",
             "quote": ("shall at the time of filing contain a complete list of "
                       "candidates of such party for all offices to be filled"),
             "locator": "10 ILCS 5/10-2",
         }],
         "recommended_verdict": "UNSUPPORTED"},
        {"claim_id": "c-002", "severity": "NONE",
         "counter_evidence": (
             "Nothing in the retrieved section cuts against this. The full-slate "
             "requirement is stated plainly and the verdict quotes it directly."
         ),
         "steelman": (
             "A speaker using 'hurdle' loosely to mean 'the thing organizers talk "
             "about' is not making a legal claim at all."
         ),
         "overreach_flags": [],
         "counter_citations": [],
         "recommended_verdict": None},
    ]
}


# ---------------------------------------------------------------------------
# Fidelity gate (contract s5): three passes, three canned responses
# ---------------------------------------------------------------------------
#
# The rendering below is a FAITHFUL one, so the demo shows the gate passing.
# Its back-translation carries all three elements with the same kinds, the same
# modal force and the same numbers -- in different words, which is the whole
# point of a plain-language rewrite and the reason the diff stems tokens rather
# than comparing them exactly.

FIDELITY_EXTRACT = {
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

FIDELITY_RENDER = {
    "rendering": (
        "A group of people who want to start a new political party across the whole "
        "state must file a petition. The petition must be signed by 1% of the "
        "voters who voted in the last statewide general election, or by 25,000 "
        "qualified voters, whichever is less. The petition must also list "
        "the party's candidates. It must name a candidate for every office to be "
        "filled. That list must be there on the day the petition is filed."
    ),
    "footnotes": [],
}

FIDELITY_BACKTRANSLATE = {
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


# ---------------------------------------------------------------------------
# Thread mode: the same stages, scripted for a comment thread
# ---------------------------------------------------------------------------
#
# Selected by a phrase that only appears in the step-7 demo's thread. A mock
# cannot know which document it is being asked about, so the demo puts a
# distinctive sentence in the thread and the mock keys off it. Crude, and
# honest about being crude -- it is a test double, not a simulation.

THREAD_MARKER = "ballot access working group"

THREAD_GATE = {
    "decisions": [
        {"sentence_index": i, "in_scope": True,
         "reason": "asserts a statutory ballot-access requirement"}
        for i in range(8)
    ]
}

_SIGS = "Under 10 ILCS 5/10-2 Illinois requires 25,000 signatures to form a new party."
_SIGS_REWORDED = (
    "Illinois requires 25,000 signatures under 10 ILCS 5/10-2 to start a new "
    "political party."
)
_SLATE = (
    "Under 10 ILCS 5/10-2 a new-party petition must contain a complete list of "
    "candidates for all offices to be filled."
)

THREAD_SEGMENT = {
    "claims": [
        {"text": _SIGS, "sentence_index": 0,
         "verbatim_span": None, "ambiguous_stance": False},
        {"text": _SIGS_REWORDED, "sentence_index": 1,
         "verbatim_span": None, "ambiguous_stance": False},
        {"text": _SLATE, "sentence_index": 2,
         "verbatim_span": None, "ambiguous_stance": False},
        {"text": _SIGS, "sentence_index": 3,
         "verbatim_span": None, "ambiguous_stance": False},
    ]
}

THREAD_CLASSIFY = {
    "classifications": [
        {"claim_id": f"c-{i:03d}", "claim_type": "LEGAL", "confidence": 0.93,
         "reasoning": "settled by reading 10 ILCS 5/10-2"}
        for i in range(1, 5)
    ]
}

THREAD_ADJUDICATE = {
    "adjudications": [
        {"claim_id": "c-001", "verdict": "MIXED", "confidence": 0.84,
         "reasoning": (
             "The statute sets the LESSER of 1% of the preceding statewide vote or "
             "25,000, so 25,000 is a ceiling rather than a flat requirement."
         ),
         "citations": [{
             "source_id": "s-001",
             "quote": ("signed by 1% of the number of voters who voted at the next "
                       "preceding Statewide general election or 25,000 qualified "
                       "voters, whichever is less"),
             "locator": "10 ILCS 5/10-2",
         }]},
        {"claim_id": "c-003", "verdict": "SUPPORTED", "confidence": 0.91,
         "reasoning": "The statute states the full-slate requirement directly.",
         "citations": [{
             "source_id": "s-001",
             "quote": ("shall at the time of filing contain a complete list of "
                       "candidates of such party for all offices to be filled"),
             "locator": "10 ILCS 5/10-2",
         }]},
    ]
}

THREAD_RED_TEAM = {
    "assessments": [
        {"claim_id": "c-001", "severity": "NONE",
         "counter_evidence": "Nothing in the retrieved section cuts against this.",
         "steelman": (
             "In recent cycles 1% of statewide turnout has exceeded 25,000, so 25,000 "
             "is the number an organizer works to."
         ),
         "overreach_flags": [], "counter_citations": [], "recommended_verdict": None},
        {"claim_id": "c-003", "severity": "NONE",
         "counter_evidence": "The requirement is stated plainly and quoted directly.",
         "steelman": "A speaker could mean 'slate' loosely rather than statutorily.",
         "overreach_flags": [], "counter_citations": [], "recommended_verdict": None},
    ]
}


#: Claim texts the thread script emits. Later stages never see the original
#: thread text -- they see the CLAIMS -- so thread mode has to be recognised
#: from those instead of from the marker sentence.
_THREAD_CLAIMS = {_SIGS, _SIGS_REWORDED, _SLATE}

#: Phrases unique to the step-3 demo statement. Everything canned under GATE /
#: SEGMENT / CLASSIFY / ADJUDICATE / RED_TEAM is about THAT statement.
_STEP3_MARKERS = (
    "rigged against outsiders",
    "Illinois makes you get 25,000 signatures",
    "the Secretary of State said the process is straightforward",
)


def _is_step3_document(prompt: str) -> bool:
    return any(marker in prompt for marker in _STEP3_MARKERS)


def _mentions_step3_claims(prompt: str) -> bool:
    """Whether a later stage is being asked about the step-3 claims."""
    return any(
        claim["text"] in prompt for claim in SEGMENT["claims"]
    ) or "The Illinois ballot-access system is rigged" in prompt


def _neutral_gate(prompt: str) -> dict:
    """In-scope for however many sentences were numbered, and nothing invented.

    The gate is the one stage where answering an unknown document is safe: it
    says only "this sentence concerns public affairs", which for an unknown
    statement about public affairs is true and carries no content of its own.
    """
    import re as _re

    indices = [int(n) for n in _re.findall(r"^\[(\d+)\]", prompt, _re.MULTILINE)]
    return {
        "decisions": [
            {"sentence_index": index, "in_scope": True,
             "reason": "asserts a public-affairs position"}
            for index in (indices or range(8))
        ]
    }


def _pick_response(prompt: str) -> dict:
    """Route by a distinctive phrase from each stage prompt."""
    thread_mode = THREAD_MARKER in prompt
    if "public affairs" in prompt and "IN SCOPE" in prompt:
        if thread_mode:
            return THREAD_GATE
        return GATE if _is_step3_document(prompt) else _neutral_gate(prompt)
    # Later stages never see the thread text -- they see the CLAIMS, rendered
    # into a numbered block -- so thread mode has to be recognised from those
    # too, by substring rather than by whole line.
    if thread_mode or any(claim in prompt for claim in _THREAD_CLAIMS):
        if "atomic claims" in prompt or "atomic assertion" in prompt:
            return THREAD_SEGMENT
        if "six types" in prompt or "what would settle this" in prompt:
            return THREAD_CLASSIFY
        if "attack the analysis" in prompt or "Analysis to attack" in prompt:
            return THREAD_RED_TEAM
        if "copy, do not compose" in prompt or "verified verbatim" in prompt:
            return THREAD_ADJUDICATE
    if not _is_step3_document(prompt) and not _mentions_step3_claims(prompt):
        # An unrecognised document. Answer with NOTHING rather than with another
        # document's canned response: a double that invents claims the run never
        # contained turns every demo built on it into a lie.
        if "atomic claims" in prompt or "atomic assertion" in prompt:
            return {"claims": []}
        if "six types" in prompt or "what would settle this" in prompt:
            return {"classifications": []}
        if "attack the analysis" in prompt or "Analysis to attack" in prompt:
            return {"assessments": []}
        if "copy, do not compose" in prompt or "verified verbatim" in prompt:
            return {"adjudications": []}
    if "atomic claims" in prompt or "atomic assertion" in prompt:
        return SEGMENT
    if "six types" in prompt or "what would settle this" in prompt:
        return CLASSIFY
    if "attack the analysis" in prompt or "Analysis to attack" in prompt:
        return RED_TEAM
    if "copy, do not compose" in prompt or "verified verbatim" in prompt:
        return ADJUDICATE
    # The fidelity passes share a lot of vocabulary -- all three talk about
    # "operative elements" -- so each is routed on a phrase unique to its file.
    if "You have not seen the original provision" in prompt:
        return FIDELITY_BACKTRANSLATE
    if "readability is the goal, fidelity is the constraint" in prompt:
        return FIDELITY_RENDER
    if "taking the provision apart" in prompt:
        return FIDELITY_EXTRACT
    return {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            # The client went away mid-response (an interrupted run, a timeout).
            # Nothing useful to do, and letting it propagate spews a traceback
            # over the demo output.
            pass

    def do_GET(self):
        if self.path == "/api/version":
            self._send({"version": "0.6.2"})
        elif self.path == "/api/tags":
            self._send({"models": [
                {"name": name, "model": name, "digest": info["digest"],
                 "size": 4_700_000_000,
                 "details": {"quantization_level": info["quantization_level"],
                             "parameter_size": info["parameter_size"],
                             "family": info["family"]}}
                for name, info in MODELS.items()
            ]})
        else:
            self._send({"error": "not found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/show":
            info = MODELS.get(payload.get("model") or MODEL, MODELS[MODEL])
            self._send({
                "details": {"quantization_level": info["quantization_level"],
                            "family": info["family"]},
                "model_info": {f"{info['family']}.context_length": 32768},
            })
            return

        if self.path == "/api/generate":
            response = _pick_response(payload.get("prompt", ""))
            # stage_calls is appended from multiple handler threads; list.append
            # is atomic under the GIL, so no lock is needed for this use.
            self.server.stage_calls.append(  # type: ignore[attr-defined]
                "gate" if response is GATE else
                "segment" if response is SEGMENT else
                "classify" if response is CLASSIFY else
                "adjudicate" if response is ADJUDICATE else
                "red_team" if response is RED_TEAM else
                "fidelity.extract" if response is FIDELITY_EXTRACT else
                "fidelity.render" if response is FIDELITY_RENDER else
                "fidelity.backtranslate" if response is FIDELITY_BACKTRANSLATE else
                "thread.gate" if response is THREAD_GATE else
                "thread.segment" if response is THREAD_SEGMENT else
                "thread.classify" if response is THREAD_CLASSIFY else
                "thread.adjudicate" if response is THREAD_ADJUDICATE else
                "thread.red_team" if response is THREAD_RED_TEAM else
                "unknown"
            )
            self._send({
                "model": payload.get("model") or MODEL,
                "response": json.dumps(response),
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 640 + len(payload.get("prompt", "")) // 40,
                "eval_count": 120,
                "total_duration": 1_850_000_000,
            })
            return

        self._send({"error": "not found"}, status=404)


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    """Start the mock in a daemon thread. Returns the server and its port.

    THREADING IS REQUIRED, NOT AN OPTIMISATION
    ------------------------------------------
    abCA builds one provider per role, and each provider holds its own
    keep-alive connection. A run using both ``classifier`` and ``segmenter``
    therefore opens TWO connections to the same backend.

    A single-threaded ``HTTPServer`` handles one connection at a time and,
    under HTTP/1.1 keep-alive, stays inside that connection's request loop
    until the client closes it. The second connection is never accepted, and
    the run deadlocks -- which is exactly what happened the first time this
    demo was run.

    Real Ollama and llama-server are both concurrent, so this is the mock
    matching production behavior rather than papering over a client bug.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.stage_calls = []  # type: ignore[attr-defined]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=11500)
    args = parser.parse_args()
    httpd, port = serve(args.port)
    print(f"mock Ollama listening on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""End-to-end demo: the real `abca analyze` against a mock Ollama.

Starts :mod:`scripts.mock_ollama` on a loopback port, writes a config pointing
at it, and runs the actual CLI as a subprocess. Nothing is stubbed except the
model's output -- CLI parsing, config loading, provider construction, HTTP
transport, structured-output validation, stage recording, ledger writing and
report rendering are all the shipping code paths.

Then it verifies the ledger the run produced, and tampers with it to show the
audit catching the edit.

Usage::

    python scripts/demo_step3.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mock_ollama import MODEL, serve

STATEMENT = (
    "Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures to start a new "
    "party, and that's the only real hurdle. The whole thing is rigged against "
    "outsiders. Last week the Secretary of State said the process is "
    "straightforward. Anyway, my dog is asleep on the couch."
)


def rule(title: str) -> None:
    print(f"\n{'=' * 4} {title} {'=' * max(0, 68 - len(title))}")


def run_cli(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src"),
           "COLUMNS": "150", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "abca.cli.main", *args],
        capture_output=True, text=True, env=env,
    )


def main() -> int:
    httpd, port = serve()
    workspace = Path(tempfile.mkdtemp(prefix="abca-demo3-"))
    config_path = workspace / "config.toml"
    ledger_root = workspace / "runs"

    config_path.write_text(
        "[defaults]\n"
        'profile = "standard"\nseed = 42\ntemperature = 0.0\nmax_claims = 200\n\n'
        "[models.classifier]\n"
        'provider = "ollama"\n'
        f'model = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        "[models.adjudicator]\n"
        'provider = "ollama"\n'
        f'model = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n\n'
        "[models.redteam]\n"
        'provider = "ollama"\n'
        f'model = "{MODEL}"\n'
        f'base_url = "http://127.0.0.1:{port}"\n',
        encoding="utf-8",
    )

    rule("1. Backend check (real CLI, real HTTP)")
    check = run_cli(["models", "check", "--config", str(config_path), "--json"])
    payload = json.loads(check.stdout)
    role = payload["roles"]["classifier"]
    print(f"  status       : {role['status']}")
    print(f"  model        : {role['model']}")
    print(f"  weights hash : {role['weights_hash']}")
    print(f"  pinned       : {role['pinned']}  -> runs recorded reproducible: true")

    rule("2. abca analyze -t")
    analyze = run_cli([
        "analyze", "-t", STATEMENT,
        "--config", str(config_path),
        "--ledger-root", str(ledger_root),
    ])
    print(analyze.stdout.rstrip())
    if analyze.returncode != 0:
        print("STDERR:", analyze.stderr)
        return 1

    print(f"\n  stages the mock served, in order: {httpd.stage_calls}")

    rule("3. The run record it wrote")
    run_id = next(ledger_root.glob("*.json")).stem
    show = run_cli(["ledger", "show", run_id, "--stages", "--ledger-root", str(ledger_root)])
    print(show.stdout.rstrip())

    rule("4. Integrity audit")
    audit = run_cli(["ledger", "audit", "--ledger-root", str(ledger_root)])
    print(audit.stdout.rstrip(), f"(exit {audit.returncode})")

    record_path = ledger_root / f"{run_id}.json"
    pristine = record_path.read_text()

    rule("5a. Forgery attempt 1 - flip a verdict to SUPPORTED")
    print("  The obvious edit a bad actor would make. It never reaches the hash")
    print("  check: the CONTRACT GATE rejects it first, because SUPPORTED requires")
    print("  a T0-T2 citation and this claim has none.\n")
    record = json.loads(pristine)
    target = next(c for c in record["result"]["claims"] if c["verdict"] == "UNVERIFIABLE")
    print(f"  flipping {target['id']}: UNVERIFIABLE -> SUPPORTED")
    target["verdict"] = "SUPPORTED"
    record_path.write_text(json.dumps(record, indent=2))
    audit2 = run_cli(["ledger", "audit", "--ledger-root", str(ledger_root)])
    print(audit2.stdout.rstrip(), f"(exit {audit2.returncode})")

    rule("5b. Forgery attempt 2 - a schema-legal edit")
    print("  A smarter forger picks a change the schema permits. A NORMATIVE claim")
    print("  may be UNVERIFIABLE or OUT_OF_SCOPE, so swapping them passes every")
    print("  validator -- and still fails, because the digests no longer match.\n")
    record = json.loads(pristine)
    target = next(c for c in record["result"]["claims"] if c["verdict"] == "UNVERIFIABLE")
    print(f"  flipping {target['id']}: UNVERIFIABLE -> OUT_OF_SCOPE")
    target["verdict"] = "OUT_OF_SCOPE"
    record_path.write_text(json.dumps(record, indent=2))
    audit3 = run_cli(["ledger", "audit", "--ledger-root", str(ledger_root)])
    print(audit3.stdout.rstrip(), f"(exit {audit3.returncode})")

    record_path.write_text(pristine)
    print("\n  (record restored)")

    httpd.shutdown()
    print(f"\nworkspace: {workspace}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

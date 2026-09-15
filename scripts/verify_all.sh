#!/usr/bin/env bash
# Everything this repository claims, checked in one command.
#
# The README quotes this script's output. That is deliberate: a badge is a
# picture of a claim, and this is the claim itself, reproducible by anyone who
# clones the repo. If a line below prints FAILED, the README is wrong and
# should be treated as wrong.
#
#   ./scripts/verify_all.sh
#
# Needs no GPU, no API key and no model: every demo runs against the mock
# server in scripts/mock_ollama.py. The `network` block is the exception --
# those tests hit real government servers and are skipped by default.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="${PWD}/src:${PYTHONPATH:-}"

echo "=== suite / network / lint / coverage ==="
python -m pytest -q 2>&1 | tail -1
python -m pytest -q -m network 2>&1 | tail -1
python -m ruff check src tests scripts tools --output-format concise 2>&1 | tail -1
python -m pytest -q --cov=abca --cov-report=term 2>&1 | grep TOTAL

echo
echo "=== identity, corpus and eval-set self-checks ==="
printf "  marks match their generator : "
python scripts/build_identity.py --check >/dev/null 2>&1 && echo OK || echo FAILED
printf "  brand guide contrast claims : "
python scripts/check_contrast.py >/dev/null 2>&1 && echo OK || echo FAILED
printf "  institutional corpus        : "
python scripts/check_context_pack.py >/dev/null 2>&1 && echo OK || echo FAILED
printf "  symmetry pairs (structural) : "
python scripts/check_symmetry.py >/dev/null 2>&1 && echo OK || echo FAILED
printf "  symmetry, lanes actually run: "
python scripts/check_symmetry.py --run >/dev/null 2>&1 && echo OK || echo FAILED
printf "  levelset corpus symmetry    : "
python scripts/check_levelset_symmetry.py --run >/dev/null 2>&1 && echo OK || echo FAILED
printf "  README contents list current: "
python scripts/build_readme_toc.py --check >/dev/null 2>&1 && echo OK || echo FAILED

echo
echo "=== all demos ==="
for d in 1 2 3 4 5 6 7 9 10 11; do
  printf "  demo_step%s: " "$d"
  if timeout 200 python "scripts/demo_step$d.py" >/dev/null 2>&1; then
    echo "OK"
  else
    echo "FAILED"
  fi
done

echo
echo "=== every command in the CLI ==="
python - <<'PY'
from typer.testing import CliRunner

from abca.cli.main import app

runner = CliRunner()
for command in ["version", "schema", "verify", "ledger", "analyze",
                "models", "config", "sources", "explain", "issue",
                "queue"]:
    result = runner.invoke(app, [command, "--help"])
    print(f"  {command:10} help exit {result.exit_code}")
PY

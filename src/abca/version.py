"""Version constants.

Three *different* versions matter, and conflating them would break the
verification story. They are separated here on purpose.

``TOOL_VERSION``
    The version of this codebase. Bumped on every release, including bug
    fixes that cannot change analysis output. It is RECORDED in every run
    but deliberately EXCLUDED from the input digest, because if it were
    included, a patch release would invalidate every previously published
    run hash. When verification finds a mismatch, the tool version delta is
    reported as context for the human reading the report.

``SCHEMA_VERSION``
    The version of the on-disk record format. Changing the meaning or the
    shape of a persisted field REQUIRES bumping this. It IS part of the
    input digest, because two runs recorded under different schema
    semantics are not comparable.

``PROMPT_CONTRACT_VERSION``
    The version of the Source-of-Truth Prompt contract (docs/01). It IS
    part of the input digest, because changing the adjudication contract
    changes what a verdict means. The prompt *files* are additionally
    hashed individually, so an edit to a prompt that forgets to bump this
    constant is still detected.

All three use semantic versioning.
"""

from __future__ import annotations

# Codebase version. Mirrors pyproject.toml [project].version.
TOOL_VERSION = "0.1.0"

# Persisted-record format version. Bump on ANY change to the meaning or
# layout of a field written to disk.
#
# 1.1.0 -- RedTeamFinding gained `counter_citations`, `severity` and
#          `independent` (build step 5). Additive: every 1.0.0 record still
#          parses. But `schema_version` is part of `input_digest`, so a 1.0.0
#          record cannot be byte-compared against a 1.1.0 replay -- see
#          abca.pipeline.replay.check_schema_compatibility. Integrity
#          verification of old records is unaffected and works forever.
# 1.2.0 -- StageName gained `sourceless` and STAGE_ORDER places it between
#          `cluster` and `retrieve` (docs/18). Additive in the same way 1.1.0
#          was: stage chains are validated as a SUBSEQUENCE of STAGE_ORDER, so
#          every 1.1.0 record -- which simply has no sourceless stage -- remains
#          a valid subsequence and still parses and audits. As with 1.1.0, a
#          1.1.0 record cannot be byte-compared against a 1.2.0 replay, because
#          schema_version is part of input_digest.
SCHEMA_VERSION = "1.2.0"

# Source-of-Truth Prompt contract version (see docs/01-source-of-truth-prompt.md).
PROMPT_CONTRACT_VERSION = "sotp/0.1.0"

__all__ = ["PROMPT_CONTRACT_VERSION", "SCHEMA_VERSION", "TOOL_VERSION"]

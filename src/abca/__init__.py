"""abCA -- the agent-based Claim Analyzer.

A reproducible, cited analysis engine for political claims.

The package is deliberately layered so that the auditability machinery
(:mod:`abca.canonical`, :mod:`abca.schema`, :mod:`abca.ledger`) has no
dependency on the inference machinery. Auditability is built first and
depends on nothing, which is the only way it stays real rather than
decorative.
"""

from abca.version import (
    PROMPT_CONTRACT_VERSION,
    SCHEMA_VERSION,
    TOOL_VERSION,
)

__all__ = ["PROMPT_CONTRACT_VERSION", "SCHEMA_VERSION", "TOOL_VERSION"]
__version__ = TOOL_VERSION

"""Schema layer: the contract, expressed as validated data structures.

Split into three modules on purpose:

* :mod:`abca.schema.enums`  -- closed vocabularies from the prompt contract.
* :mod:`abca.schema.core`   -- what the pipeline produces.
* :mod:`abca.schema.ledger` -- what gets recorded, and how it is hashed.

Nothing in this package imports the inference layer, the provider layer, or
the CLI. The dependency arrow points one way only, which is what lets the
auditability machinery be tested in isolation.
"""

from abca.schema.core import (
    ABCAModel,
    AnalysisResult,
    Citation,
    Claim,
    ClusterRef,
    Digest,
    DocumentRef,
    FidelityReport,
    InfluencePattern,
    RedTeamFinding,
)
from abca.schema.enums import (
    STAGE_ORDER,
    ClaimType,
    InputKind,
    Profile,
    SourceTier,
    StageName,
    Verdict,
    VerifyOutcome,
)
from abca.schema.ledger import (
    EnvironmentInfo,
    ModelIdentity,
    PromptRef,
    RunConfig,
    RunRecord,
    SourceSnapshot,
    StageRecord,
)

__all__ = [
    "STAGE_ORDER",
    "ABCAModel",
    "AnalysisResult",
    "Citation",
    "Claim",
    "ClaimType",
    "ClusterRef",
    "Digest",
    "DocumentRef",
    "EnvironmentInfo",
    "FidelityReport",
    "InfluencePattern",
    "InputKind",
    "ModelIdentity",
    "Profile",
    "PromptRef",
    "RedTeamFinding",
    "RunConfig",
    "RunRecord",
    "SourceSnapshot",
    "SourceTier",
    "StageName",
    "StageRecord",
    "Verdict",
    "VerifyOutcome",
]

"""Analysis pipeline: deterministic orchestration over constrained model calls.

Stage order and responsibilities are declared in
:data:`abca.schema.enums.STAGE_ORDER`; the recorder validates that a run's
stage chain matches it, so the ledger cannot claim a shape the pipeline does
not have.

This package depends on :mod:`abca.providers` and :mod:`abca.schema`. Nothing
in either of those depends on this one, so the auditability machinery stays
testable without any pipeline at all.
"""

from abca.pipeline.ingest import IngestResult, ingest_text, normalize_text
from abca.pipeline.models import DraftClaim
from abca.pipeline.orchestrator import (
    AnalyzeOptions,
    AnalyzeResult,
    analyze_text,
    to_published_claim,
)
from abca.pipeline.sentences import Sentence, split_sentences

__all__ = [
    "AnalyzeOptions", "AnalyzeResult", "DraftClaim", "IngestResult", "Sentence",
    "analyze_text", "ingest_text", "normalize_text", "split_sentences",
    "to_published_claim",
]

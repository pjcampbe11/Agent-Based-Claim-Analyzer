"""Sourceless-claim dissection: trigger detection and the distortion taxonomy."""

from abca.sourceless.distortions import (
    DISTORTION_TAXONOMY,
    TAXONOMY_VERSION,
    Distortion,
    distortion_by_name,
)
from abca.sourceless.trigger import (
    ExternalReference,
    find_external_references,
    is_sourceless,
    should_dissect,
)

__all__ = [
    "DISTORTION_TAXONOMY",
    "TAXONOMY_VERSION",
    "Distortion",
    "ExternalReference",
    "distortion_by_name",
    "find_external_references",
    "is_sourceless",
    "should_dissect",
]

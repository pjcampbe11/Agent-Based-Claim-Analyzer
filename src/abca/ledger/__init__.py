"""Run ledger: recording, storing and verifying analysis runs.

This package has no dependency on the inference layer. It can be exercised
end to end -- record a run, store it, tamper with the file, detect the
tampering, replay and classify the result -- before a single model call
exists. Building it first is what keeps auditability from becoming
decorative.
"""

from abca.ledger.recorder import RunRecorder, StageContext, collect_environment
from abca.ledger.store import LedgerCorrupt, LedgerStore, RunNotFound, default_data_dir
from abca.ledger.verify import (
    LedgerTampered,
    Replayer,
    SourceProbe,
    VerifyReport,
    compare,
    detect_drift,
    diff_recipes,
    diff_verdicts,
    verify,
)

__all__ = [
    "LedgerCorrupt", "LedgerStore", "LedgerTampered", "Replayer", "RunNotFound",
    "RunRecorder", "SourceProbe", "StageContext", "VerifyReport", "collect_environment",
    "compare", "default_data_dir", "detect_drift", "diff_recipes", "diff_verdicts", "verify",
]

# The readings behind the number

Every reading the levelset symmetry gate made when it cleared its **model half**
for the first time, so the PASS can be checked by hand rather than taken from an
exit code.

| | |
|---|---|
| Model | `qwen2.5:3b-instruct`, Ollama, CPU only, `temperature=0.0`, `seed=42` |
| Prompt | `levelset-prompt/0.5.0` · taxonomy `gaps/1.0.0` · tool `levelset/0.4.0` |
| Corpus | `evals/levelset/symmetry_pairs.json`, all 48 pairs |
| Command | `python scripts/check_levelset_symmetry.py --run --backend ollama --model qwen2.5:3b-instruct --max-tokens 1024 --records-dir .` |
| Result | **PASS**, all nine measures — see `RESULT.txt` |

96 readings, one file per side of each pair, named `<pair_id>.<side>.json`. Each
carries the pair id, which side it was, and the gap code the pair was built to
carry, so the two sides line up for a reader. None carries an author, a handle,
a user id, or a content hash of the post: levelset records no identity, and this
must not be where one enters.

## Why the 32-pair run before this one matters more

The first model-half run used the first 32 pairs and returned **INDETERMINATE**:
seven measures passed, and `any gap recorded` sat at three discordant pairs —
every one of them the model finding a gap on the right-hand post and none on
its matched twin. Not significant. Not nothing. Exactly the kind of lean that is
invisible per reading and that a hostile reader with a spreadsheet would find.

The corpus was grown to 48, which is the remedy the gate prescribes, and re-run.
That measure went to six discordant, **2 vs 4**, p = 0.69. The lean was noise.
That is now demonstrated rather than assumed, which is the only way a PASS on a
partisanship control should ever be reached: by refusing to grant it until the
sample could have found the problem.

## This is a snapshot, not a certificate

CI's `model` job re-runs the gate on a schedule and uploads a fresh set of
readings as a build artifact. This directory records the run that first cleared
it; a later run on the same pins should agree, and one that does not is a
finding.

# Running abCA on a Windows laptop with no GPU

Open-weight models, CPU only, 16 or 32 GB of RAM, nothing leaves the machine.
This is the setup for the laptop you actually have, as opposed to the 32B
adjudicator the starter config assumes.

## The short version

Open PowerShell in the repository folder and run:

```powershell
.\scripts\setup_windows_cpu.ps1
```

It installs abCA, installs Ollama if you don't have it, pulls the models, writes
the config to `%APPDATA%\abca\config.toml`, runs `abca models check`, and then
runs one real analysis so you see the whole path work. Ten minutes, most of it
downloading weights. Re-running it is safe.

The default is the **lite** tier: one 7B model does everything and the
verification passes are off. That is the right starting point for a laptop —
see the next section for what "off" means and how to turn a pass on for a run
that matters.

Then:

```powershell
abca-gui                      # every command as a form, dark by default
abca analyze -t "..."         # or the command line
```

## Verification is optional

The pipeline was built with two "second opinion" passes on top of the analysis
itself, because whoever published its verdicts wanted them attacked first:

- **Red team** — a second model tries to knock down every verdict and can
  downgrade it. One extra model call per verdict.
- **Fidelity gate** — `abca explain` renders a statute in plain language and a
  *different* model back-translates it to prove nothing was lost. Needs a second
  model family on disk.

Neither is needed to *use* the tool. Everything that makes a verdict checkable
still runs without them: claim extraction, retrieval of the actual statute /
CFR / Federal Register text, adjudication against that text, the hash-chained
run ledger, and `abca verify`. What the passes add is a second opinion, and
the record always states whether that opinion was sought.

| | how to turn off | how to turn on |
|---|---|---|
| red team | `[defaults] red_team = false` in the config (lite does this) or `--no-red-team` on a run | `--red-team` on a run, or `red_team = true` |
| fidelity gate | leave out `[models.backtranslate]` (lite does this); `explain` then says so and exits | add `[models.backtranslate]` pointing at a *different* model than the adjudicator |

Skipping is never hidden: the run record carries `red_team: false` and a
`RED TEAM SKIPPED` note, and its input digest differs from a reviewed run's.
So the sensible workflow is lite for everyday use, and `--red-team` (or a
bigger tier) on the one run you intend to send to somebody.

## Which tier you get

Three configs in `configs/`, chosen with `-Tier`. Default is `lite`.

| | `windows-cpu-lite.toml` | `windows-cpu-16gb.toml` | `windows-cpu-32gb.toml` |
|---|---|---|---|
| adjudicator | `qwen2.5:7b-instruct-q4_K_M` (4.7 GB) | `qwen2.5:7b-instruct-q4_K_M` (4.7 GB) | `qwen2.5:14b-instruct-q4_K_M` (9 GB) |
| classifier / segmenter | same model | `qwen2.5:3b-instruct` (2 GB) | `qwen2.5:7b-instruct-q4_K_M` (4.7 GB) |
| red team | **off** | `llama3.2:3b-instruct-q4_K_M` (2 GB) | `llama3.1:8b-instruct-q4_K_M` (4.9 GB) |
| back-translation (`explain`) | not configured | `llama3.2:3b-instruct-q4_K_M` | `llama3.1:8b-instruct-q4_K_M` |
| embedder | `nomic-embed-text:v1.5` | `nomic-embed-text:v1.5` | `nomic-embed-text:v1.5` |
| peak RAM, everything loaded | ~5 GB | ~10–11 GB | ~21–22 GB |
| default profile | `fast` | `fast` | `standard` |
| one paragraph, wall clock (8-core laptop) | ~30–60 s | ~1 min | ~3–5 min |

Every tag is pinned to an exact quantisation so the ledger's weight hash means
something across runs. In the 16 GB and 32 GB configs the red team and
back-translation models are a **different family** from the adjudicator — the
loader enforces that whenever a `backtranslate` role is present.

## Why these models

- **Qwen2.5 instruct** for the roles that produce structured output. The CI
  config was validated on this family: `qwen2.5:3b` read the entire levelset
  symmetry corpus with zero malformed replies, while `qwen2.5:1.5b` failed on
  most of it. Don't go below 3B for any role that has to emit JSON.
- **Llama 3.x** for the adversarial roles, because the point of the red team is a
  model that did not produce the reasoning it is attacking.
- **q4_K_M** quantisation everywhere a 7B or larger model appears. It is the
  standard 4-bit quant — roughly a third the size of the full weights with very
  little measurable loss at this scale, and the difference between a 14B that
  fits in 32 GB and one that doesn't.
- **No "thinking" models** (Qwen3, DeepSeek-R1 distills). They interleave
  reasoning tokens with the answer and fight the grammar-constrained output the
  pipeline depends on. They also burn ten times the tokens on CPU.

## What you're giving up

A 7B or 14B adjudicator is not a 32B or 70B one. It will miss nuance on
statutory language a bigger model catches, and it is more likely to say
`UNSUPPORTED` when the source really does contain the claim. Nothing hides this:
every run record carries the exact model tags, and `abca verify` will show the
diff if the same run is re-executed on a stronger model later. The workflow that
works is: run locally, and if a verdict matters, re-run it on something bigger —
the EC2 tunnel in `docs/04-linux-ec2.md`, a friend's GPU box, or the
`--consensus` panel on the 32 GB config — and publish that run's hash.

## Keeping memory under control

- `keep_alive` in both configs is short (5–10 min) so models are evicted between
  runs. Ollama's default is 30 min.
- If a 16 GB laptop pages during a run, tell Ollama to hold **one** model at a
  time. Set this once as a user environment variable and restart Ollama from the
  tray icon:

  ```powershell
  [Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "1", "User")
  ```

  Runs get slower (each stage reloads its model, a few seconds each) but never
  exceed the largest single model's footprint.
- Close the browser. That is not a joke; a modern browser with a few dozen tabs
  is the difference between the 14B model fitting and not.

## Updating

Three things can change: the tool, the models, the config. One command handles
all three after a `git pull`:

```powershell
git pull
.\scripts\setup_windows_cpu.ps1 -Update
```

`-Update` skips the Python reinstall, re-pulls the pinned tags (a no-op if they
haven't changed — Ollama only downloads what's missing), backs up your current
`config.toml` and copies the repo's version over it, then re-runs
`abca models check`.

If you've edited `%APPDATA%\abca\config.toml` by hand and want to keep it,
don't use `-Update`; instead:

```powershell
git pull
pip install -e ".[dev,pdf]"          # the tool
ollama pull <tag>                     # any model you changed
abca models check                     # every role still resolves
```

To update Ollama itself: `winget upgrade Ollama.Ollama`, or let the tray app do
it — it checks for new versions on its own.

To move up a tier (say, you want the red team on by default): `.\scripts\setup_windows_cpu.ps1 -Tier 16gb`.

## When something's wrong

| Symptom | What it is |
|---|---|
| `abca models check` says a model is missing | The pull didn't finish. `ollama pull <tag>` by hand and watch it. |
| `abca models check` says `backtranslate` equals `adjudicator` | You edited the config into an invalid state; the loader refuses it on purpose. Pick a different family, or remove the section if you don't use `explain`. |
| `abca explain` says no model for role `backtranslate` | You're on lite. Add a `[models.backtranslate]` section (different model) or use `-Tier 16gb`. |
| A run reports `source unavailable` for a `10 ILCS` citation | ilga.gov is down or blocking you. Not a model problem; `--offline` uses the cache. |
| A stage times out | CPU is slower than the timeout assumed. Raise `timeout` for that role in the config (seconds). |
| Windows Defender flags `ollama.exe` on first run | Known false positive on fresh installs; allow it. |
| Everything is slow | It is a CPU. Use `--profile fast`, cap `max_claims`, or run the 16 GB config even on a 32 GB machine. |

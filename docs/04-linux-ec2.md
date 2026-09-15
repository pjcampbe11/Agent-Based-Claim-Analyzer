# Linux / EC2 Deployment

Target: `abca` running on an AWS EC2 GPU instance, driving a local Ollama
server, per [pjcampbe11/Qwen3.8-27B-Uncensored-on-AWS-EC2-g6-Setup-Guide](https://github.com/pjcampbe11/Qwen3.8-27B-Uncensored-on-AWS-EC2-g6-Setup-Guide).

## Why this is almost free

The CLI is pure Python with no platform-specific code paths. Two things
already handle Linux, and they were built that way in step 1 rather than
retrofitted:

* `default_data_dir()` resolves `%LOCALAPPDATA%` on Windows and honors
  `XDG_DATA_HOME` (falling back to `~/.local/share/abca`) on POSIX. Covered
  by tests on both branches.
* Every path in the ledger goes through `pathlib`, and `os.replace` is atomic
  on both platforms.

The remaining work is the provider (build step 2) and packaging.

## Two topologies

**A — analyzer on the instance.** `abca` and Ollama both run on the g6, Ollama
on `127.0.0.1:11434`. You SSH in and run the CLI there. Lowest latency, no
tunnel in the inference path, and the ledger lives on the instance's EBS
volume — so put `ABCA_LEDGER_ROOT` on a volume that survives instance
termination, or the run records die with the box.

**B — analyzer local, model remote.** `abca` runs on your workstation, Ollama
runs on the g6, and an SSH tunnel forwards `localhost:11434` to the instance.
The guide's tunnel-only design already produces exactly this: nothing is
exposed publicly, and `OLLAMA_HOST=http://127.0.0.1:11434` works unchanged on
both sides of the tunnel.

```bash
# Topology B: forward the instance's Ollama to your workstation.
ssh -i ~/.ssh/abca-g6.pem -N -L 11434:127.0.0.1:11434 ubuntu@<instance-ip>
```

Topology B is the better default. It keeps run records on a machine you
control and it means the expensive instance is stopped whenever you are not
adjudicating.

## Weight pinning over a remote Ollama

The obvious worry is that a remote model breaks the reproducibility story,
since `abca` cannot hash a weights file it cannot see. It does not, because
Ollama exposes the model's manifest digest:

```bash
curl -s http://127.0.0.1:11434/api/show -d '{"name":"<model>"}' | jq -r '.details, .digest'
```

That digest is content-addressed over the same manifest that determined the
GGUF blobs, so it pins the model as tightly as a local file hash does. Step 2
records it in `ModelIdentity.weights_hash`, which keeps `reproducible: true`
honest for both topologies. Note the corollary: two different *quantizations*
of the same model are different digests, which is correct — they produce
different outputs and must not be treated as the same recipe.

## Instance sizing

The guide's `g6.xlarge` (L4, 24 GB VRAM, ~$0.80/hr) fits a 27B at q4_K_M with
room for context. For abCA that is the `adjudicator` slot. Two consequences
for the model roles in `config.toml`:

* The `classifier` role should be a small model (7–8B). It runs on every
  claim, and on a 2,000-comment thread it runs thousands of times. Putting the
  27B there is how a `fast` profile stops being fast.
* The `backtranslate` role **must be a different model from the adjudicator**
  — enforced by a validator in `RunConfig`, not just by convention. On a
  single 24 GB card that means either a small second model resident alongside
  the 27B, or accepting a model swap between passes. The swap costs seconds;
  skipping the constraint costs the entire fidelity gate, which becomes a
  no-op when the same model grades its own simplification.

Running two models on one L4 is the practical pressure point. If it will not
fit, run the back-translation on CPU with a small model rather than
collapsing the roles.

## Cost discipline

The guide's advice — stop the instance the moment you are done — matters more
here than in a chat workflow, because abCA's `forensic` profile is
deliberately slow. Two things follow:

* `--profile fast` and aggressive source caching exist partly so most runs
  never need the GPU box at all. A `LEGAL` claim whose statute is already in
  the local corpus is a retrieval hit and a small-model adjudication.
* Batch work belongs in one session. Analyzing a user timeline (`-U`) on a
  running instance and then stopping it beats leaving it up for interactive
  one-offs.

## A note on the model choice

`orcarouter/Qwen3.8-27B-Uncensored` is an abliterated model — refusal
behavior removed. For this application that is defensible on the merits: the
analyzer's job is to examine political content including content it finds
objectionable, and a model that refuses to read a post cannot adjudicate it.

Two things worth being clear-eyed about:

1. Abliteration is known to degrade instruction-following and calibration
   somewhat, and calibration is load-bearing here — `confidence` is supposed
   to track evidence quality. The **symmetry eval** (`evals/` — claim pairs
   identical in structure, opposite in political valence) is the instrument
   that would detect it. Run it on any model before promoting it to the
   `adjudicator` role, and record the result.
2. The rigor in this system comes from the schema gates and the retrieval
   tiering, not from the model's disposition. A model cannot reach
   `SUPPORTED` without a T0–T2 citation no matter how confident or
   uninhibited it is, because that is a validator, not a prompt. This is
   precisely why the gates were built as structure rather than instructions.

## Packaging

| Target | Mechanism |
|---|---|
| Windows desktop | PyInstaller one-file `abca.exe`, signed; later winget/scoop |
| Linux / EC2 | `pipx install abca`, or a `uv`-built venv baked into the AMI |
| Reproducible box | `uv pip compile` lockfile committed, so the instance's dependency versions match what the run records claim |

That last row matters more than it looks. `EnvironmentInfo.packages` records
library versions in every run, and verification reports them on mismatch. A
locked dependency set is what keeps that field from being noise.

## Systemd unit for topology A

```ini
# /etc/systemd/system/abca-ollama.service
# Bind to loopback only. The setup guide's tunnel-only posture is the whole
# security model; binding 0.0.0.0 would put an unauthenticated model server
# on the public internet.
[Unit]
Description=Ollama for abCA
After=network-online.target

[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
ExecStart=/usr/local/bin/ollama serve
Restart=on-failure
User=ollama

[Install]
WantedBy=multi-user.target
```

`OLLAMA_KEEP_ALIVE=30m` keeps the 27B resident between claims. Without it,
each adjudication can pay a model load, which on a 24 GB card is the
difference between a usable `standard` profile and an unusable one.

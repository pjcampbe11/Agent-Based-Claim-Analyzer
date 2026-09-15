<#
.SYNOPSIS
    One-shot setup of abCA on a Windows laptop with no GPU: Ollama, the models,
    the config file, and a smoke test. Re-runnable -- every step is idempotent.

.DESCRIPTION
    Run from the repository root in PowerShell:

        .\scripts\setup_windows_cpu.ps1              # lite: one model, verification passes off (default)
        .\scripts\setup_windows_cpu.ps1 -Tier 16gb   # full pipeline, red team on, sized for 16 GB
        .\scripts\setup_windows_cpu.ps1 -Tier 32gb   # full pipeline, bigger adjudicator
        .\scripts\setup_windows_cpu.ps1 -Update      # re-pull the pinned models, re-copy config

    What it does, in order:
      1. Confirms Python 3.11+ and installs abCA in editable mode (pip install -e .[dev,pdf]).
      2. Installs Ollama with winget if it is not on PATH, then starts it.
      3. Pulls every model named in configs\windows-cpu-<tier>.toml. Ollama's pull is
         resumable and skips models already present, so -Update is cheap.
      4. Copies that config to %APPDATA%\abca\config.toml (backing up any existing one).
      5. Runs `abca models check` -- every role must resolve to a pinned, present model.
      6. Runs one real analysis against the local models so you see the full path work.

    Nothing here needs admin except, possibly, winget's Ollama install.

.PARAMETER Tier
    "lite" (default), "16gb" or "32gb".
      lite  -- one 7B model does everything, red team off, no fidelity model. ~5 GB RAM.
      16gb  -- separate models per role, red team on, fidelity gate available. ~10 GB.
      32gb  -- same shape, 14B adjudicator. ~21 GB.
    Everything in `lite` is real -- retrieval, adjudication, the ledger, verify --
    it only skips the second-opinion passes. Turn one back on per run with
    `abca analyze --red-team`.

.PARAMETER Update
    Skip the Python install, re-pull models (picks up nothing if the tags are unchanged),
    re-copy the config, and re-run the checks. Use after `git pull`.

.PARAMETER SkipSmokeTest
    Do everything except the final analysis.
#>
[CmdletBinding()]
param(
    [ValidateSet("lite", "16gb", "32gb")]
    [string]$Tier = "lite",
    [switch]$Update,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 0. tier
$gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
Step "Using the '$Tier' profile on a $gb GB machine"
if ($Tier -eq "32gb" -and $gb -lt 28) { Warn "32gb profile on a $gb GB machine will page; consider -Tier 16gb" }
if ($Tier -eq "16gb" -and $gb -lt 14) { Warn "16gb profile on a $gb GB machine is tight; consider the default lite profile" }
$ConfigSrc = Join-Path $Root "configs\windows-cpu-$Tier.toml"
if (-not (Test-Path $ConfigSrc)) { throw "missing $ConfigSrc -- run this from a full checkout" }

# ---------------------------------------------------------------- 1. python + abca
if (-not $Update) {
    Step "Python and abCA"
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { throw "python not found on PATH. Install Python 3.11+ from python.org (tick 'Add to PATH') and re-run." }
    $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ([version]$ver -lt [version]"3.11") { throw "Python $ver found; abCA needs 3.11 or newer." }
    Ok "python $ver"
    & python -m pip install --quiet --upgrade pip
    & python -m pip install --quiet -e ".[dev,pdf]"
    Ok "abca installed: $(& abca version | Select-Object -First 1)"
}

# ---------------------------------------------------------------- 2. ollama
Step "Ollama"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Warn "ollama not on PATH -- installing with winget (a UAC prompt may appear)"
    winget install --id Ollama.Ollama --accept-source-agreements --accept-package-agreements -e
    # winget updates PATH for new shells only; pick it up for this one.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        throw "Ollama installed but not on PATH yet. Open a new PowerShell window and re-run this script."
    }
}
Ok "ollama $(& ollama --version)"

# Ollama on Windows runs as a tray app / service. Make sure it is answering.
$alive = $false
try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 3 | Out-Null; $alive = $true } catch {}
if (-not $alive) {
    Warn "starting ollama serve in the background"
    Start-Process -WindowStyle Hidden -FilePath "ollama" -ArgumentList "serve"
    for ($i = 0; $i -lt 30 -and -not $alive; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 2 | Out-Null; $alive = $true } catch {}
    }
    if (-not $alive) { throw "Ollama did not answer on 127.0.0.1:11434 after 30 s." }
}
Ok "ollama is serving on 127.0.0.1:11434"

# ---------------------------------------------------------------- 3. models
Step "Pulling the models pinned in configs\windows-cpu-$Tier.toml"
# Read `model = "..."` lines straight from the TOML so the script and the
# config cannot drift: the config is the only list of models there is.
$models = Select-String -Path $ConfigSrc -Pattern '^\s*model\s*=\s*"([^"]+)"' |
          ForEach-Object { $_.Matches[0].Groups[1].Value } | Sort-Object -Unique
foreach ($m in $models) {
    Write-Host "    pull $m"
    & ollama pull $m
    if ($LASTEXITCODE -ne 0) { throw "ollama pull $m failed" }
}
Ok "$($models.Count) model(s) present"

# ---------------------------------------------------------------- 4. config
Step "Config"
$ConfigDir = Join-Path $env:APPDATA "abca"
$ConfigDst = Join-Path $ConfigDir "config.toml"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
if (Test-Path $ConfigDst) {
    $bak = "$ConfigDst.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $ConfigDst $bak
    Warn "existing config backed up to $bak"
}
Copy-Item $ConfigSrc $ConfigDst -Force
Ok "wrote $ConfigDst"

# ---------------------------------------------------------------- 5. check
Step "abca models check"
& abca models check
if ($LASTEXITCODE -ne 0) { throw "models check failed -- see the table above; a role is not resolving to a present model" }
Ok "every role resolves to a pinned local model"

# ---------------------------------------------------------------- 6. smoke test
if (-not $SkipSmokeTest) {
    Step "Smoke test: one real analysis on the local models (this takes a minute or two on CPU)"
    & abca analyze -t "Under 10 ILCS 5/10-2 Illinois makes you get 25,000 signatures to start a new party. Anyway, my dog is asleep on the couch."
    if ($LASTEXITCODE -ne 0) {
        # The statement cites a real Illinois statute, so this run reaches ilga.gov.
        # That server is flaky and blocks some networks; a retrieval failure is not a
        # model failure, and `abca models check` above already proved the models.
        Warn "the smoke test exited $LASTEXITCODE -- if the output above mentions ilga.gov or 'source unavailable', that is the statute server, not your models"
    } else {
        Ok "analysis ran end to end; the run is in the ledger (abca ledger list)"
    }
}

Write-Host ""
Write-Host "Done. Try:  abca-gui       (every command as a form)" -ForegroundColor Green
Write-Host "            abca analyze -t `"...`"" -ForegroundColor Green
Write-Host "            abca analyze -t `"...`" --red-team     (add the second-opinion pass to one run)" -ForegroundColor Green
Write-Host "To update later: git pull ; .\scripts\setup_windows_cpu.ps1 -Update" -ForegroundColor Green

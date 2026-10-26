# Artel installer for Windows (PowerShell 5+).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Dir PATH] [-WithCodex] [-WithClaude] [-Service] [-NoStart] [-Reinstall] [-Yes]
#
# One command (private repository, GitHub CLI signed in):
#   iex (gh api repos/MisterReds/artel/contents/install.ps1 -H "Accept: application/vnd.github.raw" | Out-String)
# Public repository:
#   irm https://raw.githubusercontent.com/MisterReds/artel/master/install.ps1 | iex
# `iex` cannot pass parameters, so options may also be given as environment variables: ARTEL_REINSTALL=1, ARTEL_YES=1,
# ARTEL_NOSTART=1, ARTEL_SERVICE=1, ARTEL_WITH_CODEX=1, ARTEL_WITH_CLAUDE=1. The script does not depend on its own path.
#
# -Reinstall (or ARTEL_REINSTALL=1): fresh app clone and fresh virtual environment; config, projects and credentials
# in %USERPROFILE%\.artel are kept. A clone with local changes or unpushed commits is moved to app.bak-<time>.
#
# Installs only what Artel itself needs (git, Python 3.12, and the GitHub CLI when the private repository is not
# reachable) with winget, signs in to GitHub with a device code, clones Artel into %USERPROFILE%\.artel\app (a git
# clone, so the built-in updater works), creates a virtual environment, puts `artel` on the user PATH and starts the
# dashboard. Safe to run again: it updates in place. Agent CLIs (Codex, Claude Code) are installed only when needed:
# dashboard "Провайдеры" → "Установить", or `artel providers install codex|claude` (into ~\.artel\tools).
# Environment overrides: ARTEL_REPO (URL or local path to clone), ARTEL_DIR (the clone), ARTEL_VENV (the venv).
# No param() block on purpose: it does not work through `iex`. Options come from $args (-File) or the environment.
$Opt = @{}
$Dir = if ($env:ARTEL_DIR) { $env:ARTEL_DIR } else { "$env:USERPROFILE\.artel\app" }
for ($i = 0; $i -lt $args.Count; $i++) {
  $a = "$($args[$i])".TrimStart("-").ToLower()
  if ($a -eq "dir") { $i++; $Dir = $args[$i] } else { $Opt[$a] = $true }
}
function Flag($name, $envName) { [bool]($Opt[$name] -or ([Environment]::GetEnvironmentVariable($envName) -in @("1", "true", "yes"))) }
$WithCodex = Flag "withcodex" "ARTEL_WITH_CODEX"; $WithClaude = Flag "withclaude" "ARTEL_WITH_CLAUDE"
$Service = Flag "service" "ARTEL_SERVICE"; $NoStart = Flag "nostart" "ARTEL_NOSTART"
$Reinstall = Flag "reinstall" "ARTEL_REINSTALL"; $Yes = Flag "yes" "ARTEL_YES"
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Repo = if ($env:ARTEL_REPO) { $env:ARTEL_REPO } else { "https://github.com/MisterReds/artel.git" }
$Venv = if ($env:ARTEL_VENV) { $env:ARTEL_VENV } else { "$env:USERPROFILE\.artel\venv" }

function Step($t) { Write-Host "▲ " -ForegroundColor DarkYellow -NoNewline; Write-Host $t }
function Note($t) { Write-Host "  $t" -ForegroundColor DarkGray }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Winget($id) {
  if (-not (Have winget)) { throw "winget is not available; install App Installer from the Microsoft Store, or install $id manually" }
  winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
  Refresh-Path
}
function Find-Python {  # returns the command as an array: @("py", "-3.12") or @("python")
  foreach ($c in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("python"))) {
    if (Have $c[0]) {
      $rest = @($c | Select-Object -Skip 1)
      try { & $c[0] @rest -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null; if ($LASTEXITCODE -eq 0) { return ,$c } } catch {}
    }
  }
  return $null
}
function Confirm-Step($q) {
  if ($Yes) { return $true }
  try { $a = Read-Host "  $q [Y/n]" } catch { return $true }
  return ([string]::IsNullOrEmpty($a) -or $a -match '^[YyДд]')
}
function Safe-Path($p) { return ($p -and $p.Length -gt 8 -and $p.TrimEnd('\') -ne $env:USERPROFILE.TrimEnd('\')) }
function Reinstall-Prepare {
  if (-not (Safe-Path $Dir)) { throw "refusing to remove '$Dir'" }
  if (-not (Safe-Path $Venv)) { throw "refusing to remove '$Venv'" }
  $dirty = ""
  if (Test-Path "$Dir\.git") {
    if (git -C $Dir status --porcelain 2>$null) { $dirty = "uncommitted changes" }
    elseif (git -C $Dir log --branches --not --remotes --oneline 2>$null) { $dirty = "unpushed commits" }
  } elseif (Test-Path $Dir) { $dirty = "not a git clone" }
  $bak = "$Dir.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
  Step "Reinstall"
  Note "will remove: $Venv (virtual environment)"
  if ($dirty) { Note "will move aside ($dirty): $Dir -> $bak" } elseif (Test-Path $Dir) { Note "will remove: $Dir (app clone)" }
  Note "kept untouched: everything else (config, projects, credentials, tools)"
  if (-not (Confirm-Step "Reinstall Artel?")) { throw "cancelled" }
  if ($env:ARTEL_PARENT_PID) { try { Wait-Process -Id ([int]$env:ARTEL_PARENT_PID) -Timeout 60 -ErrorAction SilentlyContinue } catch {} }
  $task = Get-ScheduledTask -TaskName "Artel dashboard" -ErrorAction SilentlyContinue
  if ($task) { Stop-ScheduledTask -TaskName "Artel dashboard" -ErrorAction SilentlyContinue; $script:Service = $true }
  Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -like "$Venv\*" } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  if ($dirty) { Move-Item $Dir $bak; Note "old app kept at: $bak" }
  elseif (Test-Path $Dir) { Remove-Item -Recurse -Force $Dir }
  if (Test-Path $Venv) {
    if (-not (Test-Path "$Venv\pyvenv.cfg")) { throw "$Venv is not a virtual environment; not removing it" }
    Remove-Item -Recurse -Force $Venv
  }
}
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
function Py { $rest = @($script:py | Select-Object -Skip 1); & $script:py[0] @rest @args }

Step "Artel installer — Windows"
Step "Dependencies"
if (-not (Have git)) { Note "installing git"; Winget "Git.Git" }
$py = Find-Python
if (-not $py) { Note "installing Python 3.12"; Winget "Python.Python.3.12"; $py = Find-Python }
if (-not $py) { throw "Python 3.10+ is required" }
Note ("git " + (git --version) + " · " + (Py --version))

Step "Access to the repository"
git ls-remote $Repo HEAD 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  if (-not (Have gh)) { Note "installing GitHub CLI for sign-in"; Winget "GitHub.cli" }
  gh auth status 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { Note "sign in to GitHub (device code: open the link, enter the code)"; gh auth login --hostname github.com --git-protocol https --web }
  gh auth setup-git
  git ls-remote $Repo HEAD 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "no access to the repository with this GitHub account" }
}
Note "ok"

if ($Reinstall) { Reinstall-Prepare }

Step "Artel → $Dir"
if (Test-Path "$Dir\.git") { git -C $Dir pull --ff-only --quiet; Check "git pull"; Note ("updated to " + (git -C $Dir rev-parse --short HEAD)) }
else { New-Item -ItemType Directory -Force -Path (Split-Path $Dir) | Out-Null; git clone --quiet $Repo $Dir; Check "git clone"; Note ("cloned " + (git -C $Dir rev-parse --short HEAD)) }

Step "Python environment → $Venv"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { Py -m venv $Venv; Check "python -m venv" }
& "$Venv\Scripts\python.exe" -m pip install --quiet --upgrade pip; Check "pip upgrade"
& "$Venv\Scripts\python.exe" -m pip install --quiet -e "${Dir}[dev]"; Check "pip install"  # [dev] = pytest for the updater's checks
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Venv\Scripts*") {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$Venv\Scripts", "User"); Refresh-Path
  Note "added $Venv\Scripts to the user PATH (new terminals pick it up)"
}

if ($WithCodex) { Step "Codex CLI (ChatGPT subscription)"; & "$Venv\Scripts\artel.exe" providers install codex; Check "codex install" }
if ($WithClaude) { Step "Claude Code CLI (Claude subscription)"; & "$Venv\Scripts\artel.exe" providers install claude; Check "claude install" }

if ($Service) {
  Step "Dashboard at sign-in (Task Scheduler)"
  $action = New-ScheduledTaskAction -Execute "$Venv\Scripts\artel.exe" -Argument "web"
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  Register-ScheduledTask -TaskName "Artel dashboard" -Action $action -Trigger $trigger -Force | Out-Null
  Start-ScheduledTask -TaskName "Artel dashboard"
  Note "task 'Artel dashboard' registered"
  $NoStart = $true
}

Step "Check"
& "$Venv\Scripts\artel.exe" providers
Write-Host "`n▲ Artel установлен." -ForegroundColor DarkYellow
Note "дашборд:    artel web  →  http://127.0.0.1:8765"
Note "Codex / Claude Code: по необходимости — «Провайдеры» → «Установить», затем вход"
Note "обновление: кнопка в дашборде или artel update"
if (-not $NoStart) {
  Start-Job { Start-Sleep 2; Start-Process "http://127.0.0.1:8765" } | Out-Null
  & "$Venv\Scripts\artel.exe" web
}

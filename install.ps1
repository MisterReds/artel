# Artel installer for Windows (PowerShell 5+).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Dir PATH] [-WithCodex] [-WithClaude] [-Service] [-NoStart]
#
# Installs only what Artel itself needs (git, Python 3.12, and the GitHub CLI when the private repository is not
# reachable) with winget, signs in to GitHub with a device code, clones Artel into %USERPROFILE%\.artel\app (a git
# clone, so the built-in updater works), creates a virtual environment, puts `artel` on the user PATH and starts the
# dashboard. Safe to run again: it updates in place. Agent CLIs (Codex, Claude Code) are installed only when needed:
# dashboard "Провайдеры" → "Установить", or `artel providers install codex|claude` (into ~\.artel\tools).
# Environment overrides: ARTEL_REPO (URL or local path to clone), ARTEL_DIR (the clone), ARTEL_VENV (the venv).
param(
  [string]$Dir = $(if ($env:ARTEL_DIR) { $env:ARTEL_DIR } else { "$env:USERPROFILE\.artel\app" }),
  [switch]$WithCodex, [switch]$WithClaude, [switch]$Service, [switch]$NoStart
)
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

Step "Artel → $Dir"
if (Test-Path "$Dir\.git") { git -C $Dir pull --ff-only --quiet; Check "git pull"; Note ("updated to " + (git -C $Dir rev-parse --short HEAD)) }
else { New-Item -ItemType Directory -Force -Path (Split-Path $Dir) | Out-Null; git clone --quiet $Repo $Dir; Check "git clone"; Note ("cloned " + (git -C $Dir rev-parse --short HEAD)) }

Step "Python environment → $Venv"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { Py -m venv $Venv; Check "python -m venv" }
& "$Venv\Scripts\python.exe" -m pip install --quiet --upgrade pip; Check "pip upgrade"
& "$Venv\Scripts\python.exe" -m pip install --quiet -e $Dir; Check "pip install"
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

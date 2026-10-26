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
# in %USERPROFILE%\.artel are kept. The app dir is DELETED only when it is %USERPROFILE%\.artel\app or %ARTEL_HOME%\app and
# git shows nothing to lose (no changes, ignored files, stash, unpushed commits, extra worktrees; a git error counts as
# "not sure"); otherwise it is MOVED to <dir>.bak-<time>, never deleted. Same for a venv that is not the default one.
# An empty path, a drive root, the home directory, ARTEL_HOME itself or a dir holding artel.yaml / projects is refused.
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
function Git-Run {  # git with stderr tolerated (Windows PowerShell 5.1 turns native stderr into errors under Stop)
  $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  $out = @(); $ok = $false
  try { $out = @(& git @args 2>$null | ForEach-Object { "$_" }); $ok = ($LASTEXITCODE -eq 0) } catch { $ok = $false } finally { $ErrorActionPreference = $old }
  return [pscustomobject]@{ Ok = $ok; Out = (($out) -join "`n").Trim() }
}
function Canon($p) {
  if (-not $p) { return "" }
  try { return ([IO.Path]::GetFullPath($p)).TrimEnd('\', '/') } catch { return "" }
}
function Same-Path($a, $b) { $x = Canon $a; $y = Canon $b; return ($x -and $y -and $x.Equals($y, [StringComparison]::OrdinalIgnoreCase)) }
function Inside-Path($child, $parent) {  # $child is $parent or lies below it
  $c = Canon $child; $p = Canon $parent
  if (-not $c -or -not $p) { return $false }
  return ($c.Equals($p, [StringComparison]::OrdinalIgnoreCase) -or $c.StartsWith($p + "\", [StringComparison]::OrdinalIgnoreCase))
}
function Artel-Home { if ($env:ARTEL_HOME) { $env:ARTEL_HOME } else { Join-Path $env:USERPROFILE ".artel" } }
function Refuse-Path($label, $p) {
  $c = Canon $p
  if (-not $p -or -not $c) { throw "refusing: $label is empty or invalid" }
  if (-not (Split-Path -Parent $c)) { throw "refusing: $label is a drive root ($p)" }
  if (Same-Path $c $env:USERPROFILE) { throw "refusing: $label is your home directory ($p)" }
  if (Same-Path $c (Artel-Home)) { throw "refusing: $label is the Artel data directory itself ($p)" }
  if ((Test-Path -LiteralPath (Join-Path $c "artel.yaml")) -or (Test-Path -LiteralPath (Join-Path $c "projects") -PathType Container)) {
    throw "refusing: $label ($p) contains Artel data (artel.yaml or projects)"
  }
  if ((Inside-Path (Artel-Home) $c) -or (Inside-Path $env:USERPROFILE $c)) { throw "refusing: $label ($p) contains the Artel data or home directory" }
}
function Is-Default-Path($p, $name) {
  return ((Same-Path $p (Join-Path (Join-Path $env:USERPROFILE ".artel") $name)) -or (Same-Path $p (Join-Path (Artel-Home) $name)))
}
function App-Is-Disposable {  # $true only when nothing in $Dir can be lost; any git failure = not sure = $false
  if (-not (Test-Path -LiteralPath (Join-Path $Dir ".git") -PathType Container)) { return $false }
  $r = Git-Run -C $Dir status --porcelain --ignored; if (-not $r.Ok) { return $false }
  # build and tool caches that an install itself creates are not the owner's work; anything else is
  $left = @("$($r.Out)" -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^!! (.*/)?(__pycache__|[^/]+\.egg-info|\.pytest_cache|\.ruff_cache|\.mypy_cache)/$' })
  if ($left.Count) { return $false }
  $r = Git-Run -C $Dir stash list; if (-not $r.Ok -or $r.Out) { return $false }
  $r = Git-Run -C $Dir log HEAD --branches --tags --not --remotes --oneline; if (-not $r.Ok -or $r.Out) { return $false }
  $r = Git-Run -C $Dir worktree list --porcelain; if (-not $r.Ok) { return $false }
  if (@($r.Out -split "`n" | Where-Object { $_ -like "worktree *" }).Count -ne 1) { return $false }
  return $true
}
function Move-Aside($path, $label) {  # never deletes
  $dst = "$path.bak-$script:Ts"
  if (Test-Path -LiteralPath $dst) { $dst = "$dst-$PID" }
  Move-Item -LiteralPath $path -Destination $dst
  Note "old $label kept at: $dst"
}
function Reinstall-Prepare {
  $script:Ts = Get-Date -Format "yyyyMMdd-HHmmss"
  Refuse-Path "app directory" $Dir
  Refuse-Path "virtual environment" $Venv
  $appAction = ""; $venvAction = ""; $why = ""
  if (Test-Path -LiteralPath $Dir) {
    if (-not (Is-Default-Path $Dir "app")) { $appAction = "move"; $why = "not the default location" }
    elseif (App-Is-Disposable) { $appAction = "delete" }
    else { $appAction = "move"; $why = "uncommitted/ignored files, stash, unpushed commits, extra worktrees, or git could not be checked" }
  }
  if (Test-Path -LiteralPath $Venv) {
    if ((Test-Path -LiteralPath (Join-Path $Venv "pyvenv.cfg")) -and (Is-Default-Path $Venv "venv")) { $venvAction = "delete" } else { $venvAction = "move" }
  }
  Step "Reinstall"
  if ($venvAction -eq "delete") { Note "will remove: $Venv (virtual environment)" } elseif ($venvAction -eq "move") { Note "will move aside (never deleted): $Venv -> $Venv.bak-$script:Ts" }
  if ($appAction -eq "delete") { Note "will remove: $Dir (clean app clone, nothing to lose)" }
  elseif ($appAction -eq "move") { Note "will move aside, NOT delete ($why): $Dir -> $Dir.bak-$script:Ts" }
  Note "kept untouched: everything else (config, projects, credentials, tools)"
  if (-not (Confirm-Step "Reinstall Artel?")) { throw "cancelled" }
  if ($env:ARTEL_PARENT_PID) { try { Wait-Process -Id ([int]$env:ARTEL_PARENT_PID) -Timeout 60 -ErrorAction SilentlyContinue } catch {} }
  $task = Get-ScheduledTask -TaskName "Artel dashboard" -ErrorAction SilentlyContinue
  if ($task) { Stop-ScheduledTask -TaskName "Artel dashboard" -ErrorAction SilentlyContinue; $script:Service = $true }
  Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith("$Venv\", [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  if ($appAction -eq "delete") { Remove-Item -LiteralPath $Dir -Recurse -Force } elseif ($appAction -eq "move") { Move-Aside $Dir "app" }
  if ($venvAction -eq "delete") { Remove-Item -LiteralPath $Venv -Recurse -Force } elseif ($venvAction -eq "move") { Move-Aside $Venv "virtual environment" }
}
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }
function Py { $rest = @($script:py | Select-Object -Skip 1); & $script:py[0] @rest @args }

Step "Artel installer — Windows"
Step "Dependencies"
if (-not (Have git)) { Note "installing git"; Winget "Git.Git" }
$py = Find-Python
if (-not $py) { Note "installing Python 3.12"; Winget "Python.Python.3.12"; $py = Find-Python }
if (-not $py) { throw "Python 3.10+ is required" }
Note ("git " + (Git-Run --version).Out + " · " + (Py --version))

Step "Access to the repository"
if (-not (Git-Run ls-remote $Repo HEAD).Ok) {
  if (-not (Have gh)) { Note "installing GitHub CLI for sign-in"; Winget "GitHub.cli" }
  gh auth status 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { Note "sign in to GitHub (device code: open the link, enter the code)"; gh auth login --hostname github.com --git-protocol https --web }
  gh auth setup-git
  if (-not (Git-Run ls-remote $Repo HEAD).Ok) { throw "no access to the repository with this GitHub account" }
}
Note "ok"

if ($Reinstall) { Reinstall-Prepare }

Step "Artel → $Dir"
if (Test-Path -LiteralPath "$Dir\.git") {
  if (-not (Git-Run -C $Dir pull --ff-only --quiet).Ok) { throw "git pull failed" }
  Note ("updated to " + (Git-Run -C $Dir rev-parse --short HEAD).Out)
} else {
  New-Item -ItemType Directory -Force -Path (Split-Path $Dir) | Out-Null
  if (-not (Git-Run clone --quiet $Repo $Dir).Ok) { throw "git clone failed" }
  Note ("cloned " + (Git-Run -C $Dir rev-parse --short HEAD).Out)
}

Step "Python environment → $Venv"
if (-not (Test-Path -LiteralPath "$Venv\Scripts\python.exe")) { Py -m venv $Venv; Check "python -m venv" }
& "$Venv\Scripts\python.exe" -m pip install --quiet --upgrade pip; Check "pip upgrade"
& "$Venv\Scripts\python.exe" -m pip install --quiet -e "${Dir}[dev]"; Check "pip install"  # [dev] = pytest for the updater's checks
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath + "").IndexOf("$Venv\Scripts", [StringComparison]::OrdinalIgnoreCase) -lt 0) {
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

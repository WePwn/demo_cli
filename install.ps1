# demo_cli one-command install (Windows / PowerShell).
#
#   irm https://raw.githubusercontent.com/WePwn/demo_cli/beta/install.ps1 | iex
#
# Yes, this is irm-pipe-iex. demo_cli's whole point is that you read code
# before you run it - so read this first. It does four things: install, init,
# hook, verify. It phones nobody.
#
# Works on Windows PowerShell 5.1 and PowerShell 7+.

$ErrorActionPreference = "Stop"
function Say ($m){ Write-Host $m }
function OK  ($m){ Write-Host $m -ForegroundColor Green }
function Warn($m){ Write-Host $m -ForegroundColor Yellow }
function Die ($m){ Write-Host $m -ForegroundColor Red; exit 1 }

Say ""
Say "demo_cli installer (Windows)"
Say "----------------------------"

$py = $null
foreach ($c in @("python","python3","py")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) { Die "Python 3.9+ not found. Install from python.org, then re-run." }
Say ("  python  " + (& $py -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null))

if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
  Warn "  pipx not found - installing it"
  & $py -m pip install --user -q pipx
  & $py -m pipx ensurepath | Out-Null
  $userbase = & $py -c "import site;print(site.getuserbase())"
  $scripts  = Join-Path $userbase "Scripts"
  if (Test-Path $scripts) { $env:Path = "$scripts;$env:Path" }
}
Say "  installing demo_cli (pipx)..."
$src = if ($env:DEMO_CLI_LOCAL) { $env:DEMO_CLI_LOCAL } else { "git+https://github.com/WePwn/demo_cli.git@beta" }
try { pipx install --force $src 2>&1 | Out-Null }
catch { Die "pipx install failed - try:  pipx install $src" }

if (-not (Get-Command demo_cli -ErrorAction SilentlyContinue)) {
  $bin = (pipx environment --value PIPX_BIN_DIR) 2>$null
  if (-not $bin) { $bin = Join-Path $env:USERPROFILE ".local\bin" }
  Warn ""
  Warn "  demo_cli installed but not on PATH in this session."
  Warn "  add it and open a NEW terminal:"
  Warn ("      setx PATH `"" + $bin + ";%PATH%`"")
  Warn ""
  Warn "  until demo_cli is on PATH where you launch 'claude', Claude Code runs"
  Warn "  with NO protection and stays silent."
  exit 1
}
OK ("  demo_cli on PATH: " + (Get-Command demo_cli).Source)

Say "  wiring into this project..."
demo_cli init 2>&1 | Out-Null
try { demo_cli install-hook 2>&1 | Out-Null } catch { Warn "  install-hook: is .claude\settings.json writable?" }

Say ""
demo_cli doctor
Say ""
$st = (demo_cli status 2>$null | Out-String)
if ($st -match "hook installed\s*:?\s*yes") {
  OK "hook live. Your next destructive command gets a recovery point."
  OK "(shadow mode by default: observes and snapshots, never blocks.)"
} else {
  Warn "hook not confirmed. Run 'demo_cli doctor' above and fix what's red."
}
Say ""
Say "  try it:   demo_cli check `"rm -rf ./build`"  --mode enforce"
Say "  undo:     demo_cli undo"
Say ""

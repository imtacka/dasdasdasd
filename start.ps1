$ErrorActionPreference = "Stop"

if (-not $env:TELEGRAM_BOT_TOKEN) {
  Write-Host "Set TELEGRAM_BOT_TOKEN first:"
  Write-Host '$env:TELEGRAM_BOT_TOKEN="your_new_bot_token"'
  exit 1
}

$PythonCandidates = @(
  "python",
  "py",
  "C:\Users\imtacka\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)

foreach ($Python in $PythonCandidates) {
  try {
    & $Python -m py_compile "$PSScriptRoot\bot.py" 2>$null
    & $Python "$PSScriptRoot\bot.py"
    exit $LASTEXITCODE
  } catch {
  }
}

Write-Host "Python was not found. Install Python or use the bundled Codex runtime path."
exit 1

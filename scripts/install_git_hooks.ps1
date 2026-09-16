param([string]$Python)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
git -C $root config core.hooksPath .githooks
if ($Python) {
  $resolved = (Resolve-Path -LiteralPath $Python).Path
  & $resolved --version | Out-Null
  git -C $root config superagent.python $resolved
}
Write-Host 'Git hooks enabled: pre-commit=fast gate, pre-push=full gate'

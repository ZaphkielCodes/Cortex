param(
    [switch]$ValidateOnly,
    [ValidateSet('layout_change', 'canvas_dimensions', 'unsent_document')]
    [string]$Case,
    [string]$Python = "py",
    [string]$OutputDirectory
)
$ErrorActionPreference = "Stop"
$CortexRoot = Split-Path -Parent $PSScriptRoot
$Manifest = Join-Path $CortexRoot "benchmarks\a10_compound_acceptance.json"
$Cases = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
if ($Cases.Count -ne 3) { throw "Expected three frozen acceptance tasks." }
if (-not $ValidateOnly -and -not $Case) { throw "Choose one -Case: layout_change, canvas_dimensions, or unsent_document. Review its semantic evidence before starting the next case." }
if ($ValidateOnly) {
    Write-Output "Prepared frozen compound acceptance. Selected case: $Case. No provider or desktop calls made."
    exit 0
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $CortexRoot ("scratch\compound-acceptance-" + (Get-Date -Format "yyyyMMdd-HHmmssfff") + '-' + [guid]::NewGuid().ToString('N').Substring(0,8))
}
$OutputDirectory = Join-Path $OutputDirectory $Case
if (Test-Path -LiteralPath $OutputDirectory) { throw "Use a new output directory to preserve prior evidence." }
Write-Output "Run after quota replenishes, using blank test documents. Review each test window before approving model sharing."
Push-Location $CortexRoot
try {
    & $Python -X utf8 scripts/live_benchmark.py --manifest $Manifest --output $OutputDirectory --ids $Case --manual-approvals
    if ($LASTEXITCODE -ne 0) { throw "Acceptance runner failed. Inspect the saved logs." }
    Write-Output "Evidence saved in $OutputDirectory. Review completion predicates and compound_trace; HTTP success alone is not a pass."
} finally {
    Pop-Location
}

param(
    [string]$ApiBase = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"

try {
    $health = Invoke-RestMethod -Uri "$ApiBase/api/health"
} catch {
    Write-Host "Backend: FAILED" -ForegroundColor Red
    throw
}

$pipeline = $health.obligation_extraction.pipeline_version
$gapPipeline = $health.gap_pipeline.pipeline_version
$benchmarkVersion = $health.benchmark.version
$crawlerEnabled = [bool]$health.crawler.enabled
$crawlerNetwork = [bool]$health.crawler.network_access
$frontendSource = Join-Path $PSScriptRoot "frontend\src\App.tsx"
$frontendPipeline = "missing"

if (Test-Path $frontendSource) {
    $frontendMatch = Select-String `
        -Path $frontendSource `
        -Pattern "const REQUIRED_GAP_PIPELINE = '([^']+)';" |
        Select-Object -First 1
    if ($frontendMatch -and $frontendMatch.Matches.Count -gt 0) {
        $frontendPipeline = $frontendMatch.Matches[0].Groups[1].Value
    }
}

Write-Host "Backend: OK" -ForegroundColor Green
Write-Host "Obligation pipeline: $pipeline"
Write-Host "Gap pipeline: $gapPipeline"
Write-Host "Benchmark pack: $benchmarkVersion"
Write-Host "Frontend requires gap pipeline: $frontendPipeline"
Write-Host "Crawler enabled: $crawlerEnabled"
Write-Host "Crawler network access: $crawlerNetwork"

$crawlerStatus = 0
$crawlerRows = 0
$crawlerVersion = $health.crawler.version
$topicNetworkRequests = -1
$filesBundled = 0
$categoryCountsVerified = $false
try {
    $crawlerResult = Invoke-RestMethod `
        -Method Post `
        -Uri "$ApiBase/api/crawler/search" `
        -ContentType "application/json" `
        -Body '{"section":"All","year":"All","refresh":false}'
    $crawlerStatus = 200
    $crawlerRows = @($crawlerResult.records).Count
    $topicNetworkRequests = [int]$crawlerResult.network_requests
    $filesBundled = [int]$crawlerResult.cache_status.files_bundled
    $insurer = $crawlerResult.category_status.'Insurer / Micro Insurer'
    $joint = $crawlerResult.category_status.'Joint FSCA / PA Directives'
    $retirement = $crawlerResult.category_status.'Retirement Fund'
    $categoryCountsVerified = (
        $insurer.complete -and [int]$insurer.indexed -eq 40 -and
        $joint.complete -and [int]$joint.indexed -eq 2 -and
        $retirement.complete -and [int]$retirement.indexed -eq 8
    )
} catch {
    if ($_.Exception.Response) {
        $crawlerStatus = [int]$_.Exception.Response.StatusCode
    } else {
        throw
    }
}

Write-Host "Crawler endpoint HTTP status: $crawlerStatus"
Write-Host "Crawler version: $crawlerVersion"
Write-Host "Crawler directive rows: $crawlerRows"
Write-Host "Bundled official files: $filesBundled"
Write-Host "Crawler category populations verified: $categoryCountsVerified"
Write-Host "Topic-selection FSCA requests: $topicNetworkRequests"

$removedControlsAbsent = $true
if (Test-Path $frontendSource) {
    $removedControlsAbsent = -not [bool](Select-String `
        -Path $frontendSource `
        -Pattern "Pull Complete Topic|Refresh Selected Topic" `
        -Quiet)
}
Write-Host "Pull/refresh controls removed: $removedControlsAbsent"

if (
    $pipeline -ne "2026-08-06.2" -or
    $gapPipeline -ne "2026-08-18.2-neutral-recommendations" -or
    $benchmarkVersion -ne "2026-07-27.5" -or
    $frontendPipeline -ne "2026-08-18.2-neutral-recommendations" -or
    -not $crawlerEnabled -or
    $crawlerNetwork -or
    $crawlerVersion -ne "2026-08-23-demo.1" -or
    $crawlerStatus -ne 200 -or
    $crawlerRows -ne 50 -or
    $filesBundled -ne 50 -or
    -not $categoryCountsVerified -or
    $topicNetworkRequests -ne 0 -or
    -not $removedControlsAbsent
) {
    Write-Host "Offline library verification: FAILED" -ForegroundColor Red
    exit 1
}

Write-Host "Offline library: VERIFIED" -ForegroundColor Green

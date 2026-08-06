[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repoRoot

function Stop-Startup {
    param([Parameter(Mandatory = $true)][string]$Message)

    throw "[start-local] $Message"
}

function Invoke-DockerChecked {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "`n==> $Description" -ForegroundColor Cyan
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Startup "$Description failed with exit code $LASTEXITCODE."
    }
}

function Read-DotEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)\s*$') {
            $value = $Matches['value'].Trim()
            if ($value.Length -ge 2) {
                $first = $value.Substring(0, 1)
                $last = $value.Substring($value.Length - 1, 1)
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            $values[$Matches['key']] = $value
        }
    }
    return $values
}

function Get-ConfiguredValue {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Values,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Default
    )

    if ($Values.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace([string]$Values[$Name])) {
        return [string]$Values[$Name]
    }
    return $Default
}

function Require-ConfiguredValue {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Values,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not $Values.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace([string]$Values[$Name])) {
        Stop-Startup ".env is missing a value for $Name."
    }
}

function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Uri
    )

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 30
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
            Stop-Startup "$Name returned HTTP $($response.StatusCode)."
        }
        Write-Host "${Name}: OK ($Uri)" -ForegroundColor Green
    }
    catch {
        Stop-Startup "$Name is not healthy at $Uri. $($_.Exception.Message)"
    }
}

$envPath = Join-Path $repoRoot ".env"
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Stop-Startup ".env was not found. Create it from .env.example and fill in the local secrets first."
}

$envValues = Read-DotEnv -Path $envPath
$requiredValues = @(
    "APP_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "MODEL_PROVIDER",
    "MODEL_ALLOW_EXTERNAL",
    "FAST_CHAT_ENDPOINT",
    "FAST_CHAT_MODEL",
    "FAST_CHAT_API_KEY",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_ENDPOINT"
)
foreach ($name in $requiredValues) {
    Require-ConfiguredValue -Values $envValues -Name $name
}

if (([string]$envValues["MODEL_PROVIDER"]).ToLowerInvariant() -ne "openai-compatible") {
    Stop-Startup "MODEL_PROVIDER must be openai-compatible for the configured Chat API."
}
if (([string]$envValues["MODEL_ALLOW_EXTERNAL"]).ToLowerInvariant() -ne "true") {
    Stop-Startup "MODEL_ALLOW_EXTERNAL must be true for the configured external Chat endpoint."
}
if (([string]$envValues["EMBEDDING_PROVIDER"]).ToLowerInvariant() -ne "text-embeddings-inference") {
    Stop-Startup "EMBEDDING_PROVIDER must be text-embeddings-inference for the local TEI profile."
}

# Force the effective Compose values for this run. This does not modify .env or expose secrets.
$env:RERANKER_PROVIDER = "inherit"
$env:RERANKER_ENDPOINT = "http://tei-reranker:80"
$env:RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
$env:EMBEDDING_PROVIDER = "text-embeddings-inference"
$env:EMBEDDING_ENDPOINT = "http://tei:80"

$apiPort = Get-ConfiguredValue -Values $envValues -Name "API_PORT" -Default "8000"
$webPort = Get-ConfiguredValue -Values $envValues -Name "WEB_PORT" -Default "5173"
$embeddingPort = Get-ConfiguredValue -Values $envValues -Name "EMBEDDING_PORT" -Default "8080"
$rerankerPort = Get-ConfiguredValue -Values $envValues -Name "RERANKER_PORT" -Default "8081"

Write-Host "Using .env from $envPath" -ForegroundColor Gray
Write-Host "Effective retrieval: local TEI embedding + real TEI reranker" -ForegroundColor Gray
Write-Host "Chat credentials: loaded from .env (secret value hidden)" -ForegroundColor Gray

Invoke-DockerChecked `
    -Description "Check Docker Engine" `
    -Arguments @("info", "--format", "{{.ServerVersion}}")

Invoke-DockerChecked `
    -Description "Check NVIDIA GPU passthrough" `
    -Arguments @(
        "run", "--rm", "--gpus", "all",
        "nvidia/cuda:12.4.0-base-ubuntu22.04",
        "nvidia-smi"
    )

$composeArguments = @(
    "compose",
    "-f", "deploy/compose.yaml",
    "--env-file", ".env",
    "--profile", "embedding",
    "--profile", "reranker"
)

Invoke-DockerChecked `
    -Description "Build and start the complete GPU stack" `
    -Arguments ($composeArguments + @("up", "--build", "--force-recreate", "--detach", "--wait"))

Invoke-DockerChecked `
    -Description "Show service status" `
    -Arguments ($composeArguments + @("ps"))

Test-HttpEndpoint -Name "API live" -Uri "http://127.0.0.1:$apiPort/api/v1/health/live"
Test-HttpEndpoint -Name "API ready" -Uri "http://127.0.0.1:$apiPort/api/v1/health/ready"
Test-HttpEndpoint -Name "Web proxy" -Uri "http://127.0.0.1:$webPort/api/v1/health/ready"
Test-HttpEndpoint -Name "Embedding TEI" -Uri "http://127.0.0.1:$embeddingPort/health"
Test-HttpEndpoint -Name "Reranker TEI" -Uri "http://127.0.0.1:$rerankerPort/health"

Write-Host "`nStartup complete. Open http://127.0.0.1:$webPort" -ForegroundColor Green

param(
    [ValidateSet("Start", "Status", "Test", "Run", "Chat", "Stop")]
    [string]$Action = "Status",
    [string]$TaskFile,
    [string]$Workspace,
    [ValidateRange(1024, 262144)]
    [int]$ContextLength = 32768
)

$ErrorActionPreference = "Stop"

$lmsPath = Join-Path $env:USERPROFILE ".lmstudio\bin\lms.exe"
$modelKey = "qwen3-coder-30b-a3b-instruct"
$modelIdentifier = "forge-30b"
$serverPort = 1234

if (-not (Test-Path -LiteralPath $lmsPath)) {
    throw "LM-Studio-CLI nicht gefunden: $lmsPath"
}

function Get-LoadedModelsJson {
    $output = & $lmsPath ps --json | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Der Modellstatus konnte nicht gelesen werden."
    }
    return $output
}

function Get-AgentLoadedModel {
    param([switch]$OwnOnly)
    $models = @(Get-LoadedModelsJson | ConvertFrom-Json)
    $loaded = $models |
        Where-Object { $_.identifier -eq $modelIdentifier } |
        Select-Object -First 1
    if ($null -eq $loaded -and -not $OwnOnly) {
        $loaded = $models |
            Where-Object { $_.modelKey -eq $modelKey } |
            Select-Object -First 1
    }
    if ($null -eq $loaded) {
        return $null
    }
    return $loaded
}

function Get-AgentModelIdentifier {
    $loaded = Get-AgentLoadedModel
    if ($null -eq $loaded) {
        return $null
    }
    return [string]$loaded.identifier
}

function Confirm-AgentLoadedModel {
    param($Loaded, [switch]$OwnOnly)
    if ($null -eq $Loaded) {
        throw "Die erfolgreiche Ladeantwort ist nicht durch eine vorhandene Forge-Instanz bestätigt. Keine Serveraktion ausgeführt."
    }
    if (($Loaded.identifier -isnot [string]) -or [string]::IsNullOrWhiteSpace($Loaded.identifier) -or
        ($OwnOnly -and $Loaded.identifier -ne $modelIdentifier)) {
        throw "Kennung des geladenen Modells ist nicht verlässlich lesbar oder bestätigt nicht die eigene Forge-Instanz. Keine Serveraktion ausgeführt."
    }
    if (($Loaded.type -isnot [string]) -or ($Loaded.modelKey -isnot [string]) -or
        $Loaded.type -ne "llm" -or $Loaded.modelKey -ne $modelKey) {
        throw "Die vorhandene Forge-Modellkennung gehört nicht zum vorgesehenen Qwen3-Coder-Modell. Keine Serveraktion ausgeführt."
    }
    $actualContext = $Loaded.contextLength
    $maximumContext = $Loaded.maxContextLength
    if ((($actualContext -isnot [int]) -and ($actualContext -isnot [long])) -or
        (($maximumContext -isnot [int]) -and ($maximumContext -isnot [long])) -or
        $actualContext -le 0 -or $maximumContext -lt $actualContext) {
        throw "Kontextgröße des geladenen Modells ist nicht verlässlich lesbar. Erwartet werden contextLength und maxContextLength als positive ganze Zahlen; Modell wird nicht übernommen oder verändert."
    }
    if ((($Loaded.parallel -isnot [int]) -and ($Loaded.parallel -isnot [long])) -or $Loaded.parallel -ne 1) {
        throw "Parallelität des geladenen Modells ist nicht verlässlich passend: Erforderlich ist parallel=1. Keine Serveraktion ausgeführt."
    }
    if ($ContextLength -gt $maximumContext) {
        throw "Angeforderter Kontext $ContextLength überschreitet das gemeldete Modellmaximum $maximumContext. Keine Serveraktion ausgeführt."
    }
    if ($actualContext -ne $ContextLength) {
        $identifier = [string]$Loaded.identifier
        if ($identifier -eq $modelIdentifier) {
            $quotedLms = $lmsPath.Replace("'", "''")
            $reloadHint = "Nur wenn kein Forge-Aufruf läuft, die eigene Instanz gezielt entladen: & '$quotedLms' unload '$modelIdentifier'. Danach aus dem Repository: powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./scripts/manage_agent1.ps1 -Action Start -ContextLength $ContextLength. Der Server muss dafür nicht beendet werden."
        }
        else {
            $reloadHint = "Die vorhandene Instanz '$identifier' wird nicht entladen. Die gewünschte eigene Instanz '$modelIdentifier' muss ausdrücklich mit $ContextLength Tokens geladen werden, bevor Forge sie übernehmen kann."
        }
        throw "Geladener Kontext: $actualContext Tokens; angefordert: $ContextLength Tokens. Vorhandenes Modell wird nicht stillschweigend wiederverwendet. $reloadHint"
    }
    return [string]$Loaded.identifier
}

function Start-AgentRuntime {
    $loaded = Get-AgentLoadedModel
    $freshlyLoaded = $false
    if ($null -eq $loaded) {
        Write-Host "Lade Qwen3-Coder 30B mit $ContextLength Tokens Kontext ..."
        & $lmsPath load $modelKey `
            --context-length $ContextLength `
            --parallel 1 `
            --ttl 3600 `
            --identifier $modelIdentifier `
            --yes
        if ($LASTEXITCODE -ne 0) {
            throw "Das Agent-1-Modell konnte nicht geladen werden."
        }
        # A successful command is insufficient: inspect the actual loaded state.
        $loaded = Get-AgentLoadedModel -OwnOnly
        $freshlyLoaded = $true
    }
    $script:activeModelIdentifier = Confirm-AgentLoadedModel -Loaded $loaded -OwnOnly:$freshlyLoaded
    if (-not $freshlyLoaded) {
        Write-Host "Agent-1-Modell ist bereits geladen: $script:activeModelIdentifier"
    }

    $serverStatus = & $lmsPath server status --json | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Der lokale Serverstatus konnte nicht gelesen werden."
    }

    if ($serverStatus -notmatch '"running"\s*:\s*true') {
        Write-Host "Starte lokalen Server nur auf diesem PC ..."
        & $lmsPath server start --port $serverPort --bind 127.0.0.1
        if ($LASTEXITCODE -ne 0) {
            throw "Der lokale Agent-1-Server konnte nicht gestartet werden."
        }
    }

    Write-Host "Forge ist bereit: Modell $script:activeModelIdentifier, lokaler Port $serverPort."
}

switch ($Action) {
    "Start" {
        Start-AgentRuntime
    }
    "Status" {
        Write-Host "Geladene Modelle:"
        & $lmsPath ps
        Write-Host "`nLokaler Server:"
        & $lmsPath server status
    }
    "Test" {
        Start-AgentRuntime
        $systemPrompt = "Du bist Agent 1. Antworte fuer diesen Selbsttest nur mit AGENT1_BEREIT und fuehre keine Werkzeuge oder Dateioperationen aus."
        $answer = & $lmsPath chat $script:activeModelIdentifier `
            --system-prompt $systemPrompt `
            --prompt "Fuehre den Selbsttest aus." `
            --ttl 3600 `
            --dont-fetch-catalog `
            --yes
        if ($LASTEXITCODE -ne 0 -or ($answer -join "`n") -notmatch "AGENT1_BEREIT") {
            throw "Der Agent-1-Selbsttest ist fehlgeschlagen."
        }
        Write-Host "Selbsttest bestanden: AGENT1_BEREIT"
    }
    "Run" {
        if ([string]::IsNullOrWhiteSpace($TaskFile)) {
            throw "Für Run muss -TaskFile angegeben werden."
        }
        if ([string]::IsNullOrWhiteSpace($Workspace)) {
            throw "Für Run muss -Workspace angegeben werden."
        }
        $resolvedTaskFile = (Resolve-Path -LiteralPath $TaskFile).Path
        $resolvedWorkspace = (Resolve-Path -LiteralPath $Workspace).Path
        Start-AgentRuntime
        Push-Location (Split-Path -Parent $PSScriptRoot)
        try {
            python -m src.forge_runtime.cli `
                --task-file $resolvedTaskFile `
                --workspace $resolvedWorkspace `
                --model $script:activeModelIdentifier
            if ($LASTEXITCODE -ne 0) {
                throw "Der Forge-Bauauftrag wurde nicht freigegeben (Exitcode $LASTEXITCODE)."
            }
        }
        finally {
            Pop-Location
        }
    }
    "Chat" {
        if ([string]::IsNullOrWhiteSpace($Workspace)) {
            throw "Fuer Chat muss -Workspace angegeben werden, etwa .\workspaces\mein-auftrag."
        }
        Start-AgentRuntime
        Push-Location (Split-Path -Parent $PSScriptRoot)
        try {
            python -m src.forge_runtime.conversation_cli `
                --workspace $Workspace `
                --model $script:activeModelIdentifier
            if ($LASTEXITCODE -ne 0) {
                throw "Forge-Gespraech beendet (Exitcode $LASTEXITCODE)."
            }
        }
        finally {
            Pop-Location
        }
    }
    "Stop" {
        $serverStatus = & $lmsPath server status --json | Out-String
        if ($serverStatus -match '"running"\s*:\s*true') {
            & $lmsPath server stop
        }
        $loadedIdentifier = Get-AgentModelIdentifier
        if (-not [string]::IsNullOrWhiteSpace($loadedIdentifier)) {
            & $lmsPath unload $loadedIdentifier
        }
        Write-Host "Agent-1-Laufzeit wurde beendet."
    }
}

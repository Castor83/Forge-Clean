param(
    [switch]$ExplicitPause,

    [Parameter(Mandatory = $true)]
    [string]$CurrentGoal,

    [Parameter(Mandatory = $true)]
    [string]$CompletedWork,

    [Parameter(Mandatory = $true)]
    [string]$ChangedFiles,

    [Parameter(Mandatory = $true)]
    [string]$Decisions,

    [Parameter(Mandatory = $true)]
    [string]$NextStep,

    [Parameter(Mandatory = $true)]
    [string]$Tests,

    [Parameter(Mandatory = $true)]
    [string]$Risks,

    [Parameter(Mandatory = $true)]
    [string]$Processes,

    [Parameter(Mandatory = $true)]
    [string]$ResumeNotes
)

$ErrorActionPreference = "Stop"

if (-not $ExplicitPause) {
    throw "PAUSE_STATE.md darf nur nach der ausdrücklichen Benutzeranweisung 'Pause' aktualisiert werden."
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$statePath = Join-Path $workspaceRoot "PAUSE_STATE.md"

Push-Location $workspaceRoot
try {
    $branch = (git branch --show-current).Trim()
    $commit = (git rev-parse --short HEAD).Trim()
    $statusLines = @(git status --short | Where-Object { $_ -notmatch "PAUSE_STATE\.md$" })

    if ($LASTEXITCODE -ne 0) {
        throw "Git-Status konnte nicht ermittelt werden."
    }

    $workingTree = if ($statusLines.Count -eq 0) {
        "sauber"
    }
    else {
        ($statusLines | ForEach-Object { "- ``$_``" }) -join "`n"
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
    $fields = @(
        "CurrentGoal",
        "CompletedWork",
        "ChangedFiles",
        "Decisions",
        "NextStep",
        "Tests",
        "Risks",
        "Processes",
        "ResumeNotes"
    )
    foreach ($field in $fields) {
        $value = (Get-Variable -Name $field -ValueOnly).Trim()
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Der Pausenstand ist unvollständig: $field darf nicht leer sein."
        }
        Set-Variable -Name $field -Value $value.Replace('`n', [Environment]::NewLine)
    }

    $content = @"
# Pausenstand – Agent 1

- Aktualisiert: $timestamp
- Branch: ``$branch``
- Commit: ``$commit``

## Aktuelles Ziel

$CurrentGoal

## Erledigte Arbeit

$CompletedWork

## Geänderte Dateien und Zweck

$ChangedFiles

## Entscheidungen und Annahmen

$Decisions

## Git-Arbeitsbaum

$workingTree

## Letzte Prüfungen

$Tests

<!-- PAUSE_RUNNING_PROCESSES -->
## Laufende Prozesse

$Processes

<!-- PAUSE_NEXT_STEP -->
## Nächster konkreter Schritt

$NextStep

## Hinweise zur unmittelbaren Wiederaufnahme

$ResumeNotes

## Offene Punkte und Risiken

$Risks

## Beim Fortsetzen verifizieren

1. ``git status --short --branch``
2. ``powershell -ExecutionPolicy Bypass -File .\scripts\verify.ps1``
3. Genannte Hintergrundprozesse und ihre Protokolle
4. Tatsächlichen Zustand mit diesem Protokoll abgleichen
"@

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($statePath, $content, $utf8WithoutBom)
    Write-Output "Pausenstand aktualisiert: $statePath"
}
finally {
    Pop-Location
}

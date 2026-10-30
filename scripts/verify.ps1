$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
Push-Location $workspaceRoot
try {
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Python-Tests sind fehlgeschlagen (Exitcode $LASTEXITCODE)."
    }

    git diff --check
    if ($LASTEXITCODE -ne 0) {
        throw "Git-Diff-Prüfung ist fehlgeschlagen (Exitcode $LASTEXITCODE)."
    }
}
finally {
    Pop-Location
}

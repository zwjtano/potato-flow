$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$PythonExecutable = if ($env:POTATO_PYTHON) { $env:POTATO_PYTHON } else { "python" }

Push-Location $RepositoryRoot
try {
    & $PythonExecutable -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Push-Location (Join-Path $RepositoryRoot "potatoflow-app")
    try {
        & $PythonExecutable -m unittest discover -s tests
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally {
        Pop-Location
    }
}
finally {
    Pop-Location
}

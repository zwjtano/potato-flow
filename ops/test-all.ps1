$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$RepositoryVirtualEnvPython = Join-Path $RepositoryRoot ".venv/Scripts/python.exe"
$AppVirtualEnvPython = Join-Path $RepositoryRoot "potatoflow-app/.venv/Scripts/python.exe"
$PythonExecutable = if ($env:POTATO_PYTHON) {
    $env:POTATO_PYTHON
} elseif (Test-Path -LiteralPath $RepositoryVirtualEnvPython) {
    $RepositoryVirtualEnvPython
} elseif (Test-Path -LiteralPath $AppVirtualEnvPython) {
    $AppVirtualEnvPython
} else {
    "python"
}

$Utf8Encoding = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = $Utf8Encoding
$OutputEncoding = $Utf8Encoding
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Push-Location $RepositoryRoot
try {
    $PythonVersion = & $PythonExecutable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($PythonVersion.Trim() -ne "3.11") {
        throw "PotatoFlow tests require Python 3.11; got $($PythonVersion.Trim()) from $PythonExecutable. Set POTATO_PYTHON to a Python 3.11 executable."
    }

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

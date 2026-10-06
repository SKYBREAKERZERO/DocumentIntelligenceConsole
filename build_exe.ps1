param(
    [switch]$Bootstrap,
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Use project-local temporary directories. Some managed Windows environments
# leave %LOCALAPPDATA%\Temp\pytest-of-<user> with ACLs that pytest cannot
# enumerate, which causes WinError 5 before a test body even starts. Keeping
# build/test temp state under the project makes the EXE build reproducible and
# independent of the user's global Temp ACL/history.
$BuildTempRoot = Join-Path $PSScriptRoot '.build-temp'
$PytestBaseTemp = Join-Path $BuildTempRoot 'pytest'
New-Item -ItemType Directory -Force -Path $BuildTempRoot | Out-Null
$env:TEMP = $BuildTempRoot
$env:TMP = $BuildTempRoot

if ($env:OS -ne 'Windows_NT') {
    throw 'Windows EXE must be built on Windows. Run build_exe.cmd on a Windows 10/11 x64 machine.'
}

$Version = '1.03'
$AppName = 'DocumentIntelligenceConsole'
$BuildVenv = Join-Path $PSScriptRoot '.venv-build'
$ExistingVenv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$BuildPython = Join-Path $BuildVenv 'Scripts\python.exe'

function Resolve-Python {
    if (Test-Path $ExistingVenv) {
        return $ExistingVenv
    }
    if (Test-Path $BuildPython) {
        return $BuildPython
    }
    $candidates = @(
        @{Exe='py'; Args=@('-3.12')},
        @{Exe='py'; Args=@('-3.13')},
        @{Exe='python'; Args=@()}
    )
    foreach ($candidate in $candidates) {
        try {
            $cmd = Get-Command $candidate.Exe -ErrorAction Stop
            if ($candidate.Exe -eq 'py') {
                & $cmd.Source @($candidate.Args) -c "import sys; assert sys.version_info >= (3,12); print(sys.executable)" *> $null
            } else {
                & $cmd.Source -c "import sys; assert sys.version_info >= (3,12); print(sys.executable)" *> $null
            }
            if ($LASTEXITCODE -eq 0) {
                return @{Exe=$cmd.Source; Args=$candidate.Args}
            }
        } catch {}
    }
    throw 'Python 3.12+ was not found. Install 64-bit Python 3.12 or 3.13 and rerun build_exe.cmd.'
}

if ($Clean) {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

$resolved = Resolve-Python
if ($resolved -is [string]) {
    $python = $resolved
} else {
    if (-not (Test-Path $BuildPython)) {
        Write-Host '[bootstrap] Creating isolated Windows build environment...' -ForegroundColor Cyan
        & $resolved.Exe @($resolved.Args) -m venv $BuildVenv
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create .venv-build.' }
    }
    $python = $BuildPython
}

if ($Bootstrap -or -not (Test-Path $ExistingVenv)) {
    Write-Host '[bootstrap] Installing executable build/runtime dependencies...' -ForegroundColor Cyan
    & $python -m pip install --upgrade pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { throw 'pip bootstrap failed.' }
    & $python -m pip install -r requirements-exe.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}

Write-Host '[verify] Python source compilation...' -ForegroundColor Cyan
& $python -m compileall -q app scripts
if ($LASTEXITCODE -ne 0) { throw 'Source compilation failed.' }

$requiredModules = @(
    'ttkbootstrap', 'psycopg', 'qdrant_client', 'fitz', 'docx', 'pptx',
    'openpyxl', 'rapidocr', 'onnxruntime', 'sentence_transformers', 'torch',
    'transformers', 'PyInstaller'
)
foreach ($module in $requiredModules) {
    & $python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$module') else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Required build module is missing: $module. Run build_exe.cmd again (it bootstraps dependencies automatically)."
    }
}

if (-not $SkipTests) {
    # pytest is a build-time verification dependency rather than an application
    # runtime dependency. Older v1.03 build environments may already exist from
    # before pytest was added to requirements-exe.txt, so self-heal that case.
    & $python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pytest') else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[bootstrap] pytest is missing; installing focused-test dependency...' -ForegroundColor Cyan
        & $python -m pip install 'pytest==9.1.1'
        if ($LASTEXITCODE -ne 0) { throw 'Unable to install pytest required by the EXE verification step.' }
    }

    Write-Host '[verify] Running focused executable/runtime regression tests...' -ForegroundColor Cyan
    $tests = @(
        'tests/test_v103_health_policy.py',
        'tests/test_v101_primary_document_priority.py',
        'tests/test_v596_ai_relation_version_integration.py',
        'tests/test_v590_enterprise_qabug_rag.py'
    ) | Where-Object { Test-Path $_ }
    if ($tests.Count -gt 0) {
        # Never use pytest's default %LOCALAPPDATA%\Temp\pytest-of-<user>
        # directory. A stale/locked ACL there can raise WinError 5 during the
        # tmp_path fixture setup even when every application test is healthy.
        Remove-Item -Recurse -Force $PytestBaseTemp -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $PytestBaseTemp | Out-Null
        Write-Host "[verify] pytest temp: $PytestBaseTemp" -ForegroundColor DarkGray
        & $python -m pytest -q "--basetemp=$PytestBaseTemp" @tests
        if ($LASTEXITCODE -ne 0) { throw 'Focused regression tests failed.' }
    } else {
        Write-Host '[verify] No focused regression test files found; continuing with source/module verification.' -ForegroundColor Yellow
    }
}

Write-Host '[build] Building Windows x64 OneFile executable...' -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm document_intelligence_console.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

$exe = Join-Path $PSScriptRoot "dist\$AppName.exe"
if (-not (Test-Path $exe)) { throw "Expected executable not found: $exe" }

$releaseDir = Join-Path $PSScriptRoot "release\v$Version"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
Copy-Item $exe (Join-Path $releaseDir "$AppName.exe") -Force
if (Test-Path 'docs\DocumentIntelligenceConsole_v1.03_项目式样书.docx') {
    Copy-Item 'docs\DocumentIntelligenceConsole_v1.03_项目式样书.docx' $releaseDir -Force
}
if (Test-Path 'docs\DocumentIntelligenceConsole_v1.03_使用说明书.docx') {
    Copy-Item 'docs\DocumentIntelligenceConsole_v1.03_使用说明书.docx' $releaseDir -Force
}

$hash = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant()
$hash | Set-Content -Encoding ascii (Join-Path $releaseDir 'SHA256.txt')

Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host "BUILD SUCCESS: $exe" -ForegroundColor Green
Write-Host "Release:       $releaseDir" -ForegroundColor Green
Write-Host "SHA-256:       $hash" -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Green

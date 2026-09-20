# Data Studio launcher (PowerShell)
# UTF-8 with BOM on purpose: Windows PowerShell 5.1 reads a BOM-less .ps1
# using the ANSI code page, which would mangle the Chinese text below.
param([int]$Port = 8848)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Test-Deps([string]$exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    & $exe -c "import fastapi, pandas, uvicorn" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$py = $null
$candidates = @(
    (Join-Path $root ".venv\Scripts\python.exe"),
)
foreach ($c in $candidates) { if (Test-Deps $c) { $py = $c; break } }

if (-not $py) {
    $found = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found -and (Test-Deps $found.Source)) { $py = $found.Source }
}

if (-not $py) {
    Write-Host ""
    Write-Host "  Python with the required packages was not found." -ForegroundColor Red
    Write-Host "  Run install.bat first, or:  pip install -r requirements.txt" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "  Data Studio  ->  http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "  Python       ->  $py" -ForegroundColor DarkGray
Write-Host "  Ctrl+C to stop" -ForegroundColor DarkGray
Write-Host ""

Start-Process "http://127.0.0.1:$Port"
& $py app.py --port $Port

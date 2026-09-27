<#
  ISHA Setup bootstrapper (Windows).
  1. Finds a compatible 64-bit Python 3.10-3.13 (never the Microsoft Store stub).
  2. If none: asks, then installs Python 3.12 for THIS USER via winget, or downloads the
     official python.org installer and runs it ONLY if its Authenticode signature is valid
     and issued to the Python Software Foundation.
  3. Creates a small setup environment with PyQt5 and starts the graphical ISHA installer.
  Nothing here changes system security settings; "-ExecutionPolicy Bypass" in
  ISHA_Setup.cmd applies to this one process only.
#>
param([switch]$Headless, [string]$OfflineDir = "")
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cfg = Get-Content (Join-Path $Here "installer\installer_config.json") -Raw | ConvertFrom-Json
$SetupEnv = Join-Path $env:LOCALAPPDATA "ISHA-Setup\env"
if (-not $OfflineDir -and (Test-Path (Join-Path $Here "offline"))) { $OfflineDir = Join-Path $Here "offline" }

function Write-Step($m) { Write-Host "`n>> $m" -ForegroundColor Cyan }

function Test-Python($exe, $argsPrefix) {
    try {
        $code = "import sys,json;ok=1`ntry:`n import venv,ensurepip`nexcept Exception: ok=0`nprint(json.dumps([sys.version_info[0],sys.version_info[1],sys.version_info[2],sys.maxsize>2**32,ok,sys.executable]))"
        $out = & $exe @argsPrefix -c $code 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $v = $out | ConvertFrom-Json
        if ($v[0] -eq 3 -and $v[1] -ge 10 -and $v[1] -le 13 -and $v[3] -and $v[4]) { return $v[5] }
    } catch { }
    return $null
}

function Find-Python {
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "3.12", "3.11", "3.13", "3.10") { $cands += ,@("py", @("-$v")) }
    }
    foreach ($n in "python", "python3") {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notlike "*WindowsApps*") { $cands += ,@($c.Source, @()) }
    }
    $local = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $local) {
        Get-ChildItem $local -Directory -Filter "Python3*" | Sort-Object Name -Descending | ForEach-Object {
            $exe = Join-Path $_.FullName "python.exe"; if (Test-Path $exe) { $cands += ,@($exe, @()) } }
    }
    foreach ($c in $cands) { $p = Test-Python $c[0] $c[1]; if ($p) { return $p } }
    return $null
}

function Install-Python {
    $offlineInstaller = if ($OfflineDir) { Get-ChildItem (Join-Path $OfflineDir "python") -Filter "python-3*-amd64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 } else { $null }
    if (-not $offlineInstaller -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Step "Installing Python $($Cfg.python.recommended) for this user with winget (official source)"
        winget install -e --id $Cfg.python.windows_winget_id --scope user --silent --accept-package-agreements --accept-source-agreements
        return
    }
    if ($offlineInstaller) { $file = $offlineInstaller.FullName }
    else {
        $url = $Cfg.python.windows_installer_url
        if ($url -notlike "https://www.python.org/*") { throw "Refusing non-python.org URL: $url" }
        $file = Join-Path $env:TEMP ([IO.Path]::GetFileName($url))
        Write-Step "Downloading $url"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
    }
    $sig = Get-AuthenticodeSignature -LiteralPath $file
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*$($Cfg.python.authenticode_signer)*") {
        Remove-Item $file -ErrorAction SilentlyContinue
        throw "Python installer signature is not valid ($($sig.Status)). It was NOT run."
    }
    Write-Step "Running the official Python installer (per-user, no admin)"
    $p = Start-Process -FilePath $file -ArgumentList "/quiet InstallAllUsers=0 Include_launcher=1 Include_pip=1 PrependPath=0 Include_test=0" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Python installer exited with code $($p.ExitCode)" }
}

Write-Host "ISHA $($Cfg.app_version) Setup" -ForegroundColor Cyan
$py = Find-Python
if (-not $py) {
    Write-Host "Python not found. ISHA requires Python $($Cfg.python.min) - $($Cfg.python.max_tested) (64-bit)." -ForegroundColor Yellow
    Add-Type -AssemblyName System.Windows.Forms
    $ans = [System.Windows.Forms.MessageBox]::Show("ISHA needs Python $($Cfg.python.recommended). Install it now for your user account (official python.org build)?", "ISHA Setup", "YesNo", "Question")
    if ($ans -ne "Yes") { Write-Host "Setup cancelled."; exit 1 }
    Install-Python
    $py = Find-Python
    if (-not $py) { throw "Python was installed but could not be found. Restart Setup (a new PATH may be needed)." }
}
Write-Host "Python detected: $py"

Write-Step "Preparing the setup environment"
if (-not (Test-Path (Join-Path $SetupEnv "Scripts\python.exe"))) { & $py -m venv $SetupEnv }
$envPy = Join-Path $SetupEnv "Scripts\python.exe"
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--prefer-binary", "PyQt5", "psutil")
if ($OfflineDir -and (Test-Path (Join-Path $OfflineDir "wheels"))) { $pipArgs += @("--find-links", (Join-Path $OfflineDir "wheels")) }
& $envPy @pipArgs
if ($LASTEXITCODE -ne 0) { Write-Host "Could not install the setup UI; continuing in text mode." -ForegroundColor Yellow; $Headless = $true }

Set-Location $Here
$instArgs = @("-m", "installer")
if ($OfflineDir) { $instArgs += @("--offline-dir", $OfflineDir) }
if ($Headless) { & $envPy @instArgs --headless } else { & (Join-Path $SetupEnv "Scripts\pythonw.exe") @instArgs }

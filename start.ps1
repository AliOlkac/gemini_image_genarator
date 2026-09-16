# =============================================================================
# start.ps1 — Gemini Image Generator başlatma scripti (PowerShell)
# =============================================================================
# Kullanım:
#   1) PowerShell'i proje klasöründe aç
#   2) .\start.ps1
#
# Script şunları yapar:
#   - Sanal ortam (.venv) yoksa oluşturur
#   - Bağımlılıkları requirements.txt'ten yükler
#   - .env dosyasının varlığını kontrol eder
#   - Streamlit arayüzünü başlatır (http://localhost:8501)
# =============================================================================

# Hata olunca scripti durdur (sessiz hata yerine net mesaj)
$ErrorActionPreference = "Stop"

# Bu script hangi klasördeyse proje kökü odur (göreli yollar bozulmasın)
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Gemini Image Generator - Baslatiliyor" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# 1) Python kontrolü — sistemde Python yüklü mü?
# ---------------------------------------------------------------------------
$pythonCmd = $null

# Önce 'python' dene; Windows'ta bazen sadece 'py' launcher vardır
foreach ($candidate in @("python", "py")) {
    try {
        $version = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -or $version -match "Python") {
            $pythonCmd = $candidate
            Write-Host "[OK] Python bulundu: $version" -ForegroundColor Green
            break
        }
    }
    catch {
        # Bu komut yoksa bir sonrakini dene
        continue
    }
}

if (-not $pythonCmd) {
    Write-Host "[HATA] Python bulunamadi." -ForegroundColor Red
    Write-Host "       https://www.python.org/downloads/ adresinden Python 3.10+ kur." -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------------------
# 2) Sanal ortam (.venv) — yoksa oluştur
# ---------------------------------------------------------------------------
$venvPath = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPip = Join-Path $venvPath "Scripts\pip.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[...] Sanal ortam (.venv) olusturuluyor..." -ForegroundColor Yellow
    & $pythonCmd -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[HATA] venv olusturulamadi." -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Sanal ortam hazir." -ForegroundColor Green
}
else {
    Write-Host "[OK] Sanal ortam zaten mevcut." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 3) Bağımlılıklar — pip güncelle + requirements.txt kur
# ---------------------------------------------------------------------------
Write-Host "[...] Bagimliliklar kontrol ediliyor..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip --quiet
& $venvPip install -r (Join-Path $ProjectRoot "requirements.txt") --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Host "[HATA] pip install basarisiz. Asagidaki komutu elle calistir:" -ForegroundColor Red
    Write-Host "       .\.venv\Scripts\pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}
Write-Host "[OK] Bagimliliklar yuklu." -ForegroundColor Green

# ---------------------------------------------------------------------------
# 4) .env kontrolü — API anahtarı olmadan uygulama çalışmaz
# ---------------------------------------------------------------------------
$envFile = Join-Path $ProjectRoot ".env"
$hasKey = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*GEMINI_API_KEY\s*=\s*\S+' -Quiet)
if (-not $hasKey) {
    Write-Host ""
    Write-Host "[UYARI] Kayitli API anahtari yok." -ForegroundColor Yellow
    Write-Host "        Uygulama acilinca sol menudeki 'API Anahtari' alanina yapistirip Kaydet'e bas." -ForegroundColor Yellow
    Write-Host "        Anahtar: https://aistudio.google.com/apikey" -ForegroundColor Yellow
    Write-Host ""
}
else {
    Write-Host "[OK] API anahtari .env dosyasinda kayitli." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 5) outputs klasörü — üretilen görseller buraya yazılır
# ---------------------------------------------------------------------------
$outputsDir = Join-Path $ProjectRoot "outputs"
if (-not (Test-Path $outputsDir)) {
    New-Item -ItemType Directory -Path $outputsDir | Out-Null
    Write-Host "[OK] outputs/ klasoru olusturuldu." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 6) Streamlit'i başlat
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Streamlit aciliyor -> http://localhost:8501" -ForegroundColor Cyan
Write-Host "Durdurmak icin: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

$streamlitExe = Join-Path $venvPath "Scripts\streamlit.exe"
& $streamlitExe run (Join-Path $ProjectRoot "main.py")

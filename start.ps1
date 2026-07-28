# start.ps1 — поднимает туннель cloudflared, вписывает адрес в .env и запускает бота.
# Запуск (из папки проекта):   powershell -ExecutionPolicy Bypass -File .\start.ps1
# Остановка: Ctrl+C (туннель закроется автоматически).

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$port = 8080

# 1. Находим cloudflared (в PATH или по стандартному пути установки).
$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) { $cf = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
if (-not (Test-Path $cf)) {
    Write-Error "cloudflared не найден. Установи его: winget install --id Cloudflare.cloudflared -e"
    exit 1
}

# 2. Запускаем туннель в фоне, логи — во временные файлы.
$out = Join-Path $env:TEMP "cf_out.log"
$err = Join-Path $env:TEMP "cf_err.log"
Remove-Item $out, $err -ErrorAction SilentlyContinue

Write-Host "-> Запускаю туннель cloudflared..." -ForegroundColor Cyan
$tunnel = Start-Process -FilePath $cf `
    -ArgumentList "tunnel", "--url", "http://localhost:$port" `
    -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $out -RedirectStandardError $err

# 3. Ждём, пока в логах появится публичный https-адрес.
$url = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    $m = Select-String -Path $out, $err -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $url = $m.Matches[0].Value; break }
}
if (-not $url) {
    Write-Error "Не удалось получить адрес туннеля. Смотри лог: $err"
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "-> Туннель готов: $url" -ForegroundColor Green

# 4. Прописываем WEBAPP_URL в .env (создаём строку, если её нет).
$envFile = Join-Path $PSScriptRoot ".env"
$lines = Get-Content $envFile
if ($lines -match '^\s*WEBAPP_URL=') {
    $lines = $lines -replace '^\s*WEBAPP_URL=.*', "WEBAPP_URL=$url"
} else {
    $lines += "WEBAPP_URL=$url"
}
Set-Content -Path $envFile -Value $lines -Encoding UTF8
Write-Host "-> WEBAPP_URL записан в .env" -ForegroundColor Green

# 5. Запускаем бота; при выходе гасим туннель.
Write-Host "-> Запускаю бота. Нажми Ctrl+C для остановки." -ForegroundColor Cyan
try {
    python bot.py
} finally {
    Write-Host "`n-> Останавливаю туннель..." -ForegroundColor Yellow
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
}

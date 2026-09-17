@echo off
REM =============================================================================
REM start.bat — Çift tıklama ile uygulamayı başlat (CMD / Explorer)
REM =============================================================================
REM PowerShell scriptini çağırır. ExecutionPolicy hatası alırsan README'deki
REM "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" adımını uygula.
REM =============================================================================

cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause

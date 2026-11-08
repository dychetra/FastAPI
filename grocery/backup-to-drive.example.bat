@echo off
REM Copy this file to backup-to-drive.bat and fill in the 3 values below.
REM (backup-to-drive.bat is not uploaded to GitHub, so your password stays on this PC.)
cd /d "%~dp0"
python backup.py ^
  --url "https://YOUR-APP.onrender.com" ^
  --password "YOUR_APP_PASSWORD" ^
  --folder "G:\My Drive\Grocery backups" >> backup-log.txt 2>&1

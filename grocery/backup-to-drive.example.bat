@echo off
REM Copy this file to backup-to-drive.bat and fill in the 3 values below.
REM (backup-to-drive.bat is not uploaded to GitHub, so your password stays on this PC.)
cd /d "%~dp0"
python backup.py ^
  --url "https://scan.gechheng.shop" ^
  --password "YOUR_APP_PASSWORD" ^
  --folder "%USERPROFILE%\OneDrive\ScanPrice backups" >> backup-log.txt 2>&1

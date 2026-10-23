@echo off
rem Run Luma Picker from source (needs Python 3.10+ from python.org).
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Checking packages (the first run takes a minute)...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Installing the packages failed - see the messages above.
  pause
  exit /b 1
)
start "" pythonw src\luma_picker.py --replace

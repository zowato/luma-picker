@echo off
rem Builds Luma Picker as a Windows app in "dist\Luma Picker\".
cd /d "%~dp0"

echo Installing build tools (the first time takes a few minutes)...
python -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 goto fail

echo Running tests...
python -m pytest -q tests
if errorlevel 1 goto fail

echo Building...
python -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name "Luma Picker" --icon assets\icon.ico ^
  --distpath dist --workpath build src\luma_picker.py
if errorlevel 1 goto fail

copy /y LICENSE "dist\Luma Picker\" >nul
copy /y THIRD_PARTY_NOTICES.md "dist\Luma Picker\" >nul
copy /y README.md "dist\Luma Picker\" >nul

echo.
echo Done: dist\Luma Picker\Luma Picker.exe
choice /m "Create a desktop shortcut"
if errorlevel 2 goto end
powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); $exe=(Resolve-Path 'dist\Luma Picker\Luma Picker.exe').Path; $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Luma Picker.lnk')); $s.TargetPath=$exe; $s.WorkingDirectory=(Split-Path $exe); $s.IconLocation=$exe + ',0'; $s.Save()"
echo Shortcut created.
goto end

:fail
echo.
echo Something went wrong - see the messages above.

:end
pause

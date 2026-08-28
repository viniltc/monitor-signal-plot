@echo off
REM ===================================================================
REM  Builds Monitoring Waveform Acceptance Check into a single .exe
REM
REM  Run this from the project folder, with the virtual environment
REM  active - the prompt should show (.venv).
REM
REM  Output: dist\MonitoringWaveformAcceptanceCheck.exe
REM ===================================================================

echo.
echo Building Monitoring Waveform Acceptance Check...
echo.

REM --- Check we are in a virtual environment -------------------------
if "%VIRTUAL_ENV%"=="" (
    echo WARNING: no virtual environment detected.
    echo Activate it first with:  .venv\Scripts\activate
    echo.
    pause
)

REM --- Make sure the build tool and dependencies are present ---------
python -m pip install --upgrade pyinstaller pandas matplotlib
if errorlevel 1 goto failed

REM --- Remove previous build output ----------------------------------
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM --- Build ---------------------------------------------------------
REM  --onefile    single .exe, nothing to install (slower first start)
REM  --windowed   no console window behind the application
REM  --exclude    packages pulled in as optional dependencies that this
REM               application never uses; excluding them roughly halves
REM               the size of the executable
python -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name MonitoringWaveformAcceptanceCheck ^
  --version-file version_info.txt ^
  --exclude-module scipy ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module IPython ^
  --exclude-module jupyter ^
  --exclude-module notebook ^
  --exclude-module pytest ^
  --exclude-module sphinx ^
  --exclude-module wx ^
  signal_viewer.py

if errorlevel 1 (
    echo.
    echo Build with version metadata failed - retrying without it.
    echo The executable will work, but will have no version details
    echo under right-click ^> Properties.
    echo.
    python -m PyInstaller ^
      --onefile ^
      --windowed ^
      --name MonitoringWaveformAcceptanceCheck ^
      --exclude-module scipy ^
      --exclude-module PyQt5 ^
      --exclude-module PyQt6 ^
      --exclude-module PySide2 ^
      --exclude-module PySide6 ^
      --exclude-module IPython ^
      --exclude-module jupyter ^
      --exclude-module notebook ^
      --exclude-module pytest ^
      --exclude-module sphinx ^
      --exclude-module wx ^
      signal_viewer.py
    if errorlevel 1 goto failed
)
echo.
echo ===================================================================
echo  Build complete.
echo.
echo  Executable: dist\MonitoringWaveformAcceptanceCheck.exe
echo.
echo  Record the SHA-256 hash below against the software version, so a
echo  copy of the executable can later be shown to be the tested one:
echo ===================================================================
certutil -hashfile dist\MonitoringWaveformAcceptanceCheck.exe SHA256
echo.
pause
goto :eof

:failed
echo.
echo BUILD FAILED - see the messages above.
echo.
pause

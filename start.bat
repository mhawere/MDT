@echo off
REM start.bat — One-command launcher for Multi-Device Tester (Windows)

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "VENV=%SCRIPT_DIR%.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "PY_BOOTSTRAP="

REM ── Pick a Python launcher available on Windows ─────────────────────────
where py >nul 2>&1
if not errorlevel 1 (
    set "PY_BOOTSTRAP=py -3"
) else (
    where python >nul 2>&1
    if not errorlevel 1 set "PY_BOOTSTRAP=python"
)

if not defined PY_BOOTSTRAP (
    echo [MDT] ERROR: Python not found. Install Python 3.11+ and ensure py or python is on PATH.
    pause
    exit /b 1
)

if exist "%VENV%" if not exist "%PYTHON%" (
    rmdir /s /q "%VENV%"
)

REM ── Create venv if missing ────────────────────────────────────────────────
if not exist "%PYTHON%" (
    echo [MDT] Creating Python virtual environment...
    %PY_BOOTSTRAP% -m venv "%VENV%"
    if errorlevel 1 (
        echo [MDT] ERROR: Failed to create venv. Is Python 3.11+ installed?
        pause
        exit /b 1
    )
    echo [MDT] Virtual environment created.
)

REM ── Install dependencies ──────────────────────────────────────────────────
echo [MDT] Installing Python dependencies...
"%PIP%" install --quiet --upgrade pip
"%PIP%" install --quiet -r "%SCRIPT_DIR%requirements.txt"
echo [MDT] Dependencies ready.

REM ── Run ──────────────────────────────────────────────────────────────────
"%PYTHON%" "%SCRIPT_DIR%run.py"
pause

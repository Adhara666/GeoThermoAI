@echo off
title GeoThermoAI

echo [GeoThermoAI] Starting...
echo [DEBUG] Step 1: Looking for conda...

:: Try to find conda from PATH
where conda >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [DEBUG] Conda found in PATH
    goto :activate
)

:: Try common installation paths
echo [DEBUG] Step 2: Conda not in PATH, checking common directories...
set CONDA_DIRS=C:\ProgramData\anaconda3 C:\ProgramData\miniconda3 C:\Users\%USERNAME%\anaconda3 C:\Users\%USERNAME%\miniconda3 D:\Apps\Anaconda3 D:\Apps\Miniconda3
for %%d in (%CONDA_DIRS%) do (
    if exist "%%d\Scripts\conda.exe" (
        echo [DEBUG] Found conda at: %%d
        set CONDA_BASE=%%d
        goto :activate
    )
)

echo [GeoThermoAI] Conda not found. Please install Miniconda first:
echo [GeoThermoAI] https://docs.conda.io/en/latest/miniconda.html
pause
exit /b 1

:activate
if not defined CONDA_BASE (
    echo [DEBUG] Step 3: Determining conda base path...
    for /f "tokens=*" %%i in ('where conda') do (
        set CONDA_BASE=%%~dpi..
    )
)
echo [DEBUG] CONDA_BASE = %CONDA_BASE%

:: Activate environment
echo [DEBUG] Step 4: Activating environment...
call "%CONDA_BASE%\Scripts\activate.bat" GeoThermoAI
if %ERRORLEVEL% NEQ 0 (
    echo [DEBUG] Step 5: Environment not found, creating...
    "%CONDA_BASE%\Scripts\conda.exe" env create -f "%~dp0environment.yml"
    if %ERRORLEVEL% NEQ 0 (
        echo [GeoThermoAI] Failed to create environment.
        echo [GeoThermoAI] Please run: conda env create -f environment.yml
        pause
        exit /b 1
    )
    echo [GeoThermoAI] Environment created!
    call "%CONDA_BASE%\Scripts\activate.bat" GeoThermoAI
)

echo [GeoThermoAI] Environment ready, launching application...
cd /d "%~dp0"
python main.py

if %ERRORLEVEL% NEQ 0 (
    echo [GeoThermoAI] Program exited with error code: %ERRORLEVEL%
    pause
)

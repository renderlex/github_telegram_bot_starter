@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo Created .env from .env.example.
    )
    echo ERROR: .env file was not found or is not configured yet.
    echo Fill in .env and run again.
    pause
    exit /b 1
)

set "BOOTSTRAP_PYTHON=py -3"
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 (
    set "BOOTSTRAP_PYTHON=python"
    python -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Python 3.12+ was not found.
        echo Install Python and run this file again.
        pause
        exit /b 1
    )
)

%BOOTSTRAP_PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.12+ is required for this project.
    echo Install Python 3.12 or newer and run this file again.
    pause
    exit /b 1
)

call :ensure_venv
if errorlevel 1 exit /b %ERRORLEVEL%

".venv\Scripts\python.exe" -c "import telegram_news_bot" >nul 2>&1
if errorlevel 1 (
    echo Installing project dependencies into .venv...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 (
        echo ERROR: Failed to upgrade pip inside .venv.
        pause
        exit /b 1
    )

    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 (
        echo ERROR: Failed to install the project into .venv.
        pause
        exit /b 1
    )
)

set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

echo Starting Telegram News Bot...
".venv\Scripts\python.exe" -m telegram_news_bot.main serve
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Bot exited with code %EXIT_CODE%.
    echo Check logs\telegram_news_bot.log
    pause
)

exit /b %EXIT_CODE%

:ensure_venv
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
    if not errorlevel 1 goto :eof

    echo Existing .venv is not valid on this PC. Rebuilding it...
    rmdir /s /q ".venv"
)

echo Creating local virtual environment...
%BOOTSTRAP_PYTHON% -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create .venv.
    pause
    exit /b 1
)

goto :eof

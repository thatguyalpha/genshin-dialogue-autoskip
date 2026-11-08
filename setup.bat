@echo off
pushd "%~dp0"
color 0E

echo ============================================
echo Genshin Dialogue Autoskip - Setup
echo ============================================
echo.

:: ============================================
:: Python Installation Check
:: ============================================
echo Checking for Python...
python --version >nul 2>nul
if %errorlevel% neq 0 (
    color 0C
    echo.
    echo [ERROR] Python is not installed or not in PATH!
    echo.
    echo Please install Python 3.11+ from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    goto end
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
color 0A
echo [OK] Found %PYTHON_VERSION%
echo.

:: ============================================
:: Install Requirements via Pip
:: ============================================
color 0E
echo Installing dependencies from requirements.txt...
echo.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
set INSTALL_EXIT_CODE=%errorlevel%

echo.
if %INSTALL_EXIT_CODE% equ 0 (
    color 0A
    echo ============================================
    echo [SUCCESS] Dependencies installed successfully!
    echo You can now run the tool using run.bat
    echo ============================================
) else (
    color 0C
    echo ============================================
    echo [ERROR] Failed to install dependencies (exit code: %INSTALL_EXIT_CODE%).
    echo Please review the errors above.
    echo ============================================
)

:end
color 0F
echo.
pause

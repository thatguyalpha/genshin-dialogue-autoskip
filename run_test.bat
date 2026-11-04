@echo off
pushd "%~dp0"

:: ============================================
:: Admin Privileges Check
:: ============================================
echo Checking admin privileges...
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Elevating privileges to Administrator...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
color 0A
echo [OK] Running with Administrator privileges
echo.

:: ============================================
:: Python Installation Check
:: ============================================
color 0E
echo Checking for Python...
python --version >nul 2>nul
if %errorlevel% neq 0 (
    color 0C
    echo.
    echo [ERROR] Python is not installed or not in PATH!
    echo.
    echo Please install Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    goto end
)

:: Display Python version
for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
color 0A
echo [OK] Found %PYTHON_VERSION%
echo.

:: ============================================
:: UV Check and Installation
:: ============================================
color 0E
echo Checking for uv...

set "UV_CMD="

:: 1. Check if uv is already in PATH
where uv >nul 2>nul
if %errorlevel% equ 0 (
    set "UV_CMD=uv"
    goto uv_ready
)

:: 2. Check Python Scripts directories
call :add_to_path
if defined UV_CMD goto uv_ready

:: 3. Check python -m uv
python -m uv --version >nul 2>nul
if %errorlevel% equ 0 (
    set "UV_CMD=python -m uv"
    goto uv_ready
)

color 0E
echo uv not found. Installing uv...
echo.

:: Try python -m pip first
python -m pip --version >nul 2>nul
if %errorlevel% equ 0 (
    echo Installing uv with python -m pip...
    python -m pip install uv
    if %errorlevel% equ 0 (
        color 0A
        echo [OK] uv installed successfully!
        echo.
        call :add_to_path
        if defined UV_CMD goto uv_ready
        python -m uv --version >nul 2>nul
        if %errorlevel% equ 0 (
            set "UV_CMD=python -m uv"
            goto uv_ready
        )
    )
)

:: Try pip if python -m pip failed
pip --version >nul 2>nul
if %errorlevel% equ 0 (
    echo Installing uv with pip...
    pip install uv
    if %errorlevel% equ 0 (
        color 0A
        echo [OK] uv installed successfully!
        echo.
        call :add_to_path
        if defined UV_CMD goto uv_ready
        python -m uv --version >nul 2>nul
        if %errorlevel% equ 0 (
            set "UV_CMD=python -m uv"
            goto uv_ready
        )
    )
)

:: Try pip3 if pip failed
pip3 --version >nul 2>nul
if %errorlevel% equ 0 (
    echo Installing uv with pip3...
    pip3 install uv
    if %errorlevel% equ 0 (
        color 0A
        echo [OK] uv installed successfully!
        echo.
        call :add_to_path
        if defined UV_CMD goto uv_ready
        python -m uv --version >nul 2>nul
        if %errorlevel% equ 0 (
            set "UV_CMD=python -m uv"
            goto uv_ready
        )
    )
)

:install_failed
color 0C
echo.
echo [ERROR] Could not install or locate uv.
echo.
echo Possible reasons:
echo   - pip/pip3 is not available
echo   - Network connection issues
echo   - Insufficient permissions
echo.
echo Please try manually installing uv:
echo   pip install uv
echo.
goto end

:uv_ready
for /f "tokens=*" %%i in ('%UV_CMD% --version 2^>^&1') do set UV_VERSION=%%i
color 0A
echo [OK] Found %UV_VERSION%
echo.

:: ============================================
:: Run the Script
:: ============================================
:run_script
mode con: cols=54 lines=22
color 0F

if not defined UV_CMD set "UV_CMD=uv"

%UV_CMD% run test_detection.py
set SCRIPT_EXIT_CODE=%errorlevel%

echo.
if %SCRIPT_EXIT_CODE% equ 0 (
    color 0A
    echo ============================================
    echo [SUCCESS] Test utility finished.
    echo ============================================
    echo.
    goto end
) else (
    color 0C
    echo ============================================
    echo [ERROR] Test utility exited with error code: %SCRIPT_EXIT_CODE%
    echo ============================================
    echo.
    goto retry_prompt
)

:: ============================================
:: Retry Mechanism
:: ============================================
:retry_prompt
color 0E
set /p RETRY="Would you like to retry? (Y/N): "
if /i "%RETRY%"=="Y" goto run_script
if /i "%RETRY%"=="YES" goto run_script
echo.
echo Exiting...
goto end

:add_to_path
:: Check sys.prefix/Scripts
for /f "tokens=*" %%i in ('python -c "import sys, os; p=os.path.join(sys.prefix, 'Scripts'); print(p if os.path.isfile(os.path.join(p, 'uv.exe')) else '')" 2^>nul') do set "SYS_SCRIPTS=%%i"
if exist "%SYS_SCRIPTS%\uv.exe" (
    set "PATH=%SYS_SCRIPTS%;%PATH%"
    set "UV_CMD=uv"
    goto :eof
)

:: Check USER_BASE/Scripts
for /f "tokens=*" %%i in ('python -c "import site, os; p=os.path.join(site.USER_BASE, 'Scripts'); print(p if os.path.isfile(os.path.join(p, 'uv.exe')) else '')" 2^>nul') do set "USER_SCRIPTS=%%i"
if exist "%USER_SCRIPTS%\uv.exe" (
    set "PATH=%USER_SCRIPTS%;%PATH%"
    set "UV_CMD=uv"
    goto :eof
)
goto :eof

:end
color 0F
@pause

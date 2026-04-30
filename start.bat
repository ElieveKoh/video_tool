@echo off
chcp 65001 >nul
cls

echo =============================
echo   Video Tool v6.0
echo =============================
echo.

:: Move to current directory
cd /d "%~dp0"

echo Preparing...
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed.
    echo.
    echo Python 3.10 or higher is required.
    echo Please download from https://www.python.org/downloads/
    echo.
    echo IMPORTANT: Check "Add Python to PATH" during installation!
    pause
    exit /b 1
)

echo Python found.

:: Check FFmpeg (system or local)
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "bin\ffmpeg.exe" (
        echo.
        echo Downloading FFmpeg...
        mkdir bin 2>nul

        :: Download FFmpeg (official build)
        powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg.zip'}"

        echo Extracting FFmpeg...
        powershell -Command "Expand-Archive -Path 'ffmpeg.zip' -DestinationPath 'temp_ffmpeg' -Force"

        :: Copy ffmpeg.exe to bin folder
        for /d %%i in (temp_ffmpeg\ffmpeg-*) do (
            copy "%%i\bin\ffmpeg.exe" "bin\ffmpeg.exe" >nul
        )

        :: Cleanup
        rmdir /s /q temp_ffmpeg
        del ffmpeg.zip

        echo FFmpeg installed.
    ) else (
        echo FFmpeg already installed.
    )
    set PATH=%CD%\bin;%PATH%
) else (
    echo FFmpeg already installed.
)

:: Check yt-dlp (system or local)
where yt-dlp >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "bin\yt-dlp.exe" (
        echo.
        echo Downloading yt-dlp...
        mkdir bin 2>nul
        powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile 'bin\yt-dlp.exe'}"
        echo yt-dlp installed.
    ) else (
        echo yt-dlp already installed.
    )
    set PATH=%CD%\bin;%PATH%
) else (
    echo yt-dlp already installed.
)

:: Create/activate Python virtual environment
if not exist "venv" (
    echo.
    echo Creating Python virtual environment...
    python -m venv venv
    echo Virtual environment created.
)

call venv\Scripts\activate.bat

:: Install Streamlit (without pyarrow for Windows compatibility)
python -c "import streamlit" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo Installing Streamlit...
    python -m pip install streamlit --no-cache-dir --only-binary :all: 2>nul
    if %errorlevel% neq 0 (
        echo Retrying without pyarrow...
        python -m pip install --no-cache-dir altair numpy pandas pillow protobuf tornado watchdog
        python -m pip install --no-cache-dir streamlit --no-deps
    )
    echo Streamlit installed.
) else (
    echo Streamlit already installed.
)

:: Find available port
set PORT=8601
:CHECK_PORT
netstat -an | find ":%PORT%" | find "LISTENING" >nul
if %errorlevel% equ 0 (
    set /a PORT+=1
    goto CHECK_PORT
)

echo.
echo Starting VideoTool... (port %PORT%)
echo Browser will open automatically...
echo Localhost only (external access blocked)
echo.

:: Run Streamlit
python -m streamlit run video_converter_app.py --server.headless=false --server.port=%PORT% --server.address=localhost --server.fileWatcherType=none

echo.
echo Program terminated
pause

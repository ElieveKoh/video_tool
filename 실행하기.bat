@echo off
chcp 65001 >nul
cls

echo =============================
echo   🎥 Video Tool v5.0
echo =============================
echo.

:: 현재 디렉토리로 이동
cd /d "%~dp0"

echo ✅ 준비 중...
echo.

:: Python 확인
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ❌ Python이 설치되지 않았습니다.
    echo.
    echo Python 3.10 이상이 필요합니다.
    echo https://www.python.org/downloads/ 에서 다운로드하세요.
    echo.
    echo 설치 시 "Add Python to PATH" 옵션을 반드시 체크하세요!
    pause
    exit /b 1
)

echo ✅ Python이 설치되어 있습니다.

:: FFmpeg 확인 (시스템 또는 로컬)
where ffmpeg >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "bin\ffmpeg.exe" (
        echo.
        echo 📦 FFmpeg 다운로드 중...
        mkdir bin 2>nul

        :: FFmpeg 다운로드 (공식 빌드)
        powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg.zip'}"

        echo ✅ FFmpeg 압축 해제 중...
        powershell -Command "Expand-Archive -Path 'ffmpeg.zip' -DestinationPath 'temp_ffmpeg' -Force"

        :: ffmpeg.exe를 bin 폴더로 복사
        for /d %%i in (temp_ffmpeg\ffmpeg-*) do (
            copy "%%i\bin\ffmpeg.exe" "bin\ffmpeg.exe" >nul
        )

        :: 정리
        rmdir /s /q temp_ffmpeg
        del ffmpeg.zip

        echo ✅ FFmpeg 설치 완료
    ) else (
        echo ✅ FFmpeg이 이미 설치되어 있습니다.
    )
    set PATH=%CD%\bin;%PATH%
) else (
    echo ✅ FFmpeg이 이미 설치되어 있습니다.
)

:: yt-dlp 확인 (시스템 또는 로컬)
where yt-dlp >nul 2>&1
if %errorlevel% neq 0 (
    if not exist "bin\yt-dlp.exe" (
        echo.
        echo 📦 yt-dlp 다운로드 중...
        mkdir bin 2>nul
        powershell -Command "& {[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe' -OutFile 'bin\yt-dlp.exe'}"
        echo ✅ yt-dlp 설치 완료
    ) else (
        echo ✅ yt-dlp이 이미 설치되어 있습니다.
    )
    set PATH=%CD%\bin;%PATH%
) else (
    echo ✅ yt-dlp이 이미 설치되어 있습니다.
)

:: Python 가상환경 생성/활성화
if not exist "venv" (
    echo.
    echo 📦 Python 가상환경 생성 중...
    python -m venv venv
    echo ✅ Python 가상환경 생성 완료
)

call venv\Scripts\activate.bat

:: Streamlit 설치
python -c "import streamlit" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo 📦 Streamlit 설치 중...
    python -m pip install streamlit --quiet
    echo ✅ Streamlit 설치 완료
) else (
    echo ✅ Streamlit이 이미 설치되어 있습니다.
)

:: 사용 가능한 포트 찾기
set PORT=8601
:CHECK_PORT
netstat -an | find ":%PORT%" | find "LISTENING" >nul
if %errorlevel% equ 0 (
    set /a PORT+=1
    goto CHECK_PORT
)

echo.
echo 🚀 VideoTool 실행 중... (포트 %PORT%)
echo 브라우저가 열립니다...
echo 🔒 로컬호스트 전용 (외부 접근 차단)
echo.

:: Streamlit 실행
python -m streamlit run video_converter_app.py --server.headless=false --server.port=%PORT% --server.address=localhost

echo.
echo 프로그램 종료
pause

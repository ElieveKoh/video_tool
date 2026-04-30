#!/bin/bash

echo "🎥 스마트 비디오 변환기"
echo "====================="

# 앱 버전 (릴리즈할 때 함께 갱신)
APP_VERSION="6.0.3"
# GitHub 저장소 (예: channy/video-tool)
GITHUB_REPO="ElieveKoh/video_tool"

# 현재 스크립트가 있는 디렉토리로 이동
cd "$(dirname "$0")"
APP_DIR="$(pwd)"

# 보안 문제 자동 해결
xattr -rd com.apple.quarantine . 2>/dev/null || true
chmod +x "$0" 2>/dev/null || true

echo "✅ 준비 중..."

# Python3 간단 확인 및 설치
if ! command -v python3 &> /dev/null; then
    echo "📦 Python3 설치 중..."
    # 공식 Python 설치파일 다운로드 (간단)
    curl -o python_installer.pkg https://www.python.org/ftp/python/3.11.0/python-3.11.0-macos11.pkg
    sudo installer -pkg python_installer.pkg -target /
    rm python_installer.pkg
fi

check_and_apply_update() {
    if [ -z "$GITHUB_REPO" ] || [ "$GITHUB_REPO" = "OWNER/REPO" ]; then
        echo "ℹ️  자동 업데이트를 사용하려면 실행하기.command의 GITHUB_REPO 값을 설정하세요."
        return 0
    fi

    local api_url="https://api.github.com/repos/${GITHUB_REPO}/releases/latest"
    local release_json
    release_json="$(curl -fsSL "$api_url" 2>/dev/null)"

    if [ -z "$release_json" ]; then
        echo "ℹ️  업데이트 서버에 연결할 수 없어 현재 버전으로 실행합니다."
        return 0
    fi

    local latest_tag
    latest_tag="$(python3 - <<'PY' "$release_json"
import json, sys
try:
    data = json.loads(sys.argv[1])
    print(data.get("tag_name", "").strip())
except Exception:
    print("")
PY
)"

    if [ -z "$latest_tag" ]; then
        echo "ℹ️  최신 버전 정보를 읽지 못해 현재 버전으로 실행합니다."
        return 0
    fi

    local latest_version="${latest_tag#v}"
    local needs_update
    needs_update="$(python3 - <<'PY' "$APP_VERSION" "$latest_version"
import re, sys
def parse(v: str):
    nums = re.findall(r"\d+", v)
    return [int(x) for x in nums] if nums else [0]
def cmp(a, b):
    n = max(len(a), len(b))
    a = a + [0] * (n - len(a))
    b = b + [0] * (n - len(b))
    return (a > b) - (a < b)
current = parse(sys.argv[1])
latest = parse(sys.argv[2])
print("yes" if cmp(current, latest) < 0 else "no")
PY
)"

    if [ "$needs_update" != "yes" ]; then
        return 0
    fi

    echo ""
    echo "🆕 새 버전이 있습니다!"
    echo "   현재: v$APP_VERSION"
    echo "   최신: $latest_tag"
    read -r -p "지금 업데이트할까요? [Y/n] " answer

    case "$answer" in
        [Nn]*)
            echo "⏭️  업데이트를 건너뛰고 현재 버전으로 실행합니다."
            return 0
            ;;
    esac

    local download_url
    download_url="$(python3 - <<'PY' "$release_json"
import json, sys
try:
    data = json.loads(sys.argv[1])
    assets = data.get("assets", [])
    preferred = None
    fallback = None
    for a in assets:
        name = str(a.get("name", "")).lower()
        url = a.get("browser_download_url", "")
        if not url:
            continue
        if name.endswith(".zip"):
            if ("mac" in name or "video" in name or "tool" in name):
                preferred = url
                break
            if fallback is None:
                fallback = url
    print(preferred or fallback or data.get("zipball_url", ""))
except Exception:
    print("")
PY
)"

    if [ -z "$download_url" ]; then
        echo "❌ 다운로드 URL을 찾지 못했습니다. 현재 버전으로 실행합니다."
        return 0
    fi

    local tmp_dir="$APP_DIR/.update_tmp"
    rm -rf "$tmp_dir"
    mkdir -p "$tmp_dir"

    echo "📥 업데이트 다운로드 중..."
    if ! curl -fL "$download_url" -o "$tmp_dir/update.zip"; then
        echo "❌ 다운로드 실패. 현재 버전으로 실행합니다."
        rm -rf "$tmp_dir"
        return 0
    fi

    echo "📦 업데이트 적용 중..."
    if ! unzip -q "$tmp_dir/update.zip" -d "$tmp_dir/extracted"; then
        echo "❌ 압축 해제 실패. 현재 버전으로 실행합니다."
        rm -rf "$tmp_dir"
        return 0
    fi

    local src_dir=""
    if [ -f "$tmp_dir/extracted/video_converter_app.py" ] || [ -d "$tmp_dir/extracted/VideoTool.app" ]; then
        src_dir="$tmp_dir/extracted"
    else
        for candidate in "$tmp_dir/extracted"/*; do
            if [ -d "$candidate" ]; then
                src_dir="$candidate"
                break
            fi
        done
    fi

    if [ -z "$src_dir" ]; then
        echo "❌ 업데이트 파일 구조를 확인할 수 없습니다."
        rm -rf "$tmp_dir"
        return 0
    fi

    if ! command -v rsync >/dev/null 2>&1; then
        echo "❌ rsync가 없어 자동 업데이트를 적용할 수 없습니다."
        rm -rf "$tmp_dir"
        return 0
    fi

    if ! rsync -a \
        --exclude ".git/" \
        --exclude "venv/" \
        --exclude "bin/" \
        --exclude "converted_*/" \
        --exclude "youtube_downloads/" \
        --exclude ".update_tmp/" \
        "$src_dir/" "$APP_DIR/"; then
        echo "❌ 파일 동기화 실패. 현재 버전으로 실행합니다."
        rm -rf "$tmp_dir"
        return 0
    fi

    chmod +x "$APP_DIR/실행하기.command" 2>/dev/null || true
    rm -rf "$tmp_dir"

    echo "✅ 업데이트 완료! 최신 버전으로 다시 시작합니다."
    exec "$APP_DIR/실행하기.command"
}

check_and_apply_update

# FFmpeg 확인 (시스템 우선, 없으면 로컬 다운로드)
if ! command -v ffmpeg &> /dev/null && [ ! -f "bin/ffmpeg" ]; then
    echo "📦 FFmpeg 다운로드 중..."
    mkdir -p bin
    # 미리 컴파일된 FFmpeg 다운로드
    curl -L "https://evermeet.cx/ffmpeg/getrelease/zip" -o ffmpeg.zip
    unzip -q ffmpeg.zip -d bin/
    rm ffmpeg.zip
    chmod +x bin/ffmpeg
    export PATH="$(pwd)/bin:$PATH"
elif [ -f "bin/ffmpeg" ]; then
    export PATH="$(pwd)/bin:$PATH"
fi

# yt-dlp 확인 (시스템 우선, 없으면 로컬 다운로드)
if ! command -v yt-dlp &> /dev/null && [ ! -f "bin/yt-dlp" ]; then
    echo "📦 yt-dlp 다운로드 중..."
    mkdir -p bin
    curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos -o bin/yt-dlp
    chmod +x bin/yt-dlp
    export PATH="$(pwd)/bin:$PATH"
elif [ -f "bin/yt-dlp" ]; then
    export PATH="$(pwd)/bin:$PATH"
fi

# Python 가상환경 (Python 3.12 사용)
if [ ! -d "venv" ]; then
    echo "📦 Python 3.12 환경 설정 중..."
    # Python 3.12 찾기
    if command -v python3.12 &> /dev/null; then
        python3.12 -m venv venv
    elif [ -f "/opt/homebrew/bin/python3.12" ]; then
        /opt/homebrew/bin/python3.12 -m venv venv
    else
        echo "⚠️  Python 3.12를 찾을 수 없습니다. 기본 python3를 사용합니다."
        python3 -m venv venv
    fi
fi

source venv/bin/activate
python -m pip install streamlit --quiet

echo ""
echo "🚀 비디오 변환기 시작!"
echo "브라우저가 열립니다..."
echo "🔒 로컬호스트 전용 (외부 접근 차단)"
echo ""

# 사용 가능한 포트 찾기
PORT=8601
while lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; do
    echo "⚠️  포트 $PORT 는 이미 사용 중입니다. 다른 포트를 찾는 중..."
    PORT=$((PORT + 1))
done

echo "✅ 포트 $PORT 에서 실행합니다."

# Streamlit 실행 (localhost 전용)
python -m streamlit run video_converter_app.py --server.headless=false --server.port=$PORT --server.address=localhost --server.fileWatcherType=none

echo ""
echo "프로그램 종료"
read -p "아무 키나 누르세요..."
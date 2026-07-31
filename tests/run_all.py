#!/usr/bin/env python3
"""테스트 전체 실행.

사용법 (저장소 루트에서):
    ./venv/bin/python tests/run_all.py

필요 조건: venv에 streamlit, PATH에 ffmpeg/ffprobe.
"""
import os
import shutil
import subprocess
import sys

TESTS = [
    "test_css_integrity.py",
    "test_ui_navigation.py",
    "test_core_media.py",
    "test_mute_url.py",
]

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing:
        print(f"❌ 필요한 실행 파일이 없습니다: {', '.join(missing)}")
        return 1
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("❌ streamlit이 없습니다. ./venv/bin/python 으로 실행하세요.")
        return 1

    results = {}
    for name in TESTS:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        proc = subprocess.run([sys.executable, os.path.join(HERE, name)])
        results[name] = proc.returncode == 0

    print(f"\n{'=' * 60}\n요약\n{'=' * 60}")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    failed = [n for n, ok in results.items() if not ok]
    print("\n" + ("✅ 전체 통과" if not failed else f"❌ 실패: {', '.join(failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

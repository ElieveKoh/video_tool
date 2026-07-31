"""Mute의 URL 입력 경로 테스트 (로컬 HTTP 서버로 실제 http:// 입력을 재현).

예전 구현은 URL이면 total_duration을 0으로 두고 진행률 콜백을 건너뛰었다.
그래서 URL을 넣으면 스피너만 돌고 진행 상황을 알 수 없었다.
"""
import functools
import http.server
import os
import socketserver
import subprocess
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import video_converter_app as app  # noqa: E402

PORT = 8767
failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "clip.mp4")
    # faststart로 moov를 앞에 둬야 원격 probe가 현실적으로 동작한다
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
        "-movflags", "+faststart", src,
    ], check=True)

    handler = functools.partial(QuietHandler, directory=tmp)
    server = ReusableServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/clip.mp4"

    try:
        core = app.VideoConverterCore()

        print("\n== URL duration 조회 ==")
        duration = core.probe_duration(url)
        check("URL 길이 조회 성공 (이전엔 0 고정)", duration > 19, f"{duration:.2f}s")

        print("\n== URL Mute ==")
        out = os.path.join(tmp, "clip_muted.mp4")
        seen = []
        ok, msg = core.strip_audio(url, out, lambda p, sec, spd: seen.append((p, sec, spd)),
                                   total_duration=duration)
        check("URL Mute 성공", ok, str(msg))
        progress = [s[0] for s in seen if s[0] is not None]
        check("URL 진행률 갱신됨 (기존 버그)", bool(progress), f"{len(seen)}회 콜백")
        check("진행률 100% 도달", progress and max(progress) > 0.9,
              f"max={max(progress, default=0):.2f}")
        audio = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
             "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
        check("오디오 제거됨", audio == "", f"audio streams={audio!r}")
    finally:
        server.shutdown()
        server.server_close()

    return failures


if __name__ == "__main__":
    fails = main()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL PASS'}")
    sys.exit(1 if fails else 0)

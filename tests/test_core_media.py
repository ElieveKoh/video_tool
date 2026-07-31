"""코어 미디어 로직 테스트: ffprobe 호출 횟수, Mute 진행률, 유튜브 제목 조회 속도."""
import os
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import video_converter_app as app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def make_clip(path, seconds=10):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
        "-movflags", "+faststart", path,
    ], check=True)


def audio_streams(path):
    return subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()


def main():
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "sample.mp4")
    make_clip(src)
    core = app.VideoConverterCore()

    print("\n== ffprobe 호출 횟수 (메모이즈) ==")
    real_run = subprocess.run
    count = {"n": 0}

    def counting_run(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "ffprobe" in str(cmd[0]):
            count["n"] += 1
        return real_run(cmd, *a, **kw)

    subprocess.run = counting_run
    app.subprocess.run = counting_run
    try:
        app._VIDEO_INFO_CACHE.clear()
        count["n"] = 0
        for _ in range(5):
            info = core.get_video_info(src)
        check("반복 조회 5회 → ffprobe 1회", count["n"] == 1, f"{count['n']}회")
        check("duration 정상", abs(info["duration"] - 10.0) < 1.0, f"{info['duration']:.2f}s")

        # 이전에는 호출자 + convert_video + get_codec_options 로 파일당 3회 실행됐다
        app._VIDEO_INFO_CACHE.clear()
        count["n"] = 0
        core.get_video_info(src)
        ok, msg = core.convert_video(src, os.path.join(tmp, "out.mp4"),
                                     "h264", "original", "fast")
        check("변환 1건 → ffprobe 1회 (이전 3회)", count["n"] == 1, f"{count['n']}회")
        check("변환 성공", ok, str(msg))

        # 파일이 바뀌면 (경로, mtime, size) 키가 달라져 캐시가 무효화되어야 한다
        os.utime(src, (time.time(), time.time()))
        count["n"] = 0
        core.get_video_info(src)
        check("mtime 변경 시 캐시 무효화", count["n"] == 1, f"{count['n']}회")
    finally:
        subprocess.run = real_run
        app.subprocess.run = real_run

    print("\n== Mute 진행률 ==")
    muted = os.path.join(tmp, "muted.mp4")
    seen = []
    ok, msg = core.strip_audio(src, muted, lambda p, sec, spd: seen.append((p, sec, spd)))
    check("로컬 파일 성공", ok, str(msg))
    check("진행률 콜백 호출됨", len(seen) > 0, f"{len(seen)}회")
    check("진행률 100% 도달", seen and seen[-1][0] is not None and seen[-1][0] > 0.5,
          str(seen[-1] if seen else None))
    check("오디오 제거됨", audio_streams(muted) == "")

    # duration을 모르는 경우(원격 URL 등): 이전 구현은 total_duration>0 가드에 걸려
    # 콜백을 한 번도 호출하지 않았다 → 프로그레스 바가 아예 안 보였다
    seen2 = []
    ok2, msg2 = core.strip_audio(src, os.path.join(tmp, "muted2.mp4"),
                                 lambda p, sec, spd: seen2.append((p, sec, spd)),
                                 total_duration=0)
    check("길이 미확인 시에도 성공", ok2, str(msg2))
    check("길이 미확인 시 콜백 호출됨 (기존 버그)", len(seen2) > 0, f"{len(seen2)}회")
    check("길이 미확인 시 progress=None", all(u[0] is None for u in seen2))
    check("처리 시간은 보고됨", max((u[1] for u in seen2), default=0) > 0,
          f"{max((u[1] for u in seen2), default=0):.1f}s")

    print("\n== probe_duration ==")
    check("로컬 길이 조회", abs(core.probe_duration(src) - 10.0) < 1.0,
          f"{core.probe_duration(src):.2f}s")

    print("\n== 유튜브 제목 조회 (oEmbed) ==")
    dl = app.YouTubeDownloader()
    app._YT_TITLE_CACHE.clear()
    t0 = time.time()
    info = dl.get_video_title_fast("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    elapsed = time.time() - t0
    if info is None:
        print("  [SKIP] 네트워크 불가 — 제목 조회 테스트 생략")
    else:
        check("제목 조회 성공", info["title"] != "Unknown", info["title"][:60])
        # 기존 yt-dlp --get-title 경로는 5~20초가 걸렸다
        check("3초 이내 응답", elapsed < 3.0, f"{elapsed:.2f}s")
        t1 = time.time()
        again = dl.get_video_title_fast("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        check("캐시 적중", again == info and (time.time() - t1) < 0.05,
              f"{time.time()-t1:.4f}s")

    return failures


if __name__ == "__main__":
    fails = main()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL PASS'}")
    sys.exit(1 if fails else 0)

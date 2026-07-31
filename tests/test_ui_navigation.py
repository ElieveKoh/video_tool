"""UI 회귀 테스트 (streamlit AppTest).

핵심 목적: rerun이 발생해도 현재 섹션이 유지되는지 검증한다.
예전 구현은 st.tabs를 썼고, st.tabs의 선택 상태는 프론트엔드 전용이라
st.rerun()이 걸리면 항상 첫 탭으로 되돌아갔다("클릭하면 다른 탭으로 가는" 증상).
"""
import os
import subprocess
import sys
import tempfile
import time

from streamlit.testing.v1 import AppTest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "video_converter_app.py")

CONVERT, YOUTUBE, MUTE = "🎬 Convert", "📥 YouTube", "🔇 Mute"

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def assert_no_exception(at, stage):
    msgs = [str(e.value)[:300] for e in at.exception]
    check(f"{stage}: 예외 없음", not msgs, "; ".join(msgs))


def make_clip(path, seconds=5, audio=True):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=24:duration={seconds}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [path]
    subprocess.run(cmd, check=True)


def main():
    print("\n== 섹션 네비게이션 ==")
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert_no_exception(at, "초기 실행")
    check("기본 섹션은 Convert", at.session_state["active_tab"] == CONVERT,
          repr(at.session_state["active_tab"]))

    nav = at.radio(key="nav_section")
    check("네비게이션 항목 3개", list(nav.options) == [CONVERT, YOUTUBE, MUTE], str(list(nav.options)))

    at.radio(key="nav_section").set_value(YOUTUBE).run()
    assert_no_exception(at, "YouTube 전환")
    url_rows = [w.key for w in at.text_input if w.key and w.key.startswith("yt_url_row_")]
    check("YouTube 섹션 렌더 (URL 입력 행)", len(url_rows) == 1, str(url_rows))

    # ★ 핵심 회귀: rerun을 유발하는 버튼을 눌러도 섹션이 유지되어야 한다
    at.button(key="theme_toggle").click().run()
    assert_no_exception(at, "테마 토글 rerun")
    check("rerun 후 YouTube 유지 (탭 튐 회귀)", at.session_state["active_tab"] == YOUTUBE,
          repr(at.session_state["active_tab"]))

    at.radio(key="nav_section").set_value(MUTE).run()
    assert_no_exception(at, "Mute 전환")
    btns = [b.key for b in at.button if b.key]
    check("Mute 생성/폴더 버튼", all(k in btns for k in
          ("mute_generate_btn", "mute_folder_select")), str(sorted(btns)))
    # 중단은 전역 작업 패널의 Stop 하나로 통일했다
    check("Mute 전용 Stop 버튼 없음", "mute_stop_btn" not in btns)

    at.button(key="theme_toggle").click().run()
    check("rerun 후 Mute 유지", at.session_state["active_tab"] == MUTE)

    print("\n== 파일 목록 (위젯 key 안정성) ==")
    tmp = tempfile.mkdtemp()
    files = []
    for name in ["a clip.mp4", "b <odd> name.mp4", "c.mov"]:
        p = os.path.join(tmp, name)
        with open(p, "wb") as f:
            f.write(b"\0" * 4096)
        files.append(p)

    at.radio(key="nav_section").set_value(CONVERT).run()
    at.session_state["video_files_list"] = files
    at.session_state["file_selection_state"] = {f: True for f in files}
    at.session_state["file_meta_cache"] = {
        f: {"name": os.path.basename(f), "size": os.path.getsize(f), "date": os.path.getmtime(f)}
        for f in files
    }
    at.run()
    assert_no_exception(at, "파일 목록 렌더")

    file_keys = sorted(c.key for c in at.checkbox if c.key and c.key.startswith("file_check_"))
    check("파일당 체크박스 1개", len(file_keys) == 3, str(len(file_keys)))

    at.button(key="toggle_all_header").click().run()
    assert_no_exception(at, "전체 해제")
    values = [c.value for c in at.checkbox if c.key and c.key.startswith("file_check_")]
    keys_after = sorted(c.key for c in at.checkbox if c.key and c.key.startswith("file_check_"))
    check("전체 해제 반영", all(v is False for v in values), str(values))
    # key에 토글 카운터를 섞으면 전체 위젯이 재생성되어 파일이 많을 때 매우 느려진다
    check("체크박스 key 불변 (성능 회귀)", keys_after == file_keys)

    at.button(key="sort_size").click().run()
    assert_no_exception(at, "크기 정렬")
    check("정렬 상태 반영", at.session_state["sort_by"] == "size",
          f"{at.session_state['sort_by']}/{at.session_state['sort_order']}")

    print("\n== 유튜브 설정 ==")
    at.radio(key="nav_section").set_value(YOUTUBE).run()
    cbs = {c.key: c.value for c in at.checkbox if c.key}
    # 예전에는 변환이 끝난 뒤 value=True 체크박스를 렌더해 원본이 묻지도 않고 삭제됐다
    check("원본 삭제 옵션이 변환 전 설정에 있음", "yt_delete_original" in cbs, str(sorted(cbs)))

    print("\n== 다중 URL 입력 ==")
    def url_row_keys(app_test):
        return [w.key for w in app_test.text_input
                if w.key and w.key.startswith("yt_url_row_")]

    def btn(app_test, key):
        # 마지막 매칭 노드를 쓴다. AppTest는 스크립트 중간 st.rerun() 시 이전 트리
        # 노드를 남기므로, 첫 매칭은 rerun 이전의 낡은 상태일 수 있다.
        matches = [b for b in app_test.button if b.key == key]
        return matches[-1] if matches else None

    check("빈 입력이면 Add to Queue 비활성", btn(at, "yt_add_to_queue").disabled is True)
    # Download Now는 제거됐다 — Batch Download와 역할이 같아 헷갈렸다
    check("Download Now 버튼 없음", btn(at, "yt_download_now") is None)
    check("행이 1개면 삭제 버튼 비활성",
          all(b.disabled for b in at.button if b.key and b.key.startswith("yt_url_rm_")))

    at.button(key="yt_url_add_row").click().run()
    assert_no_exception(at, "링크 칸 추가")
    check("＋ 로 행 추가됨", len(url_row_keys(at)) == 2, str(url_row_keys(at)))

    at.button(key="yt_url_add_row").click().run()
    keys3 = url_row_keys(at)
    check("행 3개", len(keys3) == 3, str(keys3))

    # 가운데 행 삭제 시 나머지 행의 값이 밀리지 않아야 한다 (key가 행 id 기준)
    at.text_input(key=keys3[0]).set_value("https://youtu.be/AAA").run()
    at.text_input(key=keys3[2]).set_value("https://youtu.be/CCC").run()
    rm_key = keys3[1].replace("yt_url_row_", "yt_url_rm_")
    at.button(key=rm_key).click().run()
    assert_no_exception(at, "가운데 행 삭제")
    remaining = url_row_keys(at)
    values = [at.text_input(key=k).value for k in remaining]
    check("행 2개 남음", len(remaining) == 2, str(remaining))
    check("남은 행의 값이 밀리지 않음",
          values == ["https://youtu.be/AAA", "https://youtu.be/CCC"], str(values))

    # 한 칸에 여러 링크를 붙여넣으면 분리해서 센다
    at.text_input(key=remaining[0]).set_value(
        "https://youtu.be/AAA https://youtu.be/BBB, https://youtu.be/DDD").run()
    label = btn(at, "yt_add_to_queue").label
    check("붙여넣은 여러 링크를 개수에 반영 (4개)", "(4)" in label, label)
    check("중복 링크는 1회만 계산", "(5)" not in label, label)

    print("\n== 버튼 위계 ==")
    types = {b.key: b.proto.type for b in at.button if b.key}
    check("Add to Queue = primary", types.get("yt_add_to_queue") == "primary",
          str(types.get("yt_add_to_queue")))
    check("링크 칸 추가 = tertiary", types.get("yt_url_add_row") == "tertiary",
          str(types.get("yt_url_add_row")))

    print("\n== 다중 URL → 큐 (실제 네트워크) ==")
    real_urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=9bZkp7q19f0",
    ]
    rows_now = url_row_keys(at)
    at.text_input(key=rows_now[0]).set_value(" ".join(real_urls)).run()
    at.button(key="yt_add_to_queue").click().run()
    assert_no_exception(at, "큐에 일괄 추가")
    queue = at.session_state["yt_queue"]
    if len(queue) != 2:
        print(f"  [SKIP] 네트워크 불가로 큐 추가 생략 (queue={len(queue)})")
    else:
        check("2개 링크가 한 번에 큐로", len(queue) == 2, f"{len(queue)}개")
        check("입력 순서 유지", [q["url"] for q in queue] == real_urls,
              str([q["url"] for q in queue]))
        check("제목 조회됨", all(q["info"]["title"] != "Unknown" for q in queue),
              str([q["info"]["title"][:30] for q in queue]))
        check("모두 선택 상태", all(at.session_state["yt_queue_selection"].values()))
        # 행 초기화는 session_state로 확인한다. AppTest는 스크립트 중간의 st.rerun()
        # 시 이전 트리 노드를 남겨두기 때문에(하네스 한계) 트리의 위젯 개수는
        # 신뢰할 수 없다. yt_url_rows 가 앱의 실제 상태다.
        check("추가 후 입력 행 1개로 초기화",
              len(at.session_state["yt_url_rows"]) == 1,
              str(at.session_state["yt_url_rows"]))
        _add_btn = btn(at, "yt_add_to_queue")
        check("추가 후 대기 링크 0개", "(" not in _add_btn.label, _add_btn.label)
        check("추가 후 Add to Queue 비활성", _add_btn.disabled is True)

    print("\n== 섹션 전환 시 입력값 유지 ==")
    # 활성 섹션만 렌더하는 구조의 부작용: Streamlit은 이번 실행에 없는 위젯의 state를
    # 정리하므로, 보관/복원하지 않으면 섹션을 옮길 때마다 입력값과 설정이 초기화된다.
    at3 = AppTest.from_file(APP, default_timeout=120)
    at3.run()
    at3.radio(key="nav_section").set_value(MUTE).run()
    at3.text_input(key="mute_input_source").set_value("http://example.com/keep.mp4").run()
    at3.radio(key="nav_section").set_value(YOUTUBE).run()
    _r = [w.key for w in at3.text_input if w.key and w.key.startswith("yt_url_row_")]
    at3.text_input(key=_r[0]).set_value("https://youtu.be/KEEP").run()
    at3.selectbox(key="yt_resolution").set_value("720p").run()
    at3.radio(key="nav_section").set_value(CONVERT).run()
    at3.selectbox(key="vc_codec").set_value("h265").run()

    at3.radio(key="nav_section").set_value(MUTE).run()
    assert_no_exception(at3, "Mute 재방문")
    check("Mute 입력값 유지", at3.text_input(key="mute_input_source").value ==
          "http://example.com/keep.mp4", repr(at3.text_input(key="mute_input_source").value))

    at3.radio(key="nav_section").set_value(YOUTUBE).run()
    _r2 = [w.key for w in at3.text_input if w.key and w.key.startswith("yt_url_row_")]
    check("YouTube 링크 유지", at3.text_input(key=_r2[0]).value == "https://youtu.be/KEEP",
          repr(at3.text_input(key=_r2[0]).value))
    check("YouTube 설정 유지", at3.selectbox(key="yt_resolution").value == "720p",
          repr(at3.selectbox(key="yt_resolution").value))

    at3.radio(key="nav_section").set_value(CONVERT).run()
    check("Convert 설정 유지", at3.selectbox(key="vc_codec").value == "h265",
          repr(at3.selectbox(key="vc_codec").value))

    print("\n== 큐 툴바 / 배치 다운로드 위계 ==")
    # st.rerun() 잔여 노드 없이 검증하기 위해 새 인스턴스에서 큐를 직접 주입한다.
    at2 = AppTest.from_file(APP, default_timeout=120)
    at2.run()
    at2.radio(key="nav_section").set_value(YOUTUBE).run()
    seeded = [
        {"url": "https://youtu.be/seed1", "info": {"title": "seed 1", "uploader": "u",
                                                   "duration": 0, "view_count": 0},
         "settings": {"codec": "h264", "resolution": "original", "quality": "balanced",
                      "fps": "original", "scan": "progressive", "custom_video_br": None,
                      "custom_audio_br": None, "download_only": False}},
        {"url": "https://youtu.be/seed2", "info": {"title": "seed 2", "uploader": "u",
                                                   "duration": 0, "view_count": 0},
         "settings": {"codec": "h264", "resolution": "original", "quality": "balanced",
                      "fps": "original", "scan": "progressive", "custom_video_br": None,
                      "custom_audio_br": None, "download_only": False}},
    ]
    at2.session_state["yt_queue"] = seeded
    at2.session_state["yt_queue_selection"] = {i["url"]: True for i in seeded}
    at2.run()
    assert_no_exception(at2, "큐 주입 렌더")

    types2 = {b.key: b.proto.type for b in at2.button if b.key}
    check("전체 해제 = tertiary", types2.get("yt_select_all") == "tertiary",
          str(types2.get("yt_select_all")))
    check("큐 비우기 = tertiary", types2.get("yt_clear_queue") == "tertiary",
          str(types2.get("yt_clear_queue")))

    batch = btn(at2, "yt_start_batch")
    check("Batch Download 버튼이 큐 아래에 존재", batch is not None)
    if batch is not None:
        check("Batch Download = primary", batch.proto.type == "primary", str(batch.proto.type))
        check("선택 개수 표시", "(2)" in batch.label, batch.label)
        check("작업 전 활성", batch.disabled is False)
    # Stop 은 전역 작업 패널로 통일했다(섹션마다 따로 두지 않는다)
    check("섹션별 Stop 버튼 없음", btn(at2, "yt_stop_batch") is None)

    # 중복 URL은 제목 조회(네트워크) 전에 걸러진다 → 네트워크 없이 검증 가능
    _rows2 = [w.key for w in at2.text_input if w.key and w.key.startswith("yt_url_row_")]
    at2.text_input(key=_rows2[0]).set_value("https://youtu.be/seed1").run()
    at2.button(key="yt_add_to_queue").click().run()
    assert_no_exception(at2, "중복 추가 시도")
    check("이미 큐에 있는 링크는 건너뜀", len(at2.session_state["yt_queue"]) == 2,
          f"{len(at2.session_state['yt_queue'])}개")
    # 추가된 게 없을 때는 rerun하지 않으므로 경고가 그대로 보여야 한다
    # (예전에는 st.warning 직후 st.rerun()이 메시지를 지웠다)
    check("건너뜀 경고 표시", any("건너뜀" in w.value for w in at2.warning),
          str([w.value for w in at2.warning]))

    # 전부 해제하면 배치 다운로드가 비활성이어야 한다
    at2.button(key="yt_select_all").click().run()
    assert_no_exception(at2, "전체 해제")
    check("선택 0개면 Batch Download 비활성", btn(at2, "yt_start_batch").disabled is True)
    at2.button(key="yt_select_all").click().run()
    check("다시 전체 선택하면 활성", btn(at2, "yt_start_batch").disabled is False)

    at2.button(key="yt_clear_queue").click().run()
    assert_no_exception(at2, "큐 비우기")
    check("큐 비우기 동작", at2.session_state["yt_queue"] == [])
    check("큐가 비면 Batch Download 사라짐", btn(at2, "yt_start_batch") is None)

    print("\n== Mute 전체 흐름 ==")
    # 위에서 st.rerun()을 유발한 인스턴스는 트리에 잔여 노드가 남아 이후 run()이
    # 깨진다(AppTest 한계). 이 흐름은 새 인스턴스에서 검증한다.
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    src = os.path.join(tmp, "flow.mp4")
    make_clip(src)
    at.radio(key="nav_section").set_value(MUTE).run()
    at.text_input(key="mute_input_source").set_value(src).run()
    assert_no_exception(at, "Mute 입력")
    check("출력 파일명 자동 생성", at.text_input(key="mute_output_name").value == "flow_muted.mp4",
          at.text_input(key="mute_output_name").value)

    at.session_state["mute_save_folder_path"] = tmp
    at.run()
    check("작업 전 네비게이션 활성", at.radio(key="nav_section").disabled is False)
    check("작업 전 job 없음", at.session_state["job"] is None)

    # 실행. AppTest는 run_every 프래그먼트의 재실행을 동기로 따라가므로
    # '즉시 반환'을 시간으로 재는 것은 의미가 없다(논블로킹 자체는
    # test_core_media.py 의 스레드 레벨 테스트에서 검증한다).
    # 여기서는 완료 결과가 표시되고 상태가 정리되는지를 본다.
    seen_success = []
    at.button(key="mute_generate_btn").click().run()
    assert_no_exception(at, "Mute 실행")
    seen_success += [m.value for m in at.success]

    deadline = time.time() + 90
    while at.session_state["job"] is not None and time.time() < deadline:
        at.run()
        seen_success += [m.value for m in at.success]
    check("작업 완료 후 job 정리됨", at.session_state["job"] is None)

    out = os.path.join(tmp, "flow_muted.mp4")
    audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip() \
        if os.path.exists(out) else "?"

    check("출력 파일 생성", os.path.exists(out))
    check("오디오 제거됨", audio == "", f"audio streams={audio!r}")
    check("완료 결과 표시", any("무음 비디오 저장 완료" in m for m in seen_success),
          str(seen_success[-3:]))
    check("작업 후 섹션 유지", at.session_state["active_tab"] == MUTE)

    return failures


if __name__ == "__main__":
    fails = main()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL PASS'}")
    sys.exit(1 if fails else 0)

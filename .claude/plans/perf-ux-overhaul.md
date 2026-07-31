# Stage Map: 성능 + UX 전면 개선            (2026-07-31 / VideoTool-v6.0.5-mac)

목표: 탭 튐 현상 제거, rerun당 렌더 비용 1/3로 축소, 유튜브 제목 조회 20s→0.5s, Mute에 실진행률 표시.

가드레일(불변):
- `python -m py_compile video_converter_app.py` 통과
- 3개 기능(로컬 변환 / 유튜브 다운로드 / Mute) 모두 동작 유지
- 기존 CSS 클래스명(`vt-*`, `.st-key-*`)은 그대로 — Lumina 디자인 깨지지 않을 것
- 파일: `video_converter_app.py` 단일 파일 유지 (분리 안 함)

---

## S0 조사 [DONE]

### A. 탭이 튀는 근본 원인 — `st.tabs` + 20개 `st.rerun()`
- `video_converter_app.py:1865` — `tab1, tab2, tab3 = st.tabs([...])`
- `st.tabs`의 선택 상태는 **파이썬에 없다**. 프론트엔드 전용 상태이며 `st.rerun()`으로 스크립트가 재실행되면 탭 스트립이 리마운트되어 첫 탭으로 돌아간다.
- 탭2/탭3 안에서 눌리는 `st.rerun()`: 2297, 2358, 2363, 2419, 2429, 2435, 2466
  → "Add to Queue"(2466) 누르면 → rerun → 탭1로 강제 이동. **사용자가 보고한 증상 그대로.**
- 테마 토글(1832)도 어느 탭에서든 탭1로 되돌린다.
- `video_converter_app.py:1835` 조건부 `st.warning`이 tabs보다 위에 있어, 변환 시작/종료 시 tabs 앞 엘리먼트 개수가 바뀌며 리마운트를 한 번 더 유발.

### B. 앱이 느린 원인 (rerun당 낭비)
| # | 위치 | 문제 |
|---|---|---|
| B1 | 1869 / 2180 / 2480 | `with tab1/tab2/tab3` → **보이지 않는 탭까지 매 rerun마다 전부 렌더**. 실제 필요량의 3배 |
| B2 | 1259 (`st.html(get_app_css())`) | ~515줄 CSS를 매 rerun마다 재삽입. 748–749의 Google Fonts `<link>` 2개도 매번 재삽입 → 스타일 재계산 |
| B3 | 2115–2133 | 파일당 `st.columns(4)` + checkbox + markdown×3 ≈ **위젯 7개/파일**. 50파일 = 350위젯 직렬화 |
| B4 | 2125, 2376 | 위젯 key에 `toggle_counter` 포함 → "Select All" 한 번에 **모든 체크박스 key가 변경** → 전체 위젯 재생성 |
| B5 | 2089, 2097, 2105, 2113 | `@st.fragment`(2047) 안에서 맨 `st.rerun()` 호출 → fragment 스코프를 탈출해 **전체 앱 rerun**. `scope="fragment"` 누락 |
| B6 | 333 → 215, 그리고 353 | `convert_video`가 `get_codec_options`(내부에서 `get_video_info`) 호출 후 **353에서 또** `get_video_info`. 호출자 1473에서 한 번 더 → **파일당 ffprobe 프로세스 3회** |
| B7 | 1580 | 다운로드가 이미 끝난 뒤 `get_video_info(url)` = `yt-dlp --dump-json`을 유튜브에 재조회. **항목당 5~20초 순수 낭비** |
| B8 | 1487, 1630, 2156, 2476 | 변환/다운로드가 스크립트 런 안에서 동기 실행 → **작업 중 UI 전체 먹통** (탭 전환·중단 버튼 반응 없음) |

### C. 유튜브 링크 조회가 사용 불가 수준으로 느림
- `video_converter_app.py:28-43` `_fetch_video_title` → `yt-dlp --get-title`
- yt-dlp는 제목 하나를 위해 **extractor 전체를 실행**(webpage + player response + 포맷 목록 파싱). 최근 유튜브 대응 로직 때문에 5~20초.
- `--socket-timeout 15`, `timeout=30` → 최악의 경우 30초 블로킹.
- 2449에서 `st.spinner` 안에 **동기 블로킹** → 그 시간 내내 앱 정지.
- 대안: **YouTube oEmbed** (`https://www.youtube.com/oembed?url=...&format=json`) — 단순 HTTP GET, ~0.3s, `title` + `author_name` 반환. 실패 시(비공개/연령제한)에만 yt-dlp로 폴백.

### D. Mute 탭에 프로그레스 바가 없는 이유
- `video_converter_app.py:435` — `is_url`이면 `total_duration = 0`
- `video_converter_app.py:463` — `... and total_duration > 0` 가드 → **진행률 콜백이 한 번도 호출되지 않음**
- `video_converter_app.py:2549` — URL 분기는 애초에 `progress_callback=None`을 넘김
- 결과: URL 입력 시 `st.spinner`만 무한 회전. 중단 버튼도 없음.
- ffprobe는 HTTP 입력을 지원하므로 URL도 duration 취득 가능. 실패 시 "처리 시간 / 속도" 기반 표시로 폴백.

### E. 기타 UX 결함
| # | 위치 | 문제 |
|---|---|---|
| E1 | 2437-2443 | `Download Now`가 rerun 없이 인라인 실행 → 버튼이 disabled 되지 않아 **중복 클릭 가능** |
| E2 | 1783 | 변환 완료 **후에** `st.checkbox("Delete original", value=True)` 렌더 → 기본값 True가 즉시 평가되어 **원본이 묻지도 않고 삭제됨** |
| E3 | 1992, 2285 | `if 'selected_fps' not in dir():` — 의도대로 동작하지 않는 죽은 코드 |
| E4 | 1514, 1678, 1734, 1806 | 완료마다 `st.balloons()` — 배치 작업에서 소음 |
| E5 | 2534 | Mute 저장 경로가 `~/Downloads` 하드코딩. 다른 탭과 불일치, 폴더 선택 불가 |
| E6 | 1836 | "탭 전환하지 마세요" 경고 = B8의 증상을 문서로 덮은 것 |

---

## S1 탭 네비게이션 교체 (최우선) [DONE]
변경: `video_converter_app.py:1865`, 1869/2180/2480 블록 구조
- `st.tabs` → `st.session_state['active_tab']` + `st.segmented_control`(Streamlit 1.48.1 지원) 로 교체
- `with tabN:` → `if active_tab == N:` — **활성 탭만 렌더**
- 조건부 `st.warning`(1835)을 탭 스트립 **아래**로 이동
산출물: rerun 후에도 탭 유지 + 렌더 비용 1/3
검증: `python -m py_compile video_converter_app.py` → 앱 실행 후 탭2에서 Add to Queue / 테마 토글 → 탭2 유지 확인

## S2 유튜브 제목 조회 oEmbed 전환 [DONE]
변경: `video_converter_app.py:28-63` (`_fetch_video_title`), 1577-1583 (B7 제거)
- oEmbed HTTP GET 우선(`urllib`, timeout 4s) → 실패 시 yt-dlp 폴백(timeout 15s로 축소)
- 1580 다운로드 후 `--dump-json` 재조회 삭제 (이미 로컬 파일이 있으므로 ffprobe로 대체 또는 생략)
산출물: Add to Queue 응답 20s → <1s
검증: `python3 -c` 로 `_fetch_video_title` 직접 호출해 실제 URL 제목/소요시간 출력

## S3 Mute 실진행률 + 중단 [DONE]
변경: `video_converter_app.py:425-477` (`strip_audio`), 2542-2567
- URL도 ffprobe로 duration 시도 → 성공 시 실제 %, 실패 시 "처리 00:42 | 3.2x" 텍스트 폴백
- `progress_callback`을 URL 분기에도 전달, Stop 버튼 추가, 저장 폴더 선택 추가(E5)
산출물: URL/로컬 양쪽에서 진행 상황 가시화
검증: 로컬 파일 + 원격 URL 각 1회 실행해 진행률 갱신 확인

## S4 rerun/렌더 비용 절감 [DONE]
변경: 1259(CSS), 2047-2137(fragment), 2125/2376(key), 333/353(ffprobe 중복)
- B2: CSS + 폰트 링크를 `@st.cache_resource`/1회 삽입 가드로 분리
- B5: fragment 내 `st.rerun()` → `st.rerun(scope="fragment")`
- B4: `toggle_counter`를 key에서 제거하고 `st.session_state` 직접 대입 방식으로 변경
- B6: `convert_video`가 `get_video_info`를 1회만 호출하도록 결과 재사용 (+ 경로별 `@st.cache_data` 검토)
산출물: 파일 50개 기준 체크박스 클릭 응답 체감 개선, 변환 시작 지연 감소
검증: 폴더 로드 → 체크박스/정렬 클릭 반응, 변환 1건 실행 시 ffprobe 호출 횟수 로그 확인

## S5 UX 결함 수정 [DONE]
변경: 2437(E1), 1783(E2), 1992/2285(E3), balloons(E4), 1836(E6)
- E2는 **데이터 손실 위험** — 변환 전에 체크박스를 배치하도록 이동
- E1은 `st.rerun()` 후 실행 패턴으로 통일
산출물: 중복 클릭 불가, 원본 삭제는 사전 동의로만
검증: 각 시나리오 수동 1회

## EXIT
성공 기준:
1. 어떤 버튼을 눌러도 현재 탭이 유지된다
2. Add to Queue < 1초
3. Mute가 URL/로컬 모두에서 진행률을 표시한다
4. 파일 50개 목록에서 체크박스 클릭이 즉각 반응한다
최종 검증: `python -m py_compile video_converter_app.py` + `./run.command` 로 3개 탭 전 기능 수동 확인

리스크:
- `st.segmented_control` 교체 시 기존 탭 CSS(`.st-key-*`, `vt-tab-hero`) 셀렉터가 안 맞을 수 있음 → S1에서 스타일 확인 필수
- B8(동기 블로킹)은 근본 해결에 스레드+폴링 아키텍처 변경이 필요 → **이번 범위에서 제외**, S1의 "활성 탭만 렌더"로 증상 완화만
- oEmbed는 비공개/연령제한 영상에서 실패 → 폴백 경로 반드시 유지
- `video_converter_app.py.bak`(137KB)은 건드리지 않음


---

## 실행 결과 (2026-07-31)

구현 요약:
- S1: `st.tabs` → `st.radio(horizontal=True)` + CSS pill. **활성 섹션만 렌더**.
  `segmented_control`은 외형이 더 낫지만 streamlit.testing(AppTest)이 단일선택을
  직렬화하지 못해(`indices`가 문자열을 문자 단위로 순회) 회귀 테스트가 불가능했다.
  고치려는 버그를 자동 검증할 수 없으면 안 되므로 radio를 택했다.
- S2: oEmbed 1차 / yt-dlp `--print title` 폴백. 다운로드 후 `--dump-json` 재조회 삭제.
- S3: `probe_duration()` 신설(URL도 ffprobe), 콜백 시그니처 `(progress, processed, speed)`,
  길이 미확인 시 progress=None으로 불확정 표시. Stop 버튼 + 저장 폴더 선택 추가.
- S4: ffprobe 메모이즈((경로,mtime,size) 키), 체크박스 key 고정 + on_click 콜백,
  파일 행 위젯 수 축소(columns4+md3 → columns2+html1).
- S5: 원본 삭제 체크박스를 변환 '전'으로 이동(데이터 손실 수정), Download Now 중복 클릭 차단,
  URL 입력창 실제 초기화, 죽은 `dir()` 코드 제거, balloons 제거.
- 추가(계획 외, 필요해서): 작업 중 rerun 유발 컨트롤(섹션 전환/테마 토글) 잠금 +
  `finish_operation()`/`render_op_result()`로 완료 시 rerun해 잠금 해제 및 결과 표시.
  잠금만 넣고 완료 rerun이 없으면 작업 후 UI가 잠긴 채 남는 함정이 생긴다.
- `run.command:243` 매 실행 `pip install streamlit`(핀 없음) → 부재 시에만 설치 + requirements.txt 사용.

검증 (venv: python3.14 + streamlit 1.48.1, ffmpeg/ffprobe/yt-dlp는 homebrew):
| 항목 | 결과 |
|---|---|
| `python -m py_compile` | OK |
| AppTest UI 10항목 | ALL PASS — rerun 후 섹션 유지 STAYED=True (탭 튐 회귀 차단) |
| ffprobe/변환 1건 | **3회 → 1회** |
| oEmbed 제목 조회 | **0.20s** (캐시 시 0.0001s), 기존 yt-dlp 5~20s |
| Mute 로컬/URL 진행률 | 콜백 호출됨, progress 1.0 도달, 오디오 제거 확인 |
| Mute URL duration | 20.00s 취득 (기존 0 고정 → 진행률 없음) |
| Mute 전체 흐름 | 잠금→완료→rerun→결과 1회 표시→잠금 해제 PASS |

미해결 / 범위 외:
- **B8 동기 블로킹**: 변환/다운로드가 스크립트 런을 점유하는 구조는 그대로다.
  현재는 '작업 중 컨트롤 잠금'으로 증상을 막았을 뿐, 근본 해결은 스레드+폴링 전환 필요.
- **`VideoTool.app/Contents/Resources/video_converter_app.py`가 별도 복사본**이며
  이번 수정이 반영되지 않았다(git HEAD와 동일). .app으로 실행하면 구버전이 돈다.
- 버전 표기 불일치: `run.command`=6.0.5, 번들 런처=6.0.6, 앱=6.1.0(이번에 올림).
- Mute Stop 경로는 코드 검토로만 확인(테스트에서 작업이 중단 전에 완료됨).

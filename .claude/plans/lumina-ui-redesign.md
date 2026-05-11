# Lumina UI 디자인 적용 plan

`ui.html` 시안의 "Lumina Editor" 디자인 언어를 Streamlit 앱(`video_converter_app.py`)에 입히는 작업.

---

## 0. 시안 핵심 요약 (참고용)

- **컬러 시스템**: Material 3 토큰
  - light: `surface=#fcf8fb`, `primary=#0058bc`, `tertiary=#8a2bb9`, `outline=#717786`, `on-surface=#1b1b1d`
  - dark: 시안 후반부에 다크 톤 (`bg=#0f172a` 계열, slate-900/950 + glass)
- **타이포**: Inter (400/500/600/700/900) + Material Symbols Outlined
  - display-lg 48px, headline-md 24px, title-sm 18px, body-md 15px, label-caps 12px(700, uppercase)
  - mono-data 13px (queue 파일명/사이즈용)
- **글래스 패널**:
  ```css
  background: rgba(255,255,255,0.4);
  backdrop-filter: blur(20px) saturate(180%);
  border: 0.5px solid rgba(255,255,255,0.8);
  box-shadow: 0 10px 40px rgba(0,0,0,0.05);
  border-radius: 24px(12px도 혼용);
  ```
- **메시 배경**: 3개의 radial-gradient orb (top-left lavender / bottom-right pink / center blue), `position: fixed; z-index: -1`
- **레이아웃**:
  - 상단 64px TopNavBar (브랜드 "Lumina Editor" + nav links + 우측 아이콘들) — fixed
  - 좌측 80px SideNavBar (Media/Effects/Audio/Transitions/Color 아이콘 메뉴) — fixed
  - 메인: 상단 3개 stat 카드(grid-cols-3) + 하단 2컬럼(Config 4 / Queue 8)
  - 하단 32px Footer (status bar) — fixed

---

## 1. 적용 범위 결정 (가장 중요)

Streamlit은 fixed sidebar/topbar + 자체 네비게이션을 자유롭게 만들 수 없음. 세 가지 옵션:

### 옵션 A: 스타일만 입히기 (추천, 작업 ~1.5시간)
<!-- NOTE: 여기에 원하는 옵션 적어주세요 (A/B/C) -->
- 기존 Streamlit 탭 구조 유지
- glass-panel CSS, mesh 배경, Material 3 컬러 토큰, Inter 폰트, Material Symbols 아이콘 적용
- 헤더/스탯카드/패널을 Lumina 스타일로 리스킨
- **장점**: 안정적, 기존 기능 그대로
- **단점**: TopNav/SideNav 같은 외곽 레이아웃은 흉내 못 냄

### 옵션 B: 부분 레이아웃 모방 (작업 ~3시간)
- 옵션 A + 가짜 TopNavBar (HTML 마크다운으로 헤더 영역 재구성)
- Streamlit 탭을 SideNav 스타일 아이콘 버튼으로 교체 (`st.button` + 커스텀 CSS)
- 메인 콘텐츠는 `st.columns([4,8])`로 시안 2컬럼 흉내
- **장점**: 시안과 시각적으로 가장 가까움
- **단점**: Streamlit 동작 일부 어색해질 수 있음 (특히 사이드 네비)

### 옵션 C: 완전 모방 (비추, 작업 ~6시간+)
- iframe/components.html로 Lumina 레이아웃 통째로 렌더, JS로 Streamlit과 통신
- **단점**: 복잡도 폭증, 안정성 떨어짐

---

## 2. 작업 단계 (옵션 A 기준 — 옵션 변경 시 재작성)

### Step 1: 디자인 토큰 교체
<!-- NOTE:  -->
- `:root` / `[data-theme="dark"]`의 CSS 변수를 Material 3 컬러로 교체
  - `--bg-primary` → surface(`#fcf8fb`)
  - `--accent` → primary(`#0058bc`)
  - tertiary/outline/surface-variant 등 추가
- 폰트 가중치 추가 (현재 300~700 → 400~900)
- Material Symbols **Outlined** 추가 로드 (현재 Rounded만 있음)

### Step 2: 메시 배경 교체
<!-- NOTE:  -->
- 현재 `body::before/::after` 두 개의 블롭 → 시안의 3개 orb로 교체
- light: lavender(top-left) / pink(bottom-right) / blue(center) — `#d8e2ff`, `#f6d9ff`, `#adc6ff` 계열
- dark: slate-900 베이스 + 톤 다운된 같은 위치 orb

### Step 3: 글래스 패널 스타일 강화
<!-- NOTE:  -->
- 기존 `--glass-bg` rgba를 시안 값으로 교체 (0.4)
- border-radius 12~24px (현재 1.5rem=24px 이미 비슷)
- border 0.5px (시안 그대로) + 더 부드러운 shadow

### Step 4: 헤더 리디자인
<!-- NOTE:  -->
- 현재 `vt-header` (이모지 + 그라디언트 배지) → Lumina 헤더 스타일
- "Video Tool v6.0.2" → 시안의 큰 타이틀 톤 + 작은 서브타이틀
- 우측에 status indicator(디스크/메모리 같은 환경 정보)는 옵션

### Step 5: 스탯 카드 리디자인
<!-- NOTE:  -->
- 현재 `vt-stats-card` (작은 라벨 + 큰 숫자) → 시안 스타일 (좌상단 아이콘+라벨, 큰 숫자(`display-lg` 48px) + 옅은 단위)
- 카드 우상단 blur-orb 장식 추가

### Step 6: 큐 패널 리디자인
<!-- NOTE:  -->
- 시안의 12-column grid 헤더 (체크박스 / 파일명 / Format Conversion / Size / Status)
- 각 row에 hover 효과, mono-data 폰트로 사이즈 표시
- 작은 썸네일 슬롯은 비디오 파일이라 어려우므로 아이콘으로 대체
- Format pill (예: `MP4` → `H264`) 시안의 칩 스타일 차용

### Step 7: 버튼/입력 스타일
<!-- NOTE:  -->
- Primary 버튼: shadow-md + active:scale-[0.98] + Material Symbols 아이콘 결합
- Secondary 버튼: glass + border-white/40
- select / input: `bg-black/5 + border-white/40 + shadow-inner` 톤

### Step 8: 하단 status bar 리디자인
<!-- NOTE:  -->
- 현재 `vt-status-bar` (구분자 `|` 사용) → 시안 footer (좌측 코덱 정보, 우측 시스템 stat)
- mono 폰트 10px, slate-500 톤

### Step 9: YouTube 탭 다크 콘솔 톤 유지 여부
<!-- NOTE:  -->
- 현재 YouTube 탭만 진한 다크 그라디언트로 분리되어 있음
- 시안에서는 light/dark 모드 구분만 있고 탭별 분리는 없음 → 통일할지 유지할지 결정 필요

---

## 3. 손대지 않을 것

- 변환/다운로드 파이썬 로직(YouTubeDownloader, ffmpeg 호출 등)
- 세션 상태 키, 콜백, 큐 동작 방식
- `VideoTool.app/Contents/Resources/video_converter_app.py` (런처 번들 — 코드 안정화 후 sync)

---

## 4. 검증

- 양 테마(light/dark) 토글하여 둘 다 가독성 확인
- 3개 탭(Video Conversion / YouTube / Mute Video) 시각 일관성 확인
- 변환/다운로드 한 번씩 돌려서 기능 회귀 없는지 확인

---

## 질문 / 결정 필요

<!-- NOTE: 여기에 자유롭게 의견 적어주세요 -->

1. 옵션 A/B/C 중 어느 것?
2. "Lumina Editor"라는 브랜드 네임을 헤더에 노출할지, 아니면 "Video Tool" 유지할지?
3. 아이콘은 Material Symbols **Outlined**로 통일? (현재는 Rounded)
4. 다크 모드를 시안에서 본 만큼 그대로 따라갈지, 기존 GitHub 다크 톤(`#0d1117`) 유지할지?

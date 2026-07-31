"""CSS 무결성 테스트.

st.html()은 넘겨받은 내용을 HTML로 정제(sanitize)한다. 그래서 <style> 본문 안에
태그처럼 보이는 조각(꺾쇠 + 낱말)이 하나라도 있으면 style 블록이 통째로 제거되고
앱의 스타일이 전부 사라진다 — 화면이 완전히 깨지는데 파이썬 예외는 나지 않고
AppTest도 CSS를 평가하지 않으므로 다른 테스트로는 잡히지 않는다.

실제로 주석에 'st-key-KEY'를 꺾쇠로 감싸 쓴 것 때문에 한 번, 그 사실을 설명하는
주석에 링크 태그를 그대로 적어서 또 한 번 앱 전체 스타일이 죽었다.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "video_converter_app.py")

failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def extract_css():
    src = open(APP, encoding="utf-8").read()
    m = re.search(r'def get_app_css\(\):\s*"""[^"]*"""\s*return """(.*?)"""\n', src, re.S)
    if not m:
        return None
    return m.group(1)


def main():
    css = extract_css()
    check("get_app_css() 문자열을 찾음", css is not None)
    if css is None:
        return failures

    check("<style> 태그 존재", "<style>" in css and "</style>" in css)
    body = css[css.index("<style>") + len("<style>"): css.rindex("</style>")]

    # ★ 핵심: style 본문에 태그처럼 보이는 조각이 없어야 한다
    tagish = [(body[:m.start()].count("\n") + 1, body[max(0, m.start() - 60):m.start() + 40])
              for m in re.finditer(r"<[A-Za-z/!?]", body)]
    check("style 본문에 태그처럼 보이는 조각 없음", not tagish,
          "; ".join(f"line~{ln}: ...{seg.strip()[-70:]}" for ln, seg in tagish))

    # 문법 균형
    check("중괄호 균형", body.count("{") == body.count("}"),
          f"{{={body.count('{')} }}={body.count('}')}")
    check("주석 균형", len(re.findall(r"/\*", body)) == len(re.findall(r"\*/", body)),
          f"/*={len(re.findall(r'/[*]', body))} */={len(re.findall(r'[*]/', body))}")
    check("닫히지 않은 주석 없음", not re.search(r"/\*(?:(?!\*/).)*$", body, re.S))

    # 폰트는 링크 태그가 아니라 @import 로 불러와야 한다(링크 태그는 정제로 삭제됨)
    imports = re.findall(r"@import\s+url\(['\"]?([^'\")]+)", body)
    check("폰트를 @import 로 불러옴", len(imports) >= 1, str(imports))
    if imports:
        first_rule = re.search(r"[^\s/]", re.sub(r"/\*.*?\*/", "", body, flags=re.S))
        head = re.sub(r"/\*.*?\*/", "", body, flags=re.S).lstrip()
        check("@import 가 스타일시트 최상단에 위치", head.startswith("@import"),
              head[:40].replace("\n", " "))
        check("Material Symbols 폰트 포함",
              any("Material+Symbols" in u for u in imports), str(imports))

    # 아이콘 리거처를 쓰는 곳이 있으면 폰트 로드가 반드시 필요하다
    src = open(APP, encoding="utf-8").read()
    uses_ligatures = "material-symbols-outlined" in src
    check("아이콘 리거처 사용 시 폰트 로드 존재",
          (not uses_ligatures) or any("Material+Symbols" in u for u in imports))

    return failures


if __name__ == "__main__":
    fails = main()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'ALL PASS'}")
    sys.exit(1 if fails else 0)

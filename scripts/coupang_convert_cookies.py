"""
DevTools/Cookie-Editor에서 export한 쿠팡 쿠키 JSON을 Playwright storage_state로 변환.

입력: data/coupang_cookies_export.json
  허용 포맷:
    - [{"name", "value", "domain", "path", ...}, ...]  (DevTools Application 탭)
    - {"cookies": [...]}                                (Cookie-Editor JSON)
  추가 필드(expires/httpOnly/secure/sameSite)가 있으면 그대로 사용.

출력: data/coupang_session.json (Playwright storage_state)

누락 필드 기본값:
  - expires     : -1 (세션 쿠키)
  - httpOnly    : False
  - secure      : True (coupang.com은 HTTPS 고정)
  - sameSite    : "Lax"

사용처: modules/coupang_orderer.init_browser() 가 이 파일을 storage_state로 로드.
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
EXPORT_PATH = DATA_DIR / "coupang_cookies_export.json"
SESSION_PATH = DATA_DIR / "coupang_session.json"

# 이 중 하나라도 있으면 로그인 세션으로 간주
_LOGIN_MARKERS = {
    "SSID", "CUK", "MEMBER_ID",
    "rememberme", "member_srl", "ILOGIN", "sid", "sc_lid",
}


def _normalize_samesite(raw) -> str:
    if not raw:
        return "Lax"
    r = str(raw).strip().capitalize()
    if r in ("Unspecified", "No_restriction"):
        return "None"
    if r in ("Lax", "Strict", "None"):
        return r
    return "Lax"


def _to_playwright_cookie(raw: dict) -> dict:
    # expirationDate: Cookie-Editor, expires: DevTools, 없으면 세션 쿠키
    exp = raw.get("expirationDate")
    if exp is None:
        exp = raw.get("expires")
    if exp is None:
        exp = -1
    return {
        "name": raw["name"],
        "value": raw.get("value") or "",
        "domain": raw["domain"],
        "path": raw.get("path") or "/",
        "expires": float(exp),
        "httpOnly": bool(raw.get("httpOnly", False)),
        "secure": bool(raw.get("secure", True)),
        "sameSite": _normalize_samesite(raw.get("sameSite")),
    }


def main():
    print("=" * 60)
    print(" 쿠팡 쿠키 JSON → Playwright storage_state 변환")
    print("=" * 60)

    if not EXPORT_PATH.exists():
        print(f"\n[오류] 입력 파일 없음: {EXPORT_PATH}")
        print("       Chrome DevTools → Application → Storage → Cookies →")
        print("       coupang.com 에서 쿠키들을 JSON으로 export 후 이 경로에 저장하세요.")
        sys.exit(1)

    try:
        data = json.loads(EXPORT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[오류] JSON 파싱 실패: {e}")
        sys.exit(1)

    if isinstance(data, dict) and "cookies" in data:
        raw_cookies = data["cookies"]
    elif isinstance(data, list):
        raw_cookies = data
    else:
        print("[오류] 알 수 없는 포맷. 배열 또는 {\"cookies\": [...]} 형태여야 함.")
        sys.exit(1)

    cookies = []
    for c in raw_cookies:
        try:
            cookies.append(_to_playwright_cookie(c))
        except KeyError as e:
            print(f"[경고] 필수 필드 누락 (name={c.get('name')}): {e}")
        except Exception as e:
            print(f"[경고] 변환 실패 ({c.get('name')}): {e}")

    if not cookies:
        print("[오류] 변환된 쿠키가 없습니다.")
        sys.exit(1)

    names = {c["name"] for c in cookies}
    login_hits = names & _LOGIN_MARKERS

    state = {"cookies": cookies, "origins": []}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n쿠키 {len(cookies)}개 저장: {SESSION_PATH}")
    if login_hits:
        print(f"   로그인 마커 감지: {sorted(login_hits)}")
        print("   이제 python main.py --check 로 검증하세요.")
    else:
        print(f"   [경고] 로그인 마커 없음. 저장된 쿠키 일부: {sorted(names)[:10]}")
        print("          쿠팡 로그인 상태에서 쿠키를 다시 export 하세요.")


if __name__ == "__main__":
    main()

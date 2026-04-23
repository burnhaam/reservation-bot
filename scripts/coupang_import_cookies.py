"""
실제 Chrome의 쿠팡 로그인 쿠키를 읽어 Playwright storage_state로 변환.

Akamai 봇 챌린지가 patchright 등 자동화 Chromium을 막는 케이스를 우회하기 위해,
사용자가 평소 쓰는 Chrome에 이미 저장된 정상 로그인 세션을 그대로 가져와
data/coupang_session.json 에 저장한다. modules/coupang_orderer.init_browser()는
이 파일을 storage_state로 로드해 로그인 상태로 자동화를 시작한다.

사전 조건:
  1. 평소 쓰는 Chrome에서 쿠팡에 로그인되어 있을 것
  2. Chrome이 완전히 종료되어 있을 것 (SQLite Cookies DB 잠금 해제 필요)
     - 작업 관리자에서 chrome.exe 프로세스가 보이지 않아야 함
  3. browser_cookie3 설치 (pip install -r requirements.txt)

실행:
  python scripts/coupang_import_cookies.py [--profile "Default"]
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# ── 경로 설정 ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SESSION_PATH = DATA_DIR / "coupang_session.json"

# ── browser_cookie3 import ───────────────────────
try:
    import browser_cookie3
except ImportError:
    print("[오류] browser_cookie3 미설치.")
    print("       pip install -r requirements.txt 후 재시도하세요.")
    sys.exit(1)


# ── Playwright storage_state 변환 ────────────────

# 쿠팡 로그인 상태를 강하게 시사하는 쿠키 이름들.
# 전부 일치할 필요는 없고 하나라도 있으면 로그인된 것으로 간주.
_LOGIN_MARKERS = {"SSID", "CUK", "MEMBER_ID", "x-coupang-accessToken"}


def _samesite(cookie) -> str:
    """browser_cookie3 Cookie → Playwright sameSite 값 변환.

    Playwright는 "Strict" | "Lax" | "None" 만 허용. 알 수 없으면 "Lax".
    """
    rest = getattr(cookie, "_rest", None) or {}
    raw = (rest.get("SameSite") or rest.get("samesite") or "").strip().capitalize()
    if raw in ("Strict", "Lax", "None"):
        return raw
    return "Lax"


def _to_playwright_cookie(cookie) -> dict:
    """http.cookiejar.Cookie → Playwright cookie dict."""
    rest = getattr(cookie, "_rest", None) or {}
    # expires: None이면 세션 쿠키(-1), 정수면 Unix 타임스탬프
    expires = cookie.expires if cookie.expires else -1
    return {
        "name": cookie.name,
        "value": cookie.value or "",
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "expires": float(expires),
        "httpOnly": bool(rest.get("HttpOnly") or rest.get("httponly")),
        "secure": bool(cookie.secure),
        "sameSite": _samesite(cookie),
    }


def _load_coupang_cookies(profile: Optional[str]):
    """browser_cookie3로 Chrome 쿠키 jar 로드 후 coupang.com 도메인만 필터.

    profile이 지정되면 해당 프로필 폴더의 Cookies DB만 사용.
    """
    kwargs = {"domain_name": "coupang.com"}
    if profile:
        # 기본 경로: %LOCALAPPDATA%\Google\Chrome\User Data\<profile>\Network\Cookies
        import os
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if not local_appdata:
            print("[오류] %LOCALAPPDATA% 환경변수 없음.")
            sys.exit(1)
        candidates = [
            Path(local_appdata) / "Google" / "Chrome" / "User Data" / profile / "Network" / "Cookies",
            Path(local_appdata) / "Google" / "Chrome" / "User Data" / profile / "Cookies",
        ]
        cookie_file = next((str(p) for p in candidates if p.exists()), None)
        if not cookie_file:
            print(f"[오류] 프로필 '{profile}' 의 Cookies 파일을 찾을 수 없음.")
            print(f"       확인한 경로: {[str(p) for p in candidates]}")
            sys.exit(1)
        kwargs["cookie_file"] = cookie_file

    return browser_cookie3.chrome(**kwargs)


def main():
    parser = argparse.ArgumentParser(
        description="실제 Chrome에서 쿠팡 쿠키를 읽어 storage_state로 저장"
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Chrome 프로필 이름 (예: 'Default', 'Profile 1'). 생략 시 기본 프로필.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print(" 쿠팡 세션 쿠키 임포트 (실제 Chrome → storage_state)")
    print("=" * 60)
    print("\n[주의] Chrome이 완전히 종료되어 있어야 합니다.")
    print("       (Cookies DB 잠금 때문에 Chrome 실행 중이면 읽기 실패)")
    if args.profile:
        print(f" 프로필: {args.profile}")
    print()

    try:
        cj = _load_coupang_cookies(args.profile)
    except Exception as e:
        msg = str(e)
        print(f"[오류] Chrome 쿠키 읽기 실패: {msg}")
        print()
        if "database is locked" in msg.lower() or "could not" in msg.lower():
            print("  → Chrome이 아직 실행 중일 수 있습니다.")
            print("    작업 관리자에서 chrome.exe 전부 종료 후 재시도하세요.")
        elif "DPAPI" in msg or "decrypt" in msg.lower() or "encryption" in msg.lower():
            print("  → Chrome 127+ app-bound encryption 이슈 가능성.")
            print("    Chrome 설정 → 개인정보 → 쿠키 → 쿠팡만 별도 export 하거나,")
            print("    대안으로 scripts/coupang_login.py (patchright) 재시도.")
        sys.exit(1)

    cookies = []
    for c in cj:
        try:
            cookies.append(_to_playwright_cookie(c))
        except Exception:
            continue

    if not cookies:
        print("[오류] 쿠팡 도메인 쿠키를 하나도 찾지 못함.")
        print("       평소 Chrome에서 coupang.com에 로그인한 적이 있는지 확인하세요.")
        sys.exit(1)

    names = {c["name"] for c in cookies}
    has_login = bool(names & _LOGIN_MARKERS)

    state = {"cookies": cookies, "origins": []}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"쿠팡 쿠키 {len(cookies)}개 저장: {SESSION_PATH}")
    if has_login:
        print("   로그인 마커 쿠키 감지됨. python main.py --check 로 확인하세요.")
    else:
        print("   [경고] 로그인 마커 쿠키가 보이지 않습니다.")
        print("          평소 Chrome에서 쿠팡에 로그인한 뒤 다시 실행하세요.")
        print(f"          (저장된 쿠키 샘플: {sorted(names)[:10]})")


if __name__ == "__main__":
    main()

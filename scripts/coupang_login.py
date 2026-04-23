"""
쿠팡 로그인 세션 저장 스크립트 (patchright Chromium).

patchright(Playwright fork)는 Chromium 자동화 시그니처를 패치하여 Akamai
봇 탐지를 우회하므로, 실제 Chrome 바이너리나 별도 User Data 프로필은
필요 없다. 순수 launch() 방식으로 Chromium을 띄워 사용자가 수동 로그인하고,
그 결과를 storage_state로 백업한다.

로그인 후:
  - data/coupang_session.json     : storage_state (쿠키·localStorage)
modules/coupang_orderer.init_browser()가 이 파일을 storage_state로 로드해
세션을 재사용한다.

사전 조건:
  1. patchright 설치 (pip install -r requirements.txt)
     미설치 시 playwright로 자동 폴백되지만 봇 탐지 위험 존재.
"""
import json
import sys
from pathlib import Path

# ── 경로 설정 ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SESSION_PATH = DATA_DIR / "coupang_session.json"

# ── patchright 우선, 없으면 playwright 폴백 ────────
try:
    from patchright.sync_api import sync_playwright
    _USE_PATCHRIGHT = True
except ImportError:
    try:
        from playwright.sync_api import sync_playwright
        _USE_PATCHRIGHT = False
    except ImportError:
        print("[오류] patchright/playwright 모두 설치되지 않았습니다.")
        print("       pip install -r requirements.txt 후 재시도하세요.")
        sys.exit(1)

# ── stealth / 위장 스크립트 재사용 ─────────────────
sys.path.insert(0, str(PROJECT_ROOT))
try:
    from modules.coupang_orderer import _apply_stealth, _STEALTH_INIT_SCRIPT
except Exception:
    _STEALTH_INIT_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
    """

    def _apply_stealth(page):
        pass


# ── 메인 ───────────────────────────────────────────
def main():
    print("=" * 60)
    print(" 쿠팡 로그인 세션 저장 (patchright Chromium)")
    print("=" * 60)
    print(f" 엔진: {'patchright' if _USE_PATCHRIGHT else 'playwright(폴백)'}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = None
        context = None
        try:
            browser = p.chromium.launch(
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--ignore-certificate-errors",
                    "--ignore-ssl-errors",
                    "--allow-insecure-localhost",
                ],
                ignore_default_args=["--enable-automation"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                ignore_https_errors=True,
                extra_http_headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )

            page = context.new_page()
            try:
                page.add_init_script(_STEALTH_INIT_SCRIPT)
            except Exception:
                pass
            _apply_stealth(page)

            page.goto("https://www.coupang.com",
                      wait_until="domcontentloaded", timeout=60000)

            print("\n브라우저가 열렸습니다.")
            print("쿠팡 로그인 완료 후 마이쿠팡 이름이 보이면")
            print("이 터미널로 돌아와서 엔터를 눌러주세요.")
            input()

            state = context.storage_state()
            SESSION_PATH.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            cookies = state.get("cookies") or []
            print(f"\n✅ 쿠팡 세션 쿠키 {len(cookies)}개 저장 완료")
            print(f"   파일: {SESSION_PATH}")
            print("   이제 python main.py --check 를 실행하세요.")

        except Exception as e:
            print(f"\n[오류] {e}")
            sys.exit(1)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()

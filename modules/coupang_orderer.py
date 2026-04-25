"""
patchright Chromium 기반 쿠팡 장바구니 자동 담기 모듈.

쿠팡 Akamai는 Playwright 번들 Chromium의 자동화 시그니처를 감지하므로
patchright(Playwright fork)로 Chromium 자동화 지표를 런타임 패치하여 우회한다.
실제 Chrome 바이너리나 User Data 프로필은 필요 없으며, launch() 방식으로
순수 Chromium을 띄운다.

주요 함수:
- init_browser(): patchright Chromium launch로 봇 탐지 우회
- is_session_valid(): 마이쿠팡 접근으로 로그인 상태 확인
- detect_anti_bot(): 캡차/SMS/세션만료 페이지 감지
- add_single_item(): 1품목 장바구니 담기 (재시도 내장)
- add_items_to_cart(): 전체 품목 처리 (anti-bot 감지 시 중단)
- close_browser(): storage_state 백업 저장 후 context 종료
"""

import logging
import os
import random
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# patchright 우선, 미설치 시 playwright 폴백
try:
    from patchright.sync_api import sync_playwright, Error as PlaywrightError
    _USE_PATCHRIGHT = True
except ImportError:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    _USE_PATCHRIGHT = False

from modules.config_loader import load_config


logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SESSION_PATH = _PROJECT_ROOT / "data" / "coupang_session.json"
_LOG_DIR = _PROJECT_ROOT / "logs"

# navigator 자동화 시그니처 4종 위장. page.add_init_script로 문서 로드 전에
# 실행되어 쿠팡의 인라인 탐지 스크립트보다 먼저 적용된다.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['ko-KR', 'ko', 'en-US', 'en']
});
window.chrome = { runtime: {} };
"""

# 쿠팡 주요 URL
_MY_COUPANG_URL = "https://mc.coupang.com/ssr/desktop/order/list"
_LOGIN_URL = "https://login.coupang.com/login/login.pang"

# 캡차/SMS/세션 만료 감지용 selector 후보
_CAPTCHA_SELECTORS = [
    "iframe[src*='captcha']",
    "div.captcha",
    "text=자동입력방지",
    "text=보안문자",
]
_SMS_SELECTORS = [
    "input[id*='otp']",
    "input[name*='certification']",
    "text=휴대폰 인증",
    "text=SMS 인증",
]


# =============================================================
# 유틸
# =============================================================

def _delay(min_sec: float, max_sec: float) -> None:
    """지정 범위 내 랜덤 지연 (초 단위)."""
    if max_sec <= min_sec:
        time.sleep(max(0.0, min_sec))
        return
    time.sleep(random.uniform(min_sec, max_sec))


def _stock_cfg() -> dict:
    """config.json의 stock 섹션을 반환 (없으면 빈 dict)."""
    try:
        return load_config().get("stock", {}) or {}
    except Exception:
        logger.exception("[Coupang] config 로드 실패")
        return {}


def _playwright_cfg() -> dict:
    """stock.playwright 섹션을 반환 (기본값 포함)."""
    pw = _stock_cfg().get("playwright", {}) or {}
    return {
        "headless": bool(pw.get("headless", False)),
        "delay_min": float(pw.get("delay_min_sec", 2)),
        "delay_max": float(pw.get("delay_max_sec", 5)),
        "between_min": float(pw.get("between_items_min_sec", 5)),
        "between_max": float(pw.get("between_items_max_sec", 10)),
        "retry": int(pw.get("retry_count", 3)),
        "timeout_ms": int(pw.get("browser_timeout_sec", 30)) * 1000,
    }


def send_stock_alert_safe(message: str,
                          dedup_key: Optional[str] = None,
                          cooldown_hours: Optional[float] = None) -> None:
    """notifier.send_stock_alert를 실패해도 조용히 넘어가도록 감싼 헬퍼.

    dedup_key/cooldown_hours는 notifier.send_stock_alert에 그대로 전달되어
    동일 사유의 알림이 반복 발송되지 않도록 한다.
    """
    if not message:
        return
    try:
        from modules.notifier import send_stock_alert
        send_stock_alert(message, dedup_key=dedup_key, cooldown_hours=cooldown_hours)
    except Exception:
        # 알림 실패는 자동화 본체에 영향을 주면 안 됨
        pass


def _apply_stealth(page) -> None:
    """Playwright 페이지에 stealth 패치 적용. v2.x 우선, v1.x 폴백.

    - playwright-stealth 2.x: Stealth().apply_stealth_sync(page)
    - playwright-stealth 1.x: stealth_sync(page) (deprecated)
    둘 다 실패하면 사장님에게 카카오 경고를 보내되 파이프라인은 계속 진행.
    """
    # v2.x API (권장)
    try:
        from playwright_stealth import Stealth
        Stealth().apply_stealth_sync(page)
        logger.info("[Coupang] stealth 2.x 적용 완료")
        return
    except ImportError:
        pass
    except Exception:
        logger.exception("[Coupang] stealth 2.x 적용 실패, v1.x 시도")

    # v1.x API (폴백)
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
        logger.info("[Coupang] stealth 1.x 적용 완료")
        return
    except ImportError:
        logger.error("[Coupang] playwright-stealth 미설치 — 봇 탐지 위험!")
        send_stock_alert_safe(
            "[경고] playwright-stealth 미설치 상태로 쿠팡 접근",
            dedup_key="stealth_missing",
            cooldown_hours=None,
        )
    except Exception:
        logger.exception("[Coupang] stealth 1.x 적용도 실패")


# =============================================================
# 브라우저 수명주기
# =============================================================

# Chromium launch 시의 CLI 플래그. AutomationControlled 제거로 navigator.webdriver
# 기본 탐지 우회, 나머지는 리소스 최소화와 안정성 목적.
# headless는 봇 탐지 위험이 커 config(기본 False) 그대로 둔다.
_BROWSER_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-infobars",
    "--window-size=1280,800",
]

# 봇 탐지와 무관하면서 페이지 로딩 비용이 큰 정적 리소스 패턴.
# 쿠팡 상품 상세 페이지에서 이미지/폰트 전부 차단해도 가격/버튼 추출은 정상.
_BLOCKED_RESOURCE_PATTERN = "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf}"

# CDP 모드: 사용자가 띄운 실제 Chrome에 attach해서 그 지문/쿠키를 그대로 이용.
# Akamai _abck는 디바이스 지문에 묶이므로 patchright 자체 launch로는 챌린지 통과 불가.
# scripts/coupang_chrome_cdp_start.py로 Chrome을 켜고 로그인해두면 여기 attach.
_CDP_PORT = 9222
_CDP_URL = f"http://localhost:{_CDP_PORT}"

# init_browser 호출 시 CDP attach 성공 여부. close_browser가 사용자 Chrome을
# 닫지 않도록 분기하는 데 쓴다.
_CDP_ATTACHED = False


def _is_cdp_available(timeout: float = 0.5) -> bool:
    """CDP 포트(9222)가 응답하는지 TCP 레벨에서 확인. init 전에 싸게 체크."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", _CDP_PORT))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _try_cdp_attach(p):
    """CDP 포트에 attach 시도. 성공 시 (browser, context, page), 실패 시 None.

    기존 사용자 Chrome 컨텍스트(쿠키 포함)에 새 페이지 하나만 추가한다.
    기존 탭은 절대 건드리지 않으며 close_browser에서도 page만 닫는다.
    """
    try:
        browser = p.chromium.connect_over_cdp(_CDP_URL, timeout=5000)
    except Exception:
        return None

    contexts = browser.contexts
    if not contexts:
        # CDP attach인데 컨텍스트가 없는 비정상 케이스 — 새로 만든다(쿠키 없음)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            ignore_https_errors=True,
        )
    else:
        context = contexts[0]
    page = context.new_page()
    return browser, context, page


def init_browser() -> tuple:
    """브라우저 시작. CDP attach(권장) 우선, 실패 시 patchright launch 폴백.

    반환: (playwright, browser, context, page)
    """
    global _CDP_ATTACHED
    _CDP_ATTACHED = False

    p = sync_playwright().start()

    # 1순위: 사용자 Chrome에 CDP attach (Akamai 통과 가능성 ↑)
    cdp_result = _try_cdp_attach(p)
    if cdp_result is not None:
        browser, context, page = cdp_result
        _CDP_ATTACHED = True
        cfg = _playwright_cfg()
        try:
            context.set_default_timeout(cfg["timeout_ms"])
        except Exception:
            pass
        try:
            page.add_init_script(_STEALTH_INIT_SCRIPT)
        except Exception:
            logger.exception("[Coupang] stealth init script 주입 실패 (계속 진행)")
        _apply_stealth(page)
        logger.info("[Coupang] CDP attach 성공 (port %d) — 실제 Chrome 사용", _CDP_PORT)
        return p, browser, context, page

    # 2순위: patchright launch 폴백 (CDP Chrome이 안 떠있을 때)
    logger.info("[Coupang] CDP attach 실패 — patchright launch 폴백 (port %d)", _CDP_PORT)
    cfg = _playwright_cfg()
    try:
        browser = p.chromium.launch(
            headless=cfg["headless"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--ignore-certificate-errors",
                "--ignore-ssl-errors",
                "--allow-insecure-localhost",
            ],
            ignore_default_args=["--enable-automation"],
        )
    except Exception:
        try:
            p.stop()
        except Exception:
            pass
        raise

    context = browser.new_context(
        storage_state=str(_SESSION_PATH) if _SESSION_PATH.exists() else None,
        viewport={"width": 1280, "height": 800},
        locale="ko-KR",
        timezone_id="Asia/Seoul",
        ignore_https_errors=True,
        extra_http_headers={
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    context.set_default_timeout(cfg["timeout_ms"])

    # 리소스 차단 route는 로그인/세션 확인이 끝난 뒤(add_items_to_cart 내부)에 등록.

    page = context.new_page()
    try:
        page.add_init_script(_STEALTH_INIT_SCRIPT)
    except Exception:
        logger.exception("[Coupang] stealth init script 주입 실패 (계속 진행)")

    _apply_stealth(page)
    logger.info("[Coupang] patchright=%s 브라우저 launch 완료", _USE_PATCHRIGHT)
    return p, browser, context, page


def close_browser(p, browser, context, page=None) -> None:
    """세션 저장 후 브라우저 종료. CDP attach 모드면 사용자 Chrome은 보존.

    page를 받으면 그 page만 닫는다 (CDP 모드 안전성).
    """
    # CDP attach 모드: 우리가 연 page만 닫고, 사용자 Chrome/context는 절대 건드리지 않음.
    if _CDP_ATTACHED:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        try:
            p.stop()  # CDP 클라이언트 연결만 해제 (Chrome 본체는 유지)
        except Exception:
            pass
        return

    # launch 모드: 기존 동작 — storage_state 백업 후 전체 닫기
    try:
        context.storage_state(path=str(_SESSION_PATH))
        logger.info("[Coupang] 세션 저장 완료: %s", _SESSION_PATH)
    except Exception:
        logger.warning("[Coupang] 세션 저장 실패", exc_info=True)
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
    for obj in (context, browser):
        try:
            obj.close()
        except Exception:
            pass
    try:
        p.stop()
    except Exception:
        pass


# =============================================================
# 세션 / Anti-bot 감지
# =============================================================

def is_session_valid(page) -> bool:
    """마이쿠팡 페이지 접속 → 로그인 상태 확인. 로그인 페이지로 리다이렉트되면 False."""
    try:
        page.goto(_MY_COUPANG_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        logger.exception("[Coupang] 마이쿠팡 접속 실패")
        return False

    current_url = page.url or ""
    if "login" in current_url.lower():
        logger.warning("[Coupang] 세션 만료 감지 (로그인 페이지로 리다이렉트)")
        return False

    return True


def detect_anti_bot(page) -> Optional[str]:
    """캡차/SMS/세션만료/Akamai 페이지 감지 후 사유 문자열 반환.

    감지 결과:
      "captcha" | "sms" | "session_expired" |
      "akamai_challenge" | "akamai_blocked" | None
    예외 발생 시 None (감지 실패를 안전 쪽으로 처리).
    """
    try:
        current_url = (page.url or "").lower()
        if "login" in current_url:
            return "session_expired"

        # Akamai behavioral challenge 페이지 (sec-if-cpt-container div)
        try:
            if page.locator("#sec-if-cpt-container").count() > 0:
                return "akamai_challenge"
        except Exception:
            pass

        # Akamai Access Denied 정적 페이지 (Reference #..., errors.edgesuite.net 링크)
        try:
            body_text = (page.inner_text("body", timeout=2000) or "").lower()
            if "access denied" in body_text and (
                "errors.edgesuite.net" in body_text or "reference #" in body_text
            ):
                return "akamai_blocked"
        except Exception:
            pass

        for sel in _CAPTCHA_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    return "captcha"
            except Exception:
                continue

        for sel in _SMS_SELECTORS:
            try:
                if page.locator(sel).count() > 0:
                    return "sms"
            except Exception:
                continue
    except Exception:
        logger.exception("[Coupang] anti-bot 감지 중 예외")
        return None

    return None


def _wait_for_akamai_challenge_clear(page, max_sec: int = 25) -> bool:
    """Akamai behavioral challenge 페이지(#sec-if-cpt-container)가 떠 있으면
    챌린지 JS가 location.reload(true)로 실제 페이지를 다시 로드할 때까지 대기.

    반환: True=챌린지 없음 또는 통과됨 / False=max_sec 내에 해결 안 됨.
    챌린지가 아예 없으면 즉시 True.
    """
    # 챌린지 컨테이너가 없으면 바로 OK (정상 페이지 케이스)
    try:
        if page.locator("#sec-if-cpt-container").count() == 0:
            return True
    except Exception:
        return True  # locator 자체 실패는 챌린지 없음으로 간주

    logger.info("[Coupang] Akamai 챌린지 감지 — 자동 통과 대기 (최대 %ds)", max_sec)
    for i in range(max_sec):
        try:
            still = page.locator("#sec-if-cpt-container").count() > 0
        except Exception:
            still = False
        if not still:
            # 챌린지 컨테이너 사라짐 = 통과 후 reload 완료
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            logger.info("[Coupang] Akamai 챌린지 통과 (%ds 소요)", i + 1)
            return True
        time.sleep(1)

    logger.warning("[Coupang] Akamai 챌린지 %ds 경과해도 해결 안 됨 — 스킵", max_sec)
    return False


def _screenshot_on_error(page, item_name: str) -> None:
    """실패 시 logs/coupang_error_{품목}_{시각}.png로 스크린샷 저장."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch for ch in item_name if ch.isalnum() or ch in "-_") or "item"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _LOG_DIR / f"coupang_error_{safe_name}_{ts}.png"
        page.screenshot(path=str(path), full_page=True)
        logger.info("[Coupang] 오류 스크린샷 저장: %s", path)
    except Exception:
        logger.exception("[Coupang] 스크린샷 저장 실패")


# =============================================================
# 단일 품목 처리
# =============================================================

def _extract_current_price(page) -> Optional[int]:
    """상품 상세 페이지에서 현재 가격(할인가)을 원 단위 정수로 추출 (실패 시 None).

    2026년 4월 기준 쿠팡 vp 페이지 DOM:
      - 할인 적용가: div.price-amount.final-price-amount (예: '3,490원')
      - 정가/취소선: div.price-amount.original-price-amount  (제외)
      - 단위가: div.final-unit-price (예: '10g당 119원', 제외)
      - 옵션 선택가: div.option-table-list__option--selected div.option-table-list__option-price
    """
    selectors = [
        # 1순위: 최종 결제가 (할인 적용 후)
        "div.price-amount.final-price-amount",
        "div.final-price div.price-amount",
        "div.price-container div.final-price div.price-amount",
        # 2순위: 다중 옵션 상품에서 선택된 옵션의 가격
        "div.option-table-list__option--selected div.option-table-list__option-price",
        # 폴백: 구버전 DOM (혹시 다른 카테고리 페이지가 다를 경우)
        "span.total-price strong",
        "strong.price-amount",
        "span.price-value",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            text = loc.inner_text(timeout=5000) or ""
            # "3,490원" / "3490 원" / "3,490" 모두 처리: 콤마/공백/원 제거 후 숫자만
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits:
                return int(digits)
        except Exception:
            continue
    return None


def _is_sold_out(page) -> bool:
    """품절 문구가 페이지에 있으면 True."""
    try:
        for text in ("품절", "일시품절", "재고없음"):
            if page.locator(f"text={text}").count() > 0:
                return True
    except Exception:
        pass
    return False


def _click_add_to_cart(page, cfg: dict) -> bool:
    """장바구니 담기 버튼 클릭. 성공 여부 반환."""
    selectors = [
        "button.prod-cart-btn",
        "button[class*='cart-btn']",
        "button:has-text('장바구니')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            # 버튼 클릭 전에 대기 가능 상태 확인 (타임아웃 10초)
            try:
                btn.wait_for(state="visible", timeout=10000)
            except Exception:
                continue
            btn.click(timeout=5000)
            _delay(cfg["delay_min"], cfg["delay_max"])
            return True
        except Exception:
            continue
    return False


def add_single_item(page, item: dict) -> dict:
    """단일 품목을 장바구니에 담는다 (재시도 내장).

    입력: {"item_name", "url", "quantity", "max_price"}
    출력 (성공): {"status": "ordered", "price": int}
    출력 (스킵): {"status": "skipped", "reason": str}
    출력 (실패): {"status": "failed", "reason": str}
    """
    item_name = item.get("item_name", "?")
    url = item.get("url", "")
    max_price = int(item.get("max_price") or 0)

    if not url:
        return {"status": "failed", "reason": "URL 없음"}

    cfg = _playwright_cfg()
    last_error = "이유 불명"

    for attempt in range(1, cfg["retry"] + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Akamai 챌린지가 떠 있으면 최대 25초 자동 통과 대기.
            # 실제 Chrome 지문 + 정상 쿠키면 보통 3~10초 내 location.reload 로 해결됨.
            if not _wait_for_akamai_challenge_clear(page, max_sec=25):
                last_error = "Akamai 챌린지 25초 내 해결 안 됨"
                _screenshot_on_error(page, item_name)
                _delay(cfg["delay_min"], cfg["delay_max"])
                continue  # 이 attempt는 포기, 재시도 루프로

            _delay(cfg["delay_min"], cfg["delay_max"])

            # Anti-bot 감지 (captcha / sms / session_expired / akamai_blocked)
            bot_reason = detect_anti_bot(page)
            if bot_reason:
                return {"status": "failed", "reason": f"anti-bot: {bot_reason}"}

            # 품절 확인. 주문내역에 같은 이름 유사 상품이 있으면 대체 시도 (1회만).
            if _is_sold_out(page):
                if not item.get("_sold_out_fallback_tried"):
                    item["_sold_out_fallback_tried"] = True
                    try:
                        from modules.product_matcher import match_from_order_history
                        alt = match_from_order_history(item_name, page)
                    except Exception:
                        logger.exception("[Coupang] 품절 대체 검색 중 예외: %s", item_name)
                        alt = None
                    if alt and alt.get("url") and alt["url"] != url:
                        logger.info("[Coupang] 품절 → 주문내역 대체: %s → %s", item_name, alt["url"])
                        url = alt["url"]
                        max_price = 0  # 대체 URL은 가격 가드 비활성
                        # 다음 attempt에서 새 URL로 goto (루프 계속)
                        continue
                return {"status": "skipped", "reason": "품절"}

            # 가격 추출 (로깅/알림용). 가격 초과 차단은 사용자 정책상 비활성.
            current_price = _extract_current_price(page)
            if current_price is None:
                last_error = "가격 추출 실패"

            # 장바구니 담기 클릭
            if not _click_add_to_cart(page, cfg):
                last_error = "장바구니 버튼 클릭 실패"
                _screenshot_on_error(page, item_name)
                continue

            logger.info("[Coupang] 장바구니 담기 OK — %s (%s원)",
                        item_name, current_price if current_price else "?")
            return {
                "status": "ordered",
                "price": current_price if current_price is not None else 0,
            }
        except Exception as e:
            last_error = str(e)
            logger.exception("[Coupang] 담기 시도 %d/%d 실패: %s",
                             attempt, cfg["retry"], item_name)
            _screenshot_on_error(page, item_name)
            _delay(cfg["delay_min"], cfg["delay_max"])

    return {"status": "failed", "reason": f"재시도 {cfg['retry']}회 모두 실패: {last_error}"}


# =============================================================
# 전체 품목 처리
# =============================================================

def add_items_to_cart(items: list[dict]) -> dict:
    """여러 품목을 순차적으로 장바구니에 담고 통계를 반환.

    입력: [{"item_name", "url", "quantity", "max_price"}, ...]
    출력: {
        "success": [ {"item_name", "quantity", "price", ...} ],
        "skipped": [ {"item_name", "reason"} ],
        "failed":  [ {"item_name", "reason"} ],
        "stopped": bool,
        "stop_reason": str
    }
    anti-bot(캡차/SMS/세션만료) 감지 시 즉시 중단하고 stopped=True로 반환한다.
    Playwright 브라우저는 finally에서 반드시 종료된다.
    """
    result = {
        "success": [],
        "skipped": [],
        "failed": [],
        "unmapped": [],  # 매핑표 + 주문내역 모두 없는 진짜 미매핑
        "stopped": False,
        "stop_reason": "",
    }

    if not items:
        return result

    # 운영 가드: CDP 모드 Chrome(port 9222)이 떠 있지 않으면 patchright 폴백으로
    # 가봤자 Akamai에 막힌다. 자동주문을 포기하고 사용자에게 수동 주문 알림만 발송.
    if not _is_cdp_available():
        logger.error("[Coupang] CDP Chrome 미실행 — 자동주문 중단, 알림 전환")
        names = ", ".join((it.get("item_name", "?") for it in items[:5]))
        if len(items) > 5:
            names += f" 외 {len(items) - 5}건"
        send_stock_alert_safe(
            f"[수동주문필요] CDP Chrome(port 9222) 미실행 — 자동주문 스킵. "
            f"품목: {names}. "
            f"scripts/coupang_chrome_cdp_start.py 실행 후 재시도하세요.",
            dedup_key="cdp_unavailable",
            cooldown_hours=None,
        )
        # 각 품목을 failed로 마킹 + 수동주문 안내
        for it in items:
            result["failed"].append({
                "item_name": it.get("item_name", "?"),
                "reason": "cdp_unavailable — 수동주문 필요",
            })
        result["stopped"] = True
        result["stop_reason"] = "cdp_unavailable"
        return result

    cfg = _playwright_cfg()

    playwright_obj, browser, context, page = None, None, None, None
    try:
        playwright_obj, browser, context, page = init_browser()

        # 세션 유효성 사전 확인
        if not is_session_valid(page):
            result["stopped"] = True
            result["stop_reason"] = "session_expired"
            logger.error("[Coupang] 세션 무효 — 쿠키 재임포트 필요")
            # 즉시 카카오 알림 (영구 1회 — 같은 만료 사유로 재발송 안 됨)
            send_stock_alert_safe(
                "[쿠팡 세션 만료] 자동주문 중단됨.\n"
                "Chrome에서 쿠팡 재로그인 → DevTools에서 쿠키 export →\n"
                "data/coupang_cookies_export.json 갱신 →\n"
                "python scripts/coupang_convert_cookies.py 실행으로 복구하세요.",
                dedup_key="coupang_session_expired",
                cooldown_hours=None,
            )
            return result

        # 장바구니 담기 시작 전 리소스 차단 (속도 향상).
        # 로그인/세션 확인 구간에는 등록하지 않음 — 로그인 UI의 로딩 간섭 방지.
        try:
            context.route(_BLOCKED_RESOURCE_PATTERN, lambda route: route.abort())
        except Exception:
            logger.warning("[Coupang] 리소스 차단 등록 실패", exc_info=True)

        for idx, item in enumerate(items, 1):
            name = item.get("item_name", "?")

            # 페이지가 닫혔다면 새 페이지로 복구 시도 (크래시/타임아웃 직후 대비)
            try:
                if page.is_closed():
                    logger.warning("[Coupang] 페이지 닫힘 감지 — 새 페이지 생성 시도")
                    try:
                        page = context.new_page()
                        _apply_stealth(page)
                    except Exception:
                        logger.exception("[Coupang] 새 페이지 생성 실패 — 전체 중단")
                        result["stopped"] = True
                        result["stop_reason"] = "page_recreation_failed"
                        send_stock_alert_safe(
                            "[긴급] Playwright 페이지 재생성 실패 — 재시작 필요",
                            dedup_key="page_recreation_failed",
                            cooldown_hours=None,
                        )
                        break
            except Exception:
                # is_closed() 호출 자체가 실패하면 컨텍스트 크래시로 간주
                logger.exception("[Coupang] 페이지 상태 확인 실패 — 컨텍스트 크래시")
                result["stopped"] = True
                result["stop_reason"] = "context_crashed"
                send_stock_alert_safe(
                    "[긴급] Playwright 컨텍스트 크래시 — 재시작 필요",
                    dedup_key="context_crashed",
                    cooldown_hours=None,
                )
                break

            # URL 없는 품목(매핑표 매칭 실패)은 주문내역 검색으로 대체 URL 탐색.
            # 찾으면 item["url"]에 세팅하고 정상 플로우 진행, 못 찾으면 unmapped로 분류.
            if not item.get("url"):
                try:
                    from modules.product_matcher import match_from_order_history
                    alt = match_from_order_history(name, page)
                except Exception:
                    logger.exception("[Coupang] 주문내역 검색 중 예외: %s", name)
                    alt = None
                if alt and alt.get("url"):
                    logger.info("[Coupang] 매핑 미등록 → 주문내역 매치: %s → %s", name, alt["url"])
                    item["url"] = alt["url"]
                    # 주문내역 기반이라 max_price 없음 — 가격 가드 비활성
                    item["max_price"] = 0
                    item["source"] = "order_history"
                else:
                    logger.info("[Coupang] 매핑표 + 주문내역 모두 없음: %s", name)
                    result["unmapped"].append({"item_name": name})
                    continue

            try:
                single = add_single_item(page, item)
            except PlaywrightError as e:
                err_msg = str(e)
                # Target closed / Browser closed 등 치명적 크래시는 즉시 중단
                if "Target closed" in err_msg or "closed" in err_msg.lower():
                    logger.exception("[Coupang] Playwright 크래시 감지: %s", name)
                    result["failed"].append({"item_name": name, "reason": err_msg})
                    result["stopped"] = True
                    result["stop_reason"] = "context_crashed"
                    send_stock_alert_safe(
                        "[긴급] Playwright 컨텍스트 크래시 — 재시작 필요",
                        dedup_key="context_crashed",
                        cooldown_hours=None,
                    )
                    break
                logger.exception("[Coupang] Playwright 오류: %s", name)
                result["failed"].append({"item_name": name, "reason": err_msg})
                continue
            except Exception as e:
                logger.exception("[Coupang] 품목 처리 중 예외: %s", name)
                result["failed"].append({"item_name": name, "reason": str(e)})
                continue

            status = single.get("status")
            if status == "ordered":
                result["success"].append({
                    "item_name": name,
                    "quantity": item.get("quantity", 1),
                    "price": single.get("price", 0),
                    "url": item.get("url", ""),
                })
            elif status == "skipped":
                result["skipped"].append({
                    "item_name": name,
                    "reason": single.get("reason", "스킵"),
                })
            else:
                reason = single.get("reason", "실패")
                result["failed"].append({"item_name": name, "reason": reason})
                # anti-bot 감지 시 유형별 처리:
                # - captcha / sms / session_expired / akamai_blocked : 사람 개입 필요 → 전체 중단
                # - akamai_challenge : 일시적 챌린지 → 다른 품목으로 계속 진행
                if reason.startswith("anti-bot"):
                    fatal_reasons = ("captcha", "sms", "session_expired", "akamai_blocked")
                    if any(r in reason for r in fatal_reasons):
                        result["stopped"] = True
                        result["stop_reason"] = reason
                        logger.error("[Coupang] 치명적 anti-bot 감지 → 즉시 중단: %s", reason)
                        break
                    else:
                        logger.warning("[Coupang] 일시적 anti-bot (%s) → 이 품목만 스킵, 계속 진행", reason)

            # 품목 간 대기 (마지막 제외)
            if idx < len(items):
                _delay(cfg["between_min"], cfg["between_max"])
    except Exception as e:
        logger.exception("[Coupang] 장바구니 처리 중 전역 예외")
        result["stopped"] = True
        result["stop_reason"] = f"예외: {e}"
    finally:
        close_browser(playwright_obj, browser, context, page)

    return result

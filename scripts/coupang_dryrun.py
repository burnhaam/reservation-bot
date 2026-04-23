"""
쿠팡 세션/Akamai 통과 드라이런 테스트.

장바구니 담기 버튼을 누르지 않고 다음만 검증:
  1) init_browser() - patchright Chromium 기동 + storage_state 로드
  2) is_session_valid() - 마이쿠팡 접근 (로그인 쿠키 유효성)
  3) 상품 상세 페이지 goto (Akamai 통과 여부)
  4) detect_anti_bot() - 캡차/SMS/세션만료 감지
  5) _extract_current_price() - 가격 추출 가능 여부

운영 영향 없음 (카트 담기 호출 안 함).

사용:
  python scripts/coupang_dryrun.py                           # 기본 샘플 URL 사용
  python scripts/coupang_dryrun.py <상품URL>                 # 특정 URL 테스트
  python scripts/coupang_dryrun.py --key "곰곰 쌀과자 고소한맛"  # 매핑 키로 조회
"""
import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 기본 테스트 URL (최근주문일 2026-04-21 매핑 중 하나)
_DEFAULT_URL = "https://www.coupang.com/vp/products/8304592330"


def _load_mapped_url(key: str) -> str | None:
    mapping_path = PROJECT_ROOT / "data" / "product_mapping.json"
    data = json.loads(mapping_path.read_text(encoding="utf-8"))
    item = data.get(key)
    if isinstance(item, dict):
        return item.get("url")
    return None


def main():
    parser = argparse.ArgumentParser(description="쿠팡 세션 드라이런 테스트")
    parser.add_argument("url", nargs="?", default=None, help="테스트할 상품 URL")
    parser.add_argument("--key", default=None, help="product_mapping.json의 키로 URL 조회")
    args = parser.parse_args()

    url = args.url
    if not url and args.key:
        url = _load_mapped_url(args.key)
        if not url:
            print(f"[오류] 매핑에 '{args.key}' 없음")
            sys.exit(1)
    if not url:
        url = _DEFAULT_URL

    print("=" * 60)
    print(" 쿠팡 드라이런 테스트 (장바구니 담기 호출 안 함)")
    print("=" * 60)
    print(f" 대상 URL: {url}\n")

    from modules.coupang_orderer import (
        init_browser,
        close_browser,
        is_session_valid,
        detect_anti_bot,
        _extract_current_price,
        _is_sold_out,
        _USE_PATCHRIGHT,
    )

    print(f"[1/6] 브라우저 기동 (patchright={_USE_PATCHRIGHT})...")
    t0 = time.time()
    p, browser, context, page = init_browser()
    print(f"      OK ({time.time() - t0:.1f}s)\n")

    try:
        print("[2/6] 세션 유효성 확인 (마이쿠팡 접근)...")
        t0 = time.time()
        valid = is_session_valid(page)
        print(f"      결과: {'OK' if valid else 'FAIL (로그인 필요)'} ({time.time() - t0:.1f}s)")
        print(f"      현재 URL: {page.url}\n")
        if not valid:
            print("[중단] 세션 무효 — data/coupang_session.json 재갱신 필요")
            return

        print("[3/6] 상품 상세 페이지 접속...")
        t0 = time.time()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"      OK ({time.time() - t0:.1f}s)")
            print(f"      현재 URL: {page.url}\n")
        except Exception as e:
            print(f"      FAIL: {e}\n")
            return

        print("[4/6] Anti-bot 감지...")
        bot = detect_anti_bot(page)
        if bot:
            print(f"      [WARN] 감지됨: {bot}")
        else:
            print("      [OK]   없음 (Akamai 통과)")
        print()

        print("[5/6] 가격/품절 확인...")
        sold_out = _is_sold_out(page)
        price = _extract_current_price(page)
        print(f"      품절여부: {'품절' if sold_out else '판매중'}")
        print(f"      추출가격: {f'{price:,}원' if price is not None else '실패'}")
        print()

        print("[6/6] 장바구니 버튼 셀렉터 점검 (클릭 안 함)...")
        # _click_add_to_cart의 셀렉터들이 현재 페이지에서 정상 발견되는지 확인
        button_selectors = [
            "button.prod-cart-btn",
            "button[class*='cart-btn']",
            "button:has-text('장바구니')",
        ]
        button_found = None
        for sel in button_selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    print(f"      [MISS] {sel}")
                    continue
                visible = loc.is_visible(timeout=1000)
                disabled = loc.is_disabled(timeout=1000)
                text = (loc.inner_text(timeout=1000) or "").strip()[:20]
                state = []
                if visible: state.append("visible")
                if disabled: state.append("disabled")
                print(f"      [HIT]  {sel}  text='{text}' [{','.join(state)}]")
                if visible and not disabled and button_found is None:
                    button_found = sel
            except Exception as e:
                print(f"      [ERR]  {sel}  ({e})")
        if button_found:
            print(f"      => 첫 클릭 가능 셀렉터: {button_found}")
        else:
            print("      [WARN] 클릭 가능한 장바구니 버튼을 찾지 못함")
        print()

        # 종합 판정
        ok = valid and not bot and not sold_out and price is not None and button_found
        print("=" * 60)
        if ok:
            print(" [PASS] 드라이런 통과 — 자동화 실행 준비 완료")
        else:
            reasons = []
            if not valid: reasons.append("세션무효")
            if bot: reasons.append(f"anti-bot:{bot}")
            if sold_out: reasons.append("품절")
            if price is None: reasons.append("가격추출실패")
            if not button_found: reasons.append("카트버튼없음")
            print(f" [WARN] 드라이런 부분통과 — {', '.join(reasons)}")
        print("=" * 60)
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

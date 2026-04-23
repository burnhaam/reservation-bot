"""match_from_order_history 테스트 — 오타 포함 / 매핑에 없는 품목 / 없는 품목."""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()


def main():
    from modules.coupang_orderer import init_browser, close_browser, is_session_valid
    from modules.product_matcher import match_from_order_history

    test_cases = [
        ("빈쯔", "오타: 빈쯔 → 빈츠로 정정되어 매치될지"),
        ("탐사 샴푸", "축약형: 축약 키워드로 탐사 실키 샴푸 매치될지"),
        ("스파클러", "메모 표현: 스파클러 → 스파클라 매치될지"),
        ("라면 총각네", "없는 품목: 주문 이력 없어 None 반환해야 함"),
    ]

    p, browser, context, page = init_browser()
    try:
        if not is_session_valid(page):
            print("[중단] 세션 무효")
            return

        for raw, desc in test_cases:
            print(f"\n=== 테스트: '{raw}' ({desc}) ===")
            t0 = time.time()
            result = match_from_order_history(raw, page)
            elapsed = time.time() - t0
            if result:
                print(f"  [HIT] {elapsed:.1f}s")
                print(f"    URL     : {result['url']}")
                print(f"    kw 매치 : {result.get('search_keyword')}")
                print(f"    source  : {result.get('source')}")
            else:
                print(f"  [MISS] {elapsed:.1f}s (주문내역에 없음)")
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

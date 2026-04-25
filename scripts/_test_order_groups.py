"""fetch_recent_order_groups() 단독 테스트."""
import json
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv()


def main():
    from modules.coupang_orderer import init_browser, close_browser, is_session_valid
    from modules.product_matcher import fetch_recent_order_groups

    p, browser, context, page = init_browser()
    try:
        if not is_session_valid(page):
            print("[중단] 세션 무효")
            return

        groups = fetch_recent_order_groups(page, lookback_days=14)
        print(f"\n=== 최근 14일 주문 묶음: {len(groups)}건 ===\n")
        for g in groups:
            print(f"[{g['order_date']}] 상품 {len(g['products'])}개")
            for prod in g["products"]:
                title = prod["title"][:60]
                print(f"   - {title}")
            print()
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

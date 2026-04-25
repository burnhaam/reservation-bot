"""최근 주문 묶음 상세 출력 + 4개 품목 매칭 시뮬레이션."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING)

from modules.coupang_orderer import (  # noqa: E402
    init_browser,
    close_browser,
    is_session_valid,
    _is_cdp_available,
)
from modules.product_matcher import fetch_recent_order_groups  # noqa: E402


def main():
    if not _is_cdp_available():
        print("CDP 미가용")
        return 1

    p, browser, ctx, page = init_browser()
    try:
        if not is_session_valid(page):
            print("세션 무효")
            return 1

        groups = fetch_recent_order_groups(page, lookback_days=14)
        print(f"\n=== 최근 14일 주문 묶음 {len(groups)}건 ===\n")
        for i, g in enumerate(groups, 1):
            print(f"[묶음 #{i}] 날짜: {g['order_date']}")
            for j, prod in enumerate(g["products"], 1):
                title = prod.get("title", "")
                print(f"   {j}. {title[:90]}")
            print()

        test_names = ["크라운 참쌀설병", "스파클라", "쌈장", "장작", "마시멜로우", "커피캡슐", "전병"]
        print("=== 매칭 시뮬레이션 (현재 로직: 'name in title') ===")
        for name in test_names:
            hits = []
            for g in groups:
                for prod in g["products"]:
                    if name in prod.get("title", ""):
                        hits.append(prod["title"][:60])
            if hits:
                print(f"  [{name}] OK 매치: {hits}")
            else:
                print(f"  [{name}] FAIL 매치 없음")
    finally:
        close_browser(p, browser, ctx, page)
    return 0


if __name__ == "__main__":
    sys.exit(main())

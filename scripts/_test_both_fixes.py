"""
이슈 #1 (수량 4개 담기) + #2 (Discord 모바일 URL) 통합 검증.

#1: 실제 장작 4개를 장바구니에 담음. 끝나면 사용자가 수동으로 비워야 함.
#2: 가짜 success 데이터로 메시지를 빌드, URL이 m.coupang.com인지 확인.
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_message_url():
    print("=" * 60)
    print(" [TEST #2] Discord 메시지 URL 검증")
    print("=" * 60)
    from modules.notifier import _build_stock_message

    fake_result = {
        "success": [
            {"item_name": "장작", "quantity": 4, "price": 10890,
             "url": "https://www.coupang.com/vp/products/5381058739"},
        ],
        "skipped": [],
        "unmapped": [],
        "failed": [],
    }
    msg = _build_stock_message(fake_result)
    print("\n--- 생성된 메시지 (cp949-safe 변환) ---")
    # cp949 콘솔에서 emoji 출력 시 크래시 방지 — '?' 로 대체
    print(msg.encode("cp949", errors="replace").decode("cp949"))
    print("--- /끝 ---\n")

    expected = "https://m.coupang.com/cart/cartView.pang"
    old = "https://cart.coupang.com/cartView.pang"
    has_new = expected in msg
    has_old = old in msg
    print(f"  새 URL ({expected}) 포함: {has_new}")
    print(f"  옛 URL ({old}) 잔재: {has_old}")
    return has_new and not has_old


def test_quantity_e2e():
    print("\n" + "=" * 60)
    print(" [TEST #1] 장작 4개 장바구니 담기 E2E")
    print(" (실제 사용자 장바구니에 4개 추가됨)")
    print("=" * 60)
    from modules.coupang_orderer import add_items_to_cart

    items = [{
        "item_name": "장작",
        "url": "https://www.coupang.com/vp/products/5381058739",
        "quantity": 4,
        "max_price": 14157,
        "source": "test",
    }]
    print(f"\n입력 items: {items}\n")

    t0 = time.time()
    result = add_items_to_cart(items)
    dt = time.time() - t0
    print(f"\n--- result (소요 {dt:.1f}s) ---")
    for k in ("success", "skipped", "failed", "unmapped"):
        rows = result.get(k) or []
        print(f"  {k} ({len(rows)})")
        for r in rows:
            print(f"    - {r}")
    print(f"  stopped: {result.get('stopped')}")
    print(f"  stop_reason: {result.get('stop_reason')}")

    # 성공 판정
    success = result.get("success") or []
    if len(success) != 1:
        return False
    qty = success[0].get("quantity")
    print(f"\n  성공 항목의 quantity 필드: {qty}")
    return qty == 4


def main():
    ok2 = test_message_url()
    ok1 = test_quantity_e2e()
    print("\n" + "=" * 60)
    print(f" [TEST #1 E2E] {'PASS' if ok1 else 'FAIL'}")
    print(f" [TEST #2 URL] {'PASS' if ok2 else 'FAIL'}")
    print("=" * 60)


if __name__ == "__main__":
    main()

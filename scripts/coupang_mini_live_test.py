"""
쿠팡 자동주문 미니 라이브 테스트 — 가장 싼 품목 1개만 실제 카트에 담아 검증.

data/product_mapping.json 에서 '자동주문_허용=true' 중 최저가 품목을 골라
add_items_to_cart()를 호출한다. 결제로 진행하지 않고 카트 담기만 수행 (안전).

사용:
  # 드라이런 (실제 클릭 안 함)
  python scripts/coupang_mini_live_test.py

  # 실제 카트 담기 (뒤에 직접 --yes 명시)
  python scripts/coupang_mini_live_test.py --yes

주의:
  - CDP 모드 Chrome(scripts/coupang_chrome_cdp_start.py 실행 상태) 필수
  - 성공 시 그 Chrome 카트에 품목 1개 담김 → 테스트 후 수동으로 비우기
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _pick_cheapest_allowed_item() -> dict | None:
    mapping = json.loads((PROJECT_ROOT / "data" / "product_mapping.json").read_text(encoding="utf-8"))
    candidates = []
    for name, info in mapping.items():
        if name.startswith("_"):
            continue
        if not isinstance(info, dict):
            continue
        if not info.get("자동주문_허용"):
            continue
        if not info.get("url"):
            continue
        # 최근가격 > 최대가격 > 평균가격 순으로 참고
        price = (info.get("최근가격") or info.get("최대가격")
                 or info.get("평균가격") or 9_999_999)
        candidates.append((int(price), name, info))
    if not candidates:
        return None
    candidates.sort()
    _, name, info = candidates[0]
    return {
        "item_name": name,
        "url": info["url"],
        "quantity": 1,
        "max_price": int(info.get("최대가격") or 0),
    }


def _load_by_key(key: str) -> dict | None:
    mapping = json.loads((PROJECT_ROOT / "data" / "product_mapping.json").read_text(encoding="utf-8"))
    info = mapping.get(key)
    if not isinstance(info, dict) or not info.get("url"):
        return None
    return {
        "item_name": key,
        "url": info["url"],
        "quantity": 1,
        "max_price": int(info.get("최대가격") or 0),
    }


def main():
    parser = argparse.ArgumentParser(description="쿠팡 미니 라이브 테스트 (1품목 카트 담기)")
    parser.add_argument("--yes", action="store_true", help="실제 카트 담기를 실행")
    parser.add_argument("--key", default=None, help="매핑 키로 품목 지정 (생략 시 최저가)")
    args = parser.parse_args()

    if args.key:
        item = _load_by_key(args.key)
        if not item:
            print(f"[오류] 매핑에 '{args.key}' 없음 또는 URL 누락.")
            sys.exit(1)
    else:
        item = _pick_cheapest_allowed_item()
        if not item:
            print("[오류] 자동주문_허용=true 인 품목을 찾지 못함.")
            sys.exit(1)

    print("=" * 60)
    print(" 쿠팡 미니 라이브 테스트")
    print("=" * 60)
    print(f"\n 품목      : {item['item_name']}")
    print(f" URL       : {item['url']}")
    print(f" 수량      : {item['quantity']}")
    print(f" 최대가격  : {item['max_price']:,}원")
    print()

    if not args.yes:
        print("[드라이런 모드]")
        print("  실제 카트 담기 없이 위 정보만 출력했습니다.")
        print("  실행하려면: python scripts/coupang_mini_live_test.py --yes")
        return

    print("[실제 실행] add_items_to_cart() 호출...\n")

    # CDP 가용성 사전 체크 (패치라이트 폴백 방지)
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    cdp_ok = False
    try:
        sock.connect(("127.0.0.1", 9222))
        cdp_ok = True
    except Exception:
        pass
    finally:
        sock.close()

    if not cdp_ok:
        print("[오류] CDP Chrome이 port 9222에서 응답하지 않습니다.")
        print("       scripts/coupang_chrome_cdp_start.py 를 먼저 실행하세요.")
        sys.exit(1)

    from modules.coupang_orderer import add_items_to_cart

    result = add_items_to_cart([item])

    print("\n" + "=" * 60)
    print(" 결과")
    print("=" * 60)
    print(f" success : {len(result['success'])}건 — {result['success']}")
    print(f" skipped : {len(result['skipped'])}건 — {result['skipped']}")
    print(f" failed  : {len(result['failed'])}건 — {result['failed']}")
    print(f" stopped : {result['stopped']}  reason={result['stop_reason']!r}")
    print()

    if result["success"]:
        print("[PASS] 실제 카트에 담김. CDP Chrome 탭의 장바구니에서 확인 후 비워주세요.")
        print("       URL: https://cart.coupang.com/cartView.pang")
    else:
        print("[FAIL] 카트 담기 실패. 로그/스크린샷(logs/) 확인 필요.")


if __name__ == "__main__":
    main()

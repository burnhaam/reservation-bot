"""
특정 날짜/이벤트의 재고 메모를 재생(replay)해 현재 파이프라인이 어떻게 동작하는지 점검.

모드:
  --preview (기본)  : DB 기록/카트 담기 없이 "어떤 품목이 어떻게 처리될지" 미리보기.
                      중복차단/이미처리/하루한도 필터 모두 우회해 원본 메모의 파싱/매칭
                      결과를 있는 그대로 보여준다.
  --live           : 매칭된 품목을 실제로 add_items_to_cart()로 담기. DB 기록은 생성하지 않음.
                      CDP Chrome 필요. 카트에 실제 품목이 담김.

사용:
  # 기본: 가장 최근 처리 이벤트의 메모를 프리뷰
  python scripts/coupang_replay_memo.py

  # 특정 날짜의 메모를 프리뷰 (YYYY-MM-DD, stock_orders에 detected_at 기준)
  python scripts/coupang_replay_memo.py --date 2026-04-21

  # 특정 event_id 지정
  python scripts/coupang_replay_memo.py --event-id lrbdlrlaslljnps9j1sq

  # 실제 카트 담기
  python scripts/coupang_replay_memo.py --date 2026-04-21 --live
"""
import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# .env 로드 (GEMINI_API_KEY 등). main.py와 동일한 방식.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass


def _find_event_id(date_filter: str | None) -> str | None:
    """stock_orders DB에서 detected_at의 날짜(YYYY-MM-DD)가 일치하는 event_id 반환."""
    db_path = PROJECT_ROOT / "db" / "reservations.db"
    if not db_path.exists():
        print(f"[오류] DB 파일 없음: {db_path}")
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        if date_filter:
            row = conn.execute(
                "SELECT calendar_event_id FROM stock_orders "
                "WHERE date(detected_at) = ? "
                "ORDER BY detected_at DESC LIMIT 1",
                (date_filter,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT calendar_event_id FROM stock_orders "
                "ORDER BY detected_at DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _fetch_event_description(event_id: str) -> tuple[str, str] | None:
    """캘린더에서 event_id의 description을 직접 조회. (summary, description)."""
    from modules.calendar import _get_google_calendar_service, _resolve_google_calendar_id
    from modules.config_loader import load_config

    cfg = load_config().get("stock", {}) or {}
    cal_name = cfg.get("calendar_name", "")
    if not cal_name:
        print("[오류] config.json의 stock.calendar_name 미설정")
        return None

    try:
        gsvc = _get_google_calendar_service()
    except Exception as e:
        print(f"[오류] 구글 캘린더 서비스 초기화 실패: {e}")
        return None

    cal_id = _resolve_google_calendar_id(gsvc, cal_name)
    if not cal_id:
        print(f"[오류] 캘린더 '{cal_name}' 해석 실패")
        return None

    try:
        ev = gsvc.events().get(calendarId=cal_id, eventId=event_id).execute()
    except Exception as e:
        print(f"[오류] 이벤트 조회 실패 ({event_id}): {e}")
        return None

    return ev.get("summary", ""), (ev.get("description") or "").strip()


def main():
    parser = argparse.ArgumentParser(description="재고 메모 재생 테스트")
    parser.add_argument("--date", help="YYYY-MM-DD (stock_orders.detected_at 기준)")
    parser.add_argument("--event-id", help="캘린더 event_id 직접 지정")
    parser.add_argument("--live", action="store_true", help="실제 카트 담기 실행 (CDP 필요)")
    args = parser.parse_args()

    event_id = args.event_id or _find_event_id(args.date)
    if not event_id:
        scope = f"date={args.date}" if args.date else "전체"
        print(f"[오류] {scope} 에 해당하는 event_id를 찾지 못함")
        sys.exit(1)

    print("=" * 60)
    print(" 재고 메모 재생 테스트")
    print("=" * 60)
    print(f" event_id : {event_id}")
    print(f" 모드     : {'LIVE (실제 카트 담기)' if args.live else 'PREVIEW (시뮬레이션)'}")
    print()

    fetched = _fetch_event_description(event_id)
    if not fetched:
        sys.exit(1)
    summary, memo_text = fetched
    print(f" summary  : {summary}")
    print(f" 메모 길이 : {len(memo_text)}자")
    print("-" * 60)
    print(memo_text)
    print("-" * 60)
    print()

    if not memo_text:
        print("[중단] description이 비어있음")
        return

    # 파싱
    from modules import stock_parser, product_matcher
    print("[1] Gemini 파싱...")
    try:
        items = stock_parser.parse_shortage_items(memo_text)
    except Exception as e:
        print(f"    [오류] 파싱 실패: {e}")
        sys.exit(1)
    print(f"    추출 품목: {len(items)}건")
    for it in items:
        name = it.get("item_name")
        cs = it.get("current_stock")
        print(f"      - {name}  (current_stock={cs})")
    print()

    # 매칭
    mapping = product_matcher.load_mapping()
    print("[2] 매핑 + 분류...")
    to_order = []
    skipped = []
    unmapped = []
    for it in items:
        raw = (it.get("item_name") or "").strip()
        if not raw:
            continue
        canonical = product_matcher.normalize_to_canonical(raw, mapping)

        skip_info = (product_matcher.is_skip_item(raw, mapping)
                     or product_matcher.is_skip_item(canonical, mapping))
        if skip_info:
            skipped.append((canonical, skip_info.get("reason", "")))
            continue

        hit = product_matcher.match_from_mapping(canonical, mapping)
        if not hit:
            unmapped.append(canonical)
            continue

        qty = hit.get("quantity", 1)
        max_stock = int(hit.get("max_stock", 0) or 0)
        current = it.get("current_stock")
        if max_stock > 0 and current is not None:
            needed = max_stock - int(current)
            if needed <= 0:
                skipped.append((canonical, f"재고 충분 ({current}/{max_stock})"))
                continue
            qty = needed

        to_order.append({
            "item_name": canonical,
            "url": hit.get("url", ""),
            "quantity": qty,
            "max_price": hit.get("max_price", 0),
        })

    print(f"    카트 대상: {len(to_order)}건")
    for o in to_order:
        print(f"      ORDER  {o['item_name']:<25} x{o['quantity']}  max={o['max_price']:,}원  {o['url']}")
    print(f"    스킵    : {len(skipped)}건")
    for name, reason in skipped:
        print(f"      SKIP   {name:<25} ({reason})")
    print(f"    미매핑  : {len(unmapped)}건")
    for name in unmapped:
        print(f"      UNMAP  {name}")
    print()

    if not args.live:
        print("[PREVIEW] 실제 카트 담기는 하지 않았습니다.")
        print("          --live 플래그로 재실행하면 CDP Chrome 카트에 실제 담김.")
        return

    if not to_order:
        print("[중단] 담을 품목 없음")
        return

    # LIVE: CDP 가용성 체크 후 add_items_to_cart 호출
    print("[3] LIVE: add_items_to_cart() 호출")
    from modules.coupang_orderer import _is_cdp_available, add_items_to_cart
    if not _is_cdp_available():
        print("    [오류] CDP Chrome(port 9222) 미응답.")
        print("           scripts/coupang_chrome_cdp_start.py 먼저 실행하세요.")
        sys.exit(1)

    result = add_items_to_cart(to_order)

    print()
    print("=" * 60)
    print(" LIVE 결과")
    print("=" * 60)
    print(f" success : {len(result['success'])}건")
    for s in result["success"]:
        print(f"   PASS  {s['item_name']:<25} x{s.get('quantity')}  price={s.get('price'):,}원")
    print(f" skipped : {len(result['skipped'])}건")
    for s in result["skipped"]:
        print(f"   SKIP  {s['item_name']:<25}  {s.get('reason')}")
    print(f" failed  : {len(result['failed'])}건")
    for s in result["failed"]:
        print(f"   FAIL  {s['item_name']:<25}  {s.get('reason')}")
    print(f" stopped : {result['stopped']}  reason={result['stop_reason']!r}")
    print()
    print("[주의] 이 스크립트는 stock_orders DB에 기록을 남기지 않습니다.")
    print("       main.py 정규 실행과 별도로 카트만 담겼습니다.")


if __name__ == "__main__":
    main()

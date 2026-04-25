"""21일 메모 기반 로직 ①②③ 엔드투엔드 테스트.

절차:
  0. 기존 stock_orders 백업 (파일로)
  1. DB 클리어 (4/21 메모 재감지를 위해)
  2. run_stock_pipeline() — 장바구니 담기 + 로직 ② 자동 트리거
  3. 생성된 레코드 detected_at 을 4일 전으로 백데이트
  4. sync_mapping_from_orders(mode='scheduled') 호출 — 로직 ①③
  5. DB 최종 상태 출력
  6. 테스트 stock_orders 전체 삭제
"""
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from modules.db import get_connection  # noqa: E402


def _sep(msg: str = "") -> None:
    print("\n" + "=" * 72)
    if msg:
        print(msg)
        print("=" * 72)


def backup_stock_orders() -> Path:
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM stock_orders").fetchall()]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = PROJECT_ROOT / "data" / f"_test_backup_{stamp}.json"
    backup_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[백업] 기존 {len(rows)}건 → {backup_path.name}")
    return backup_path


def delete_all_stock_orders() -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM stock_orders")
        conn.commit()
        return cur.rowcount


def list_stock_orders() -> list[dict]:
    with get_connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, detected_at, item_name, status, matched_source, "
                "confirmed_at, scan_done_at FROM stock_orders ORDER BY id"
            ).fetchall()
        ]


def backdate_all(days_back: int) -> int:
    new_date = (datetime.now() - timedelta(days=days_back)).isoformat(timespec="seconds")
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE stock_orders SET detected_at = ? WHERE scan_done_at IS NULL",
            (new_date,),
        )
        conn.commit()
        return cur.rowcount


def print_orders(orders: list[dict], title: str) -> None:
    print(f"\n[{title}] 총 {len(orders)}건")
    for r in orders:
        conf = "O" if r.get("confirmed_at") else "-"
        scan = "O" if r.get("scan_done_at") else "-"
        src = r.get("matched_source") or ""
        print(
            f"  #{r['id']:3} [{r['status']:10}] conf={conf} scan={scan} "
            f"src={src:15} {r['item_name']}"
        )


def main() -> int:
    _sep("로직 ①②③ 엔드투엔드 테스트 시작")
    print(f"현재 시각: {datetime.now().isoformat(timespec='seconds')}")

    # Step 0: 백업
    _sep("Step 0 — 기존 stock_orders 백업")
    backup_path = backup_stock_orders()

    # Step 1: 클리어
    _sep("Step 1 — DB 클리어 (4/21 메모 재감지 강제)")
    n = delete_all_stock_orders()
    print(f"삭제: {n}건")

    # Step 2: 파이프라인 실행 (장바구니 담기 + 로직 ②)
    _sep("Step 2 — run_stock_pipeline() 실행")
    print("로직 ② (instant) 는 파이프라인 끝에서 자동 트리거됩니다.\n")
    from main import run_stock_pipeline

    n_processed = run_stock_pipeline()
    print(f"\n[Step 2 완료] run_stock_pipeline() 반환값: {n_processed}")

    new_rows = list_stock_orders()
    print_orders(new_rows, "파이프라인 후 DB 상태")

    if not new_rows:
        print("\n[중단] 파이프라인이 아무 레코드도 생성하지 않음 — 이후 단계 불가")
        print(f"백업 파일 유지: {backup_path}")
        return 1

    # Step 3: 백데이트
    _sep("Step 3 — detected_at 을 4일 전으로 백데이트 (3일+ 경과 시뮬레이션)")
    n = backdate_all(days_back=4)
    print(f"백데이트: {n}건")

    # Step 4: 로직 ①③ (scheduled 스캔)
    _sep("Step 4 — sync_mapping_from_orders(mode='scheduled') 호출")
    from modules.coupang_orderer import (
        init_browser,
        close_browser,
        is_session_valid,
        _is_cdp_available,
    )
    from modules.product_matcher import sync_mapping_from_orders

    if not _is_cdp_available():
        print("[경고] CDP 미가용 — scheduled 스캔 스킵")
        result = None
    else:
        p, browser, ctx, page = init_browser()
        try:
            if not is_session_valid(page):
                print("[경고] 쿠팡 세션 무효 — scheduled 스캔 스킵")
                result = None
            else:
                result = sync_mapping_from_orders(page, mode="scheduled", lookback_days=14)
        finally:
            close_browser(p, browser, ctx, page)

    if result is not None:
        print("\n[Step 4 결과]")
        print(f"  confirmed ({len(result.get('confirmed', []))}건): {result.get('confirmed', [])}")
        print(
            f"  unconfirmed ({len(result.get('unconfirmed', []))}건): "
            f"{result.get('unconfirmed', [])}"
        )
        print(f"  added ({len(result.get('added', []))}건):")
        for a in result.get("added", []):
            print(f"    • {a.get('title', '')[:60]} (alias={a.get('alias')})")
        print(f"  pending ({len(result.get('pending', []))}건):")
        for pd in result.get("pending", []):
            print(f"    • #{pd.get('id')} {pd.get('title', '')[:60]} action={pd.get('action', '?')}")

    # Step 5: 최종 DB 상태
    _sep("Step 5 — 스캔 후 DB 최종 상태")
    final_rows = list_stock_orders()
    print_orders(final_rows, "최종 DB")

    # Step 6: 테스트 데이터 삭제
    _sep("Step 6 — 테스트 stock_orders 전체 삭제")
    n = delete_all_stock_orders()
    print(f"삭제: {n}건")

    remaining = list_stock_orders()
    print(f"\n남은 레코드: {len(remaining)}건 (0이어야 정상)")

    _sep("테스트 완료")
    print(f"백업 파일: {backup_path}")
    print("필요 시 백업으로 복구 가능: INSERT INTO stock_orders ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())

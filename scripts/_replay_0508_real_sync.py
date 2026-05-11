"""05-08 누락 sync 를 실제 쿠팡 주문내역과 비교해 재현.

흐름:
  1) 가짜 테스트로 등록된 pending #22~#26 제거 + next_id 22 로 복귀
  2) CDP attach → 세션 검증
  3) sync_mapping_from_orders(page, mode='instant', lookback_days=14) 호출
     - 패치된 로직(ordered+skipped+failed 모두 비교) 사용
     - 실제 쿠팡 주문내역에서 발견한 URL 이 봇 matched_url 과 다른 경우만
       Discord pending 큐에 mapping_update 등록
  4) 결과 요약 출력
"""
import json
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

PENDING_PATH = PROJECT_ROOT / "data" / "pending_approvals.json"

# Step 1: 가짜 테스트 항목 제거
_TEST_IDS = {22, 23, 24, 25, 26}
data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
before = len(data["items"])
data["items"] = [it for it in data["items"] if it["id"] not in _TEST_IDS]
data["next_id"] = 22
PENDING_PATH.write_text(
    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"[Cleanup] pending {before}건 → {len(data['items'])}건, next_id=22")

# Step 2~3: 실제 sync 실행
from modules.coupang_orderer import (
    init_browser, close_browser, is_session_valid, _is_cdp_available,
)
from modules.product_matcher import sync_mapping_from_orders

if not _is_cdp_available():
    print("[Error] CDP 포트 9222 미응답 — scripts/coupang_chrome_cdp_start.py 먼저 실행")
    sys.exit(1)

p, browser, ctx, page = init_browser()
try:
    if not is_session_valid(page):
        print("[Error] 쿠팡 세션 무효 — 쿠키 재임포트 필요")
        sys.exit(1)
    print("[Sync] sync_mapping_from_orders(mode='instant', lookback_days=14) 시작…")
    result = sync_mapping_from_orders(page, mode="instant", lookback_days=14)
finally:
    close_browser(p, browser, ctx, page)

# Step 4: 결과 요약
print()
print("=" * 60)
print("[Sync 결과]")
print(f"  added     (신규 매핑 자동추가): {len(result.get('added', []))}")
print(f"  updated   (URL 교체 제안)     : {len(result.get('updated', []))}")
print(f"  pending   (Discord 승인 대기) : {len(result.get('pending', []))}")
print(f"  confirmed (실주문 확인)       : {len(result.get('confirmed', []))}")
print(f"  unconfirmed                   : {len(result.get('unconfirmed', []))}")
print()
if result.get("pending"):
    print("[Discord 승인 큐에 등록된 항목]")
    for p_item in result["pending"]:
        print(f"  #{p_item['id']} [{p_item['action']}] {p_item['title']}")
        if p_item.get("current_url"):
            print(f"      current  : {p_item['current_url']}")
        if p_item.get("suggested_url"):
            print(f"      suggested: {p_item['suggested_url']}")
if result.get("added"):
    print()
    print("[자동 매핑 추가 (파일 갱신됨)]")
    for a in result["added"]:
        print(f"  + {a.get('title')} ← {a.get('original','')[:50]} (날짜 {a.get('order_date','')})")

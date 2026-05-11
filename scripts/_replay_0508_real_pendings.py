"""05-08 누락 sync 결과의 '진짜 mapping_update diff 만' Discord 큐 등록.

직전 _replay_0508_real_sync.py 실행에서 추출한 실제 쿠팡 주문내역 URL 기반:
- 봇이 cart 에 담은 matched_url 과 사용자가 실제 결제한 URL 이 다른 경우만 후보
- URL 이 같으면 (예: 생수 ↔ 탐사 샘물 동일 URL) 교체 불필요
- 매핑 파일은 .bak 에서 복구 (Gemini Phase 2 가 quota/503 으로 실패해서 신규 매핑이
  잘못 추가됐던 사고 정리)
"""
import json
import shutil
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from modules.discord_bot import add_pending
from modules.notifier import _send_discord_webhook

MAPPING_PATH = PROJECT_ROOT / "data" / "product_mapping.json"
MAPPING_BAK = PROJECT_ROOT / "data" / "product_mapping.json.bak"

# Step 1: 매핑 파일 .bak 에서 복구 (Phase 2 실패로 잘못 추가된 3건 제거)
shutil.copyfile(MAPPING_BAK, MAPPING_PATH)
restored = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
print(f"[Restore] product_mapping.json keys: {len(restored)}")

# Step 2: 실제 URL diff 가 있는 2건만 mapping_update 후보로 등록
# (실주문 URL 은 직전 sync 실행에서 resolve_product_url 로 캡처한 값)
_REAL_DIFFS = [
    {
        "memo_item": "에이센트 블랙 에디션 디퓨저",
        "current_url": "https://www.coupang.com/vp/products/8901008542",
        "suggested_url": "https://www.coupang.com/vp/products/8957518691",
        "actual_title": "에이센트 대용량 디퓨저 리필, 1개, 1L, 라이브러리",
        "status_phrase": "봇은 담기 건너뜀(품절 등)",
    },
    {
        "memo_item": "야광봉",
        "current_url": "https://www.coupang.com/vp/products/5935010575",
        "suggested_url": "https://www.coupang.com/vp/products/4838552876",
        "actual_title": "마메 LED 야광팔찌 100p, 랜덤 발송, 1세트",
        "status_phrase": "봇이 매핑 URL 로 담기 성공",
    },
]
_ORDER_DATE = "2026-05-08"

ids = []
for d in _REAL_DIFFS:
    # mapping 에 키가 실제로 있는지 확인 (없으면 _apply_approval 에서 실패)
    if d["memo_item"] not in restored:
        print(f"[Warn] '{d['memo_item']}' 매핑에 없음 — pending 등록 스킵")
        continue
    pid = add_pending(
        item_type="mapping_update",
        memo_item=d["memo_item"],
        current_url=d["current_url"],
        suggested_url=d["suggested_url"],
        reason=(
            f"'{d['memo_item']}' {d['status_phrase']}. "
            f"실주문({_ORDER_DATE})에 '{d['actual_title'][:50]}' 발견 — URL 교체?"
        ),
    )
    ids.append((pid, d["memo_item"], d["actual_title"]))
    print(f"pending #{pid}: {d['memo_item']} → {d['actual_title'][:40]}")

# Step 3: 안내 Discord 발송
bullet = "\n".join(
    f"• `#{pid}` `{memo}` → 실주문 '{title[:45]}'"
    for pid, memo, title in ids
)
msg = (
    "🔁 **[05-08 재현 / 실데이터] mapping_update 제안 2건**\n\n"
    "직전 sync(`mode=instant`, `lookback_days=14`) 에서 실제 쿠팡 주문내역과 비교했을 때 "
    "**봇 matched_url ≠ 실주문 URL** 인 항목만 추렸습니다.\n\n"
    f"{bullet}\n\n"
    "💡 참고: '생수'(`...7689270513`)는 사용자가 실제 받은 상품의 정식 명칭이 "
    "'탐사 샘물, 500ml, 40개'였으나 **URL 은 봇 추천과 동일**해서 교체 후보에서 제외했습니다.\n\n"
    "각 메시지에서 [✅ 승인 (URL 교체)] 누르면 `data/product_mapping.json` 의 해당 키 URL 이 "
    "suggested 로 갱신되고 `.bak` 에 직전 상태가 백업됩니다.\n"
    "[❌ 거절] 누르면 큐에서 제거되고 매핑은 변경 없음."
)
ok = _send_discord_webhook(msg)
print(f"안내 알림: {ok}")

"""05-08 누락 분 중 사용자 직접 지정한 2건 Discord pending 등록.

(A) 테이프클리너(돌돌이) URL → 8개 세트 variant 로 교체
(B) 미용티슈 기본수량 1 → 2 (URL 변경 없음 — qty 만 갱신)

새 mapping_update 핸들러가 suggested_url / suggested_quantity 둘 중 채워진 필드만
적용한다. ✅ 클릭 시 product_mapping.json 의 해당 entry 가 갱신되고 .bak 백업 생성.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from modules.discord_bot import add_pending
from modules.notifier import _send_discord_webhook


pid_a = add_pending(
    item_type="mapping_update",
    memo_item="테이프클리너",
    current_url="https://www.coupang.com/vp/products/8969582523",
    suggested_url="https://www.coupang.com/vp/products/8969582523?vendorItemId=93229337651",
    reason=(
        "'테이프클리너' 봇이 매핑 URL 로 담기 성공. "
        "실주문(2026-05-08): 같은 product code 의 8개 세트 변형(vendorItemId=93229337651). "
        "URL 교체? (다음에도 8개 세트 자동주문)"
    ),
)
print(f"pending #{pid_a}: 테이프클리너 URL → 8개 세트 variant")

pid_b = add_pending(
    item_type="mapping_update",
    memo_item="미용티슈",
    current_quantity=1,
    suggested_quantity=2,
    reason=(
        "'미용티슈' 봇이 매핑 URL 로 담기 성공(수량 1). "
        "실주문(2026-05-08): 수량 2개. 기본수량 1→2?"
    ),
)
print(f"pending #{pid_b}: 미용티슈 기본수량 1 → 2")

msg = (
    "🔁 **[05-08 후속] mapping_update 2건 (URL + 수량)**\n\n"
    f"• `#{pid_a}` `테이프클리너` — **URL variant 교체** (8개 세트, vendorItemId=93229337651)\n"
    f"• `#{pid_b}` `미용티슈` — **기본수량 1 → 2** (URL 변경 없음)\n\n"
    "두 항목 모두 [✅ 승인 (매핑 갱신)] / [❌ 거절] 버튼으로 처리하세요. "
    "✅ 클릭 시 채워진 필드만 적용(URL 만 / 수량 만 / 둘 다)."
)
ok = _send_discord_webhook(msg)
print(f"안내 알림: {ok}")

"""디스코드 승인 플로우 테스트 - 3건 pending 추가 + 안내 알림 발송."""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from modules.discord_bot import add_pending
from modules.notifier import _send_discord_webhook


def main():
    id1 = add_pending(
        item_type="mapping_add",
        memo_item="[테스트]디스코드승인A",
        suggested_url="https://www.coupang.com/vp/products/9999999001",
        suggested_max_price=1000,
        reason="Discord 승인 플로우 테스트 (mapping_add — 신규 매핑 추가)",
    )
    id2 = add_pending(
        item_type="mapping_update",
        memo_item="스파클라",
        current_url="https://www.coupang.com/vp/products/8259186367",
        suggested_url="https://www.coupang.com/vp/products/9999999002",
        reason="Discord 승인 플로우 테스트 (mapping_update — 실존 매핑 URL 교체)",
    )
    id3 = add_pending(
        item_type="mapping_disable",
        memo_item="[테스트]존재하지않는키",
        reason="Discord 승인 플로우 테스트 (mapping_disable — 매핑 없는 키, 승인 시 실패 응답 확인용)",
    )
    print(f"pending 추가: id={id1}, {id2}, {id3}")

    msg = (
        "🔔 **매핑 승인 버튼 플로우 테스트**\n\n"
        "3건 대기 건이 큐에 추가됐습니다. 봇이 곧 (10초 이내) 채널에 "
        "각각 버튼이 달린 메시지를 발송합니다. 아래 순서대로 버튼 클릭 테스트해보세요.\n\n"
        f"• `#{id1}` **[신규추가]** `[테스트]디스코드승인A` — 새 매핑 등록\n"
        f"• `#{id2}` **[URL교체]** `스파클라` → 테스트 URL — 실존 매핑 URL 덮어쓰기\n"
        f"• `#{id3}` **[비활성화]** `[테스트]존재하지않는키` — 존재 안 하는 키 (실패 케이스)\n\n"
        "**테스트 순서**:\n"
        f"1. `#{id1}` 메시지에서 [✅ 매핑 승인] 클릭 → '승인 완료' suffix 확인\n"
        f"2. `#{id2}` 메시지에서 [❌ 매핑 거절] 클릭 → '거절' suffix 확인\n"
        f"3. `#{id3}` 메시지에서 [✅ 매핑 승인] 클릭 → 'ephemeral 실패' 메시지 확인 (버튼은 그대로)\n"
        f"4. `#{id3}` 메시지에서 [❌ 매핑 거절] 클릭 → 정리\n\n"
        "승인한 건이 있으면 `data/product_mapping.json`에 `[테스트]디스코드승인A` 키가 추가됩니다.\n"
        "테스트 끝나면 그 키 제거 요청하세요."
    )
    ok = _send_discord_webhook(msg)
    print(f"알림 발송: {ok}")


if __name__ == "__main__":
    main()

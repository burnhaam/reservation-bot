"""05-08 07:51 cart 사이클에서 누락된 mapping_update Discord 알림 재현.

당시 stock_orders 5건(ordered 4 + skipped 1)을 그대로 사용해
sync_mapping_from_orders 가 봤어야 했을 mapping_update 제안을
Discord 큐에 강제 등록한다. suggested_url 은 명확히 가짜(9999990XXX)이므로
**테스트 후 모두 ❌ 거절** — ✅ 승인하면 실제 매핑이 가짜 URL 로 덮어써짐.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from modules.discord_bot import add_pending
from modules.notifier import _send_discord_webhook


# 05-08 07:51:20 stock_orders 그대로 — db/reservations.db id=98~102
_REPLAY_ITEMS = [
    # (memo_item, current_url, status_phrase, fake_suggested_id)
    ("미용티슈",
     "https://www.coupang.com/vp/products/6186243973",
     "봇이 매핑 URL 로 담기 성공",
     "9999990001"),
    ("테이프클리너",
     "https://www.coupang.com/vp/products/8969582523",
     "봇이 매핑 URL 로 담기 성공",
     "9999990002"),
    ("야광봉",
     "https://www.coupang.com/vp/products/5935010575",
     "봇이 매핑 URL 로 담기 성공",
     "9999990003"),
    ("에이센트 블랙 에디션 디퓨저",
     "https://www.coupang.com/vp/products/8901008542",
     "봇은 담기 건너뜀(품절 등)",
     "9999990004"),
    ("생수",
     "https://www.coupang.com/vp/products/7689270513",
     "봇이 매핑 URL 로 담기 성공",
     "9999990005"),
]
_ORDER_DATE = "2026-05-08"


def main():
    ids = []
    for memo, cur_url, phrase, fake_id in _REPLAY_ITEMS:
        pid = add_pending(
            item_type="mapping_update",
            memo_item=memo,
            current_url=cur_url,
            suggested_url=f"https://www.coupang.com/vp/products/{fake_id}",
            reason=(
                f"[테스트/05-08 재현] '{memo}' {phrase}. "
                f"실주문({_ORDER_DATE})에 '[테스트URL] {memo} 가짜상품' 발견 — URL 교체? "
                "(이 알림은 누락된 05-08 sync 재현 테스트입니다. 모두 ❌ 거절하세요)"
            ),
        )
        ids.append((pid, memo))
        print(f"pending #{pid}: {memo}")

    bullet = "\n".join(
        f"• `#{pid}` `{memo}` — current vs **테스트URL(9999990XXX)**"
        for pid, memo in ids
    )
    msg = (
        "🧪 **[테스트] 05-08 07:51 누락된 mapping_update 재현**\n\n"
        "당시 cart 사이클(`stock_orders` id=98~102)에서 `sync_mapping_from_orders` "
        "가 호출됐어야 했지만 로그·매핑변화 둘 다 없음. "
        "방금 수정한 `current_url_by_name` 로직(ordered+skipped+failed 모두 비교) 이 "
        "정상 동작할 때 Discord 에 떠야 할 5건을 그대로 큐에 넣었습니다.\n\n"
        f"{bullet}\n\n"
        "**⚠️ 모두 [❌ 거절] 클릭하세요.** suggested_url 이 **가짜(9999990XXX)** 라 "
        "✅ 승인 시 실제 매핑 URL 이 가짜로 덮어써집니다.\n\n"
        "이 테스트의 목적: 버튼 라벨/메시지 포맷/`reason` status 분기"
        "(`담기 성공` vs `담기 건너뜀(품절 등)`)가 정상 출력되는지 눈으로 확인."
    )
    ok = _send_discord_webhook(msg)
    print(f"안내 알림 발송: {ok}")


if __name__ == "__main__":
    main()

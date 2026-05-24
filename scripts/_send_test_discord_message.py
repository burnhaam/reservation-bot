"""
장작 4개 가짜 stock_result로 메시지를 빌드해 Discord 웹훅으로 발송.
사용자가 휴대폰에서 링크 클릭 → 쿠팡 앱 자동 실행 검증용.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    from modules.notifier import _build_stock_message, _send_discord_webhook

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
    if not msg:
        print("[ERR] 메시지 빌드 실패")
        return

    # cp949 콘솔 크래시 회피 — 메시지는 utf-8로 디스코드에 전송됨 (영향 없음)
    safe = msg.encode("cp949", errors="replace").decode("cp949")
    print("--- 발송할 메시지 ---")
    print(safe)
    print("---")

    ok = _send_discord_webhook(msg)
    print(f"\nDiscord 웹훅 결과: {'OK' if ok else 'FAIL'}")
    if not ok:
        print("DISCORD_WEBHOOK_URL 환경변수 또는 네트워크 확인 필요")


if __name__ == "__main__":
    main()

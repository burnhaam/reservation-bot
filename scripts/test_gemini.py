"""Gemini API 연결 및 재고 메모 파싱 테스트 (개발자 편의용).

실행:
    python scripts/test_gemini.py

전제:
    .env에 GEMINI_API_KEY가 설정되어 있어야 한다.
    python-dotenv가 .env를 자동 로드하도록 modules.env_loader를 경유한다.
"""

import sys
from pathlib import Path


# 프로젝트 루트를 sys.path에 추가 (modules 임포트용)
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    # .env 자동 로드
    try:
        from modules.env_loader import load_env
        load_env()
    except Exception as e:
        print(f"[경고] .env 로드 실패: {e}")

    from modules.stock_parser import check_api_key_valid, parse_shortage_items

    print("[1/2] API 키 검증...")
    ok, msg = check_api_key_valid()
    print(f"  결과: {msg}")
    if not ok:
        return 1

    print("\n[2/2] 실제 메모 파싱 테스트...")
    test_memos = [
        "세정티슈재고없음, 키친타월3개, 장작3박스, 빈츠 15개정도 남았습니다",
        "불멍키트는 쫀드기는 택배가와서 넣었고 스파클라는 없어서 넣지않았습니다",
        "오늘 손님 잘 받았어요",  # 품목 없음 → 빈 배열 기대
    ]
    for memo in test_memos:
        result = parse_shortage_items(memo)
        print(f"\n입력: {memo[:50]}")
        print(f"결과: {result}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
재고 메모 감지 모듈.

캘린더 일정의 description(메모)에 내용이 있고 아직 DB에 처리 기록이 없는
후보만 반환한다. 스태프 캘린더의 메모에는 재고 상태만 기록한다고 가정하므로
별도 트리거 키워드 체크는 하지 않는다.

주요 함수:
- detect_stock_memos(): 처리 대상 후보 메모 리스트를 반환
"""

import hashlib
import logging

from modules import calendar
from modules.config_loader import load_config
from modules.db import get_connection


logger = logging.getLogger(__name__)


def _hash_memo(text: str) -> str:
    """메모 텍스트의 SHA256 해시를 계산하여 hex 문자열로 반환."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _already_processed(event_id: str, memo_hash: str) -> bool:
    """동일한 (event_id, memo_hash) 조합이 DB에 있으면 True (이미 처리됨)."""
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM stock_orders "
                "WHERE calendar_event_id = ? AND memo_hash = ? LIMIT 1",
                (event_id, memo_hash),
            )
            return cur.fetchone() is not None
    except Exception:
        logger.exception("[StockDetect] DB 조회 실패 (event_id=%s)", event_id)
        # 조회 실패 시 중복 처리 방지를 위해 이미 처리된 것으로 간주
        return True


def detect_stock_memos() -> list[dict]:
    """description이 있고 아직 처리되지 않은 메모를 반환.

    동작:
    1. config에서 calendar_name / memo_lookback_days 로드
    2. calendar.read_stock_memos()로 최근 N일 + 향후 1일 일정 조회
    3. description이 비어 있지 않은 메모의 SHA256 해시 계산
    4. DB에서 이미 처리된 (event_id, memo_hash) 조합 제외
    5. 메모가 수정된 경우 → 새 해시이므로 다시 처리됨

    스태프 캘린더의 메모에는 재고 상태만 기록한다고 가정하므로
    trigger 키워드 필터는 하지 않는다. 품목 추출은 stock_parser에서 담당.

    반환: [{"event_id", "memo_text", "memo_hash", "event_date"}]
    """
    try:
        config = load_config()
    except Exception:
        logger.exception("[StockDetect] config 로드 실패")
        return []

    stock_cfg = config.get("stock", {}) or {}
    if not stock_cfg.get("enabled", False):
        logger.info("[StockDetect] stock.enabled=false — 감지 건너뜀")
        return []

    calendar_name = stock_cfg.get("calendar_name", "")
    lookback_days = int(stock_cfg.get("memo_lookback_days", 7))

    if not calendar_name:
        logger.error("[StockDetect] stock.calendar_name 미설정")
        return []

    try:
        raw_memos = calendar.read_stock_memos(calendar_name, lookback_days)
    except Exception:
        logger.exception("[StockDetect] 캘린더 메모 조회 실패")
        return []

    candidates: list[dict] = []
    for memo in raw_memos:
        description = (memo.get("description", "") or "").strip()
        if not description:
            continue

        event_id = memo.get("event_id", "")
        memo_hash = _hash_memo(description)

        if _already_processed(event_id, memo_hash):
            continue

        candidates.append({
            "event_id": event_id,
            "memo_text": description,
            "memo_hash": memo_hash,
            "event_date": memo.get("start_date", ""),
        })

    logger.info("[StockDetect] 처리 대상 메모: %d건 (전체 %d건 중)",
                len(candidates), len(raw_memos))
    return candidates

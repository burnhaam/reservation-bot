"""
자연어 재고 메모 → 부족 품목 JSON 리스트 추출 (Gemini API 기반).

PRD v2 FR-2 명세 구현. Gemini의 response_schema 기능으로 JSON 배열 출력을
강제하여 파싱 안정성을 높였고, 30초 timeout과 모든 예외를 빈 배열로 흡수하여
재고 파이프라인 전체가 AI 호출 실패로 중단되지 않도록 한다.

변경 이력:
- v1: Anthropic Claude Haiku 기반
- v2: Google Gemini 2.5 Flash로 교체 — 무료 할당량 활용
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from google.genai.errors import APIError

from modules.config_loader import load_config


logger = logging.getLogger(__name__)


# 연속 실패 카운터: 3회 이상 연속 실패 시 영구 1회 카카오 알림.
# Task Scheduler가 프로세스를 매번 새로 띄우므로 파일 기반 상태 필요.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PARSER_STATE_PATH = _PROJECT_ROOT / "data" / "parser_failure_state.json"
_FAILURE_ALERT_THRESHOLD = 3


def _load_parser_state() -> dict:
    try:
        if _PARSER_STATE_PATH.exists():
            return json.loads(_PARSER_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[StockParse] 상태 파일 로드 실패 — 0에서 시작", exc_info=True)
    return {"consecutive_failures": 0, "last_failure_at": None}


def _save_parser_state(state: dict) -> None:
    try:
        _PARSER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PARSER_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.warning("[StockParse] 상태 파일 저장 실패", exc_info=True)


def _record_parser_outcome(success: bool, err_msg: str = "") -> None:
    """성공 시 카운터 리셋, 실패 시 증가 + 임계치 도달 시 카카오 알림.

    알림은 dedup_key='gemini_parse_failed'로 영구 1회. 임계치 회복 후 다시 연속
    실패가 누적되면 notify_state.json의 해당 키를 수동 제거해야 재발송 가능.
    """
    state = _load_parser_state()
    if success:
        if state.get("consecutive_failures", 0) > 0:
            logger.info("[StockParse] Gemini 호출 성공 — 실패 카운터 리셋")
        state["consecutive_failures"] = 0
        state["last_failure_at"] = None
        _save_parser_state(state)
        return

    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    state["last_failure_at"] = datetime.now().isoformat()
    _save_parser_state(state)

    if state["consecutive_failures"] >= _FAILURE_ALERT_THRESHOLD:
        try:
            from modules.notifier import send_stock_alert
            send_stock_alert(
                f"[Gemini API 장애] 재고 메모 파싱 {state['consecutive_failures']}회 연속 실패.\n"
                f"최근 오류: {err_msg[:200] if err_msg else '불명'}\n"
                "GEMINI_API_KEY 유효성과 쿼터를 확인하세요. "
                "복구 후 다시 알림 받으려면 data/notify_state.json 에서 "
                "'gemini_parse_failed' 키를 지우세요.",
                dedup_key="gemini_parse_failed",
                cooldown_hours=None,
            )
        except Exception:
            logger.exception("[StockParse] 장애 알림 발송 중 예외")


# 기본 모델명 (config.json의 stock.gemini_model로 오버라이드 가능)
_DEFAULT_MODEL = "gemini-2.5-flash"
# PRD 4.2 "1사이클 5분 이내" 근거. HttpOptions는 ms 단위를 요구.
_REQUEST_TIMEOUT_SEC = 30.0
_REQUEST_TIMEOUT_MS = int(_REQUEST_TIMEOUT_SEC * 1000)
_MAX_OUTPUT_TOKENS = 1024


# Gemini 구조화 출력 스키마 (OpenAPI 형식).
# response_schema=list[dict]는 SDK가 additionalProperties를 붙여 Gemini가 거부하므로
# 명시적 스키마로 변환해야 한다.
_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "item_name": {"type": "STRING"},
        "reason": {"type": "STRING"},
        "current_stock": {"type": "INTEGER", "nullable": True},
    },
    "required": ["item_name"],
}

_ITEMS_ARRAY_SCHEMA = {
    "type": "ARRAY",
    "items": _ITEM_SCHEMA,
}

_BATCH_ENTRY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "event_id": {"type": "STRING"},
        "items": _ITEMS_ARRAY_SCHEMA,
    },
    "required": ["event_id", "items"],
}

_BATCH_ARRAY_SCHEMA = {
    "type": "ARRAY",
    "items": _BATCH_ENTRY_SCHEMA,
}


# 시스템 프롬프트: 메모에 언급된 모든 품목을 추출하고, 현재 재고 수량이 명시되면
# current_stock 필드에 기록한다. 메모엔 재고 상태만 적힌다는 전제라 별도 트리거
# 키워드 없이 품목이 언급만 되면 부족으로 간주한다 (충분/많음 표현만 예외).
# "매핑표 품목 목록"이 contents에 주어지면 그 목록의 정확한 이름으로 정규화해야
# main.py의 별칭 매칭이 제대로 동작한다.
_SYSTEM_PROMPT = """당신은 숙박업 재고 메모 분석기입니다. 다음 규칙으로 JSON 배열만 출력하세요.

추출 규칙:
1. 메모에 언급된 모든 재고 품목을 추출 (품목명이 나오면 기본적으로 부족한 것으로 간주)
2. "충분", "많이 있음", "넉넉" 등 재고가 넉넉하다는 표현만 있는 품목은 제외
3. 현재 재고 수량이 숫자로 명시되어 있으면 current_stock 필드에 정수로 기록 (예: "장작 2개 남음" → 2). 명시 없으면 null
4. 매핑표 품목 목록이 제공되면, 유사한 품목은 목록의 **정확한 이름**으로 정규화
   - 예: "스파클러" → "스파클라", "키친타올" → "키친타월", "네스프레소 캡슐" → 매핑표에 "커피캡슐"이 있으면 "커피캡슐"
   - 목록에 명백히 대응되는 항목이 없으면 원래 가장 일반적인 형태로 출력
5. 출력은 반드시 JSON 배열, 다른 설명 금지

출력 형식:
[
  {"item_name": "장작", "reason": "2개 남음", "current_stock": 2},
  {"item_name": "키친타월", "reason": "없음", "current_stock": null}
]

품목이 하나도 없으면 빈 배열 [] 반환."""


def _build_canonical_hint() -> str:
    """매핑표의 canonical 이름(+ 스킵 품목명)을 Gemini 컨텍스트용 힌트 문자열로 빌드.

    호출 실패 시 빈 문자열 반환. 매핑표가 없으면 힌트 없이 동작.
    """
    try:
        from modules import product_matcher
        mapping = product_matcher.load_mapping()
        names = list((mapping.get("items") or {}).keys())
        skip_names = list((mapping.get("skip_items") or {}).keys())
        all_names = names + skip_names
        if not all_names:
            return ""
        return "매핑표 품목 목록 (이 이름으로 정규화): " + ", ".join(all_names)
    except Exception:
        logger.exception("[StockParse] 매핑표 힌트 로드 실패")
        return ""


def _get_api_key() -> Optional[str]:
    """환경변수에서 Gemini API 키를 조회. 없으면 None."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return key if key else None


def _get_model_name() -> str:
    """config.json에서 Gemini 모델명 조회. 기본값은 gemini-2.5-flash."""
    try:
        cfg = load_config()
        stock_cfg = cfg.get("stock", {}) or {}
        return stock_cfg.get("gemini_model", _DEFAULT_MODEL)
    except Exception:
        logger.exception("[StockParse] config 로드 실패 — 기본 모델 사용")
        return _DEFAULT_MODEL


def parse_shortage_items(memo_text: str) -> list[dict]:
    """재고 메모 자연어 텍스트를 부족 품목 리스트로 변환.

    Args:
        memo_text: 알바생이 작성한 자유 형식 한글 메모.

    Returns:
        [{"item_name": str, "reason": str}, ...] 형태의 리스트.
        파싱 실패, API 오류, 타임아웃 등 모든 예외 상황에서 빈 배열 반환 (예외 미전파).

    예시:
        >>> parse_shortage_items("세정티슈 없음, 키친타월 3개")
        [{"item_name": "세정티슈", "reason": "없음"},
         {"item_name": "키친타월", "reason": "부족"}]
    """
    if not memo_text or not memo_text.strip():
        return []

    api_key = _get_api_key()
    if not api_key:
        logger.warning("[StockParse] GEMINI_API_KEY 미설정 — 파싱 스킵")
        return []

    try:
        client = genai.Client(api_key=api_key)
        model = _get_model_name()

        hint = _build_canonical_hint()
        contents = f"{hint}\n\n메모: {memo_text}" if hint else memo_text

        # response_schema로 JSON 배열 출력 강제. Gemini가 다른 텍스트 섞지 않음.
        # thinking_budget=0 — 2.5 Flash는 thinking 모델이라 기본값이면 추론에 토큰을
        # 먼저 써버려 빈 text가 나올 수 있음. 결정론적 추출 작업이므로 비활성화.
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_ITEMS_ARRAY_SCHEMA,
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                http_options=types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
            ),
        )

        parsed = _parse_response_text(response.text)
        # 응답 자체가 비어 있으면 실패로 간주 (API 장애 가능성)
        _record_parser_outcome(success=True)
        return parsed

    except APIError as e:
        # Gemini API 자체 오류 (rate limit, invalid key, timeout 등)
        err_str = str(e).lower()
        if "timeout" in err_str or "deadline" in err_str:
            logger.warning("[StockParse] Gemini API 타임아웃(%.0f초) — 빈 배열 반환",
                           _REQUEST_TIMEOUT_SEC)
        else:
            logger.exception("[StockParse] Gemini API 오류: %s", e)
        _record_parser_outcome(success=False, err_msg=str(e))
        return []

    except Exception as e:
        logger.exception("[StockParse] 예기치 못한 오류")
        _record_parser_outcome(success=False, err_msg=str(e))
        return []


def _parse_response_text(text: Optional[str]) -> list[dict]:
    """Gemini 응답 텍스트를 파싱하여 유효한 품목 리스트 반환.

    response_schema로 JSON을 강제했어도 방어적으로 재검증한다.
    각 항목의 item_name이 비어있거나 타입이 맞지 않으면 드롭.
    """
    if not text:
        logger.error("[StockParse] 빈 응답")
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.exception("[StockParse] JSON 파싱 실패: %r", text[:200])
        return []

    if not isinstance(data, list):
        logger.error("[StockParse] 응답이 배열이 아님: %r", type(data))
        return []

    result: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = (item.get("item_name") or "").strip()
        reason = (item.get("reason") or "").strip()
        current_stock = _coerce_stock(item.get("current_stock"))
        if name:
            result.append({
                "item_name": name,
                "reason": reason or "부족",
                "current_stock": current_stock,
            })

    logger.info("[StockParse] 메모 파싱 완료: %d개 품목 추출", len(result))
    return result


def _coerce_stock(value) -> Optional[int]:
    """Gemini가 내려준 current_stock 값을 정수로 안전 변환. 실패하면 None."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# =============================================================
# 배치 파싱 (PRD 4.2 성능) — 여러 메모를 1회 API 호출로 처리
# =============================================================

_BATCH_SYSTEM_PROMPT = """당신은 숙박업 재고 메모 분석기입니다. 다음 규칙으로 JSON 배열만 출력하세요.

입력은 여러 메모가 [ID] 구분자와 함께 합쳐진 텍스트입니다.
각 [ID]별로 아래 추출 규칙을 독립적으로 적용하고, event_id를 유지한 JSON 배열로 반환하세요.

추출 규칙:
1. 메모에 언급된 모든 재고 품목을 추출 (품목명이 나오면 기본적으로 부족한 것으로 간주)
2. "충분", "많이 있음", "넉넉" 등 재고가 넉넉하다는 표현만 있는 품목은 제외
3. 현재 재고 수량이 숫자로 명시되어 있으면 current_stock 필드에 정수로 기록 (예: "장작 2개 남음" → 2). 명시 없으면 null
4. 매핑표 품목 목록이 제공되면, 유사한 품목은 목록의 **정확한 이름**으로 정규화 (예: "스파클러" → "스파클라")
5. 출력은 반드시 JSON 배열, 다른 설명 금지

출력 형식 (event_id는 입력 [ID]와 동일, items가 비어있어도 항목 자체는 유지):
[
  {"event_id": "evt_1", "items": [{"item_name": "장작", "reason": "2개 남음", "current_stock": 2}]},
  {"event_id": "evt_2", "items": []}
]"""


def parse_shortage_items_batch(memos: list[dict]) -> dict:
    """여러 메모를 1회 API 호출로 병렬 추출.

    Args:
        memos: [{"event_id": str, "memo_text": str}, ...]
               비어 있거나 1건이면 개별 호출로 자연스럽게 폴백한다.

    Returns:
        {event_id: [{"item_name": str, "reason": str}, ...], ...}
        API 실패 시 개별 호출(`parse_shortage_items`)로 폴백하여 부분 결과라도 확보.
        완전 실패 시 빈 dict 반환.
    """
    if not memos:
        return {}

    # 1개면 배치 오버헤드가 더 크므로 단건 호출
    if len(memos) == 1:
        m = memos[0]
        eid = m.get("event_id", "")
        text = m.get("memo_text", "") or ""
        return {eid: parse_shortage_items(text)}

    api_key = _get_api_key()
    if not api_key:
        logger.warning("[StockParse] GEMINI_API_KEY 미설정 — 배치 파싱 스킵")
        return {}

    # 입력 텍스트 조립 — [ID] 구분자는 모델이 쉽게 인식하는 단순 포맷
    parts: list[str] = []
    for m in memos:
        eid = (m.get("event_id") or "").strip()
        text = (m.get("memo_text") or "").strip()
        if not eid or not text:
            continue
        parts.append(f"[{eid}]\n{text}")

    if not parts:
        return {}

    combined = "\n\n---\n\n".join(parts)

    try:
        client = genai.Client(api_key=api_key)
        model = _get_model_name()

        hint = _build_canonical_hint()
        contents = f"{hint}\n\n{combined}" if hint else combined

        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=_BATCH_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_BATCH_ARRAY_SCHEMA,
                max_output_tokens=_MAX_OUTPUT_TOKENS * 4,  # 여러 메모용 여유
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                http_options=types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
            ),
        )
    except APIError as e:
        logger.warning("[StockParse] 배치 API 실패 — 개별 호출로 폴백: %s", e)
        return _fallback_individual_calls(memos)
    except Exception:
        logger.exception("[StockParse] 배치 호출 예외 — 개별 호출로 폴백")
        return _fallback_individual_calls(memos)

    text = getattr(response, "text", None) or ""
    if not text:
        logger.warning("[StockParse] 배치 응답 비어있음 — 개별 호출로 폴백")
        return _fallback_individual_calls(memos)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.exception("[StockParse] 배치 JSON 파싱 실패 — 개별 호출로 폴백")
        return _fallback_individual_calls(memos)

    if not isinstance(data, list):
        logger.error("[StockParse] 배치 응답이 배열 아님 — 개별 호출로 폴백")
        return _fallback_individual_calls(memos)

    # 응답을 event_id → 정규화된 items 로 변환
    result: dict = {eid: [] for eid in (m.get("event_id", "") for m in memos) if eid}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        eid = (entry.get("event_id") or "").strip()
        raw_items = entry.get("items") or []
        if not eid or not isinstance(raw_items, list):
            continue
        clean: list[dict] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            name = (it.get("item_name") or "").strip()
            reason = (it.get("reason") or "").strip()
            current_stock = _coerce_stock(it.get("current_stock"))
            if name:
                clean.append({
                    "item_name": name,
                    "reason": reason or "부족",
                    "current_stock": current_stock,
                })
        result[eid] = clean

    total_items = sum(len(v) for v in result.values())
    logger.info("[StockParse] 배치 파싱 완료: 메모 %d건 → %d개 품목", len(memos), total_items)
    _record_parser_outcome(success=True)
    return result


def _fallback_individual_calls(memos: list[dict]) -> dict:
    """배치 실패 시 각 메모를 단건 호출로 순차 처리."""
    result: dict = {}
    for m in memos:
        eid = m.get("event_id", "")
        if not eid:
            continue
        try:
            result[eid] = parse_shortage_items(m.get("memo_text", "") or "")
        except Exception:
            logger.exception("[StockParse] 폴백 단건 호출 실패: %s", eid)
            result[eid] = []
    return result


def check_api_key_valid() -> tuple[bool, str]:
    """main.py --check 용도. 실제 소량 호출로 API 키 유효성을 검증.

    Returns:
        (유효 여부, 메시지) — 메시지는 콘솔에 그대로 표시된다.
    """
    api_key = _get_api_key()
    if not api_key:
        return False, "GEMINI_API_KEY 환경변수 미설정"

    try:
        client = genai.Client(api_key=api_key)
        model = _get_model_name()
        # thinking_budget=0: 2.5 Flash는 thinking 모델이라 기본값이면 소량 토큰을
        # 전부 추론에 써버려 빈 응답이 됨. 점검용 호출에서는 비활성화.
        response = client.models.generate_content(
            model=model,
            contents="ping",
            config=types.GenerateContentConfig(
                max_output_tokens=20,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                http_options=types.HttpOptions(timeout=10000),
            ),
        )
        if response.text:
            return True, f"Gemini {model} 연결 OK"
        return False, "응답 비어있음"
    except APIError as e:
        return False, f"API 오류: {e}"
    except Exception as e:
        return False, f"연결 실패: {e}"

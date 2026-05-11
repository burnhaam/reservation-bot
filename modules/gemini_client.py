"""Gemini API 호출 공통 유틸 + 자동 키 체인 fallback.

구성:
- 주 키:      환경변수 `GEMINI_API_KEY`
- 백업 키 N:  환경변수 `GEMINI_API_KEY_BACKUP`, `GEMINI_API_KEY_BACKUP2`,
              `GEMINI_API_KEY_BACKUP3`, ... (연속된 정수 suffix, 빈 슬롯에서 중단)

429(RESOURCE_EXHAUSTED) / quota 에러 발생 시 다음 키로 순차 전환. 한 번 소진된 키는
프로세스 수명 동안 다시 시도하지 않으며, 프로세스 재시작 시 주 키부터 다시 시도.

사용:
    from modules.gemini_client import generate_content_with_fallback
    response = generate_content_with_fallback(
        model="gemini-2.5-flash", contents="...", config=cfg,
    )
"""
import logging
import os
import threading
from typing import Any, Optional

try:
    from google import genai
    from google.genai.errors import APIError  # type: ignore
except ImportError:  # genai 미설치 환경 대비
    genai = None  # type: ignore
    APIError = Exception  # type: ignore


logger = logging.getLogger(__name__)

_lock = threading.Lock()
_active_index = 0  # 현재 사용 중인 키의 _key_chain() 인덱스


def _key_chain() -> list[str]:
    """주 키 → BACKUP → BACKUP2 → BACKUP3 → ... 순서로 비어있지 않은 키만 반환.

    BACKUP_N 은 N=2 부터 1씩 증가, 첫 빈 슬롯에서 중단. (BACKUP1 슬롯은 없음 — 그
    자리는 suffix 없는 GEMINI_API_KEY_BACKUP 가 차지)
    """
    chain: list[str] = []
    primary = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if primary:
        chain.append(primary)
    backup = (os.environ.get("GEMINI_API_KEY_BACKUP") or "").strip()
    if backup:
        chain.append(backup)
    idx = 2
    while True:
        key = (os.environ.get(f"GEMINI_API_KEY_BACKUP{idx}") or "").strip()
        if not key:
            break
        chain.append(key)
        idx += 1
    return chain


def get_active_key() -> str:
    """현재 활성 키 반환. 체인 끝을 넘어서면 빈 문자열."""
    chain = _key_chain()
    if not chain:
        return ""
    idx = min(_active_index, len(chain) - 1)
    return chain[idx]


def get_client() -> Optional[Any]:
    """현재 활성 키로 genai.Client 반환. 키 없거나 genai 미설치면 None."""
    if genai is None:
        return None
    key = get_active_key()
    if not key:
        return None
    try:
        return genai.Client(api_key=key)
    except Exception:
        logger.exception("[Gemini] Client 생성 실패")
        return None


def mark_quota_exhausted() -> bool:
    """현재 활성 키를 소진 표시 후 다음 키로 전환. 전환 성공 시 True.

    - 체인 끝(다음 키 없음) → False
    - 동시 접근 안전 (threading.Lock).
    """
    global _active_index
    with _lock:
        chain = _key_chain()
        next_index = _active_index + 1
        if next_index >= len(chain):
            return False
        _active_index = next_index
    # logger 호출은 lock 밖에서
    slot_label = "BACKUP" if _active_index == 1 else f"BACKUP{_active_index}"
    logger.warning(
        "[Gemini] 키 #%d 쿼터 소진 → %s 로 전환 (프로세스 재시작 시 주 키부터 재시도)",
        _active_index - 1, slot_label,
    )
    return True


def is_quota_error(exc: Exception) -> bool:
    """예외가 quota/429/RESOURCE_EXHAUSTED 인지 판정."""
    s = (str(exc) or "").lower()
    return "429" in s or "resource_exhausted" in s or "quota" in s


def generate_content_with_fallback(
    model: str,
    contents: Any,
    config: Any = None,
) -> Any:
    """Gemini generate_content 호출 + 429 시 자동 다음 키 재시도.

    호출 계약:
        genai.Client(api_key=...).models.generate_content(
            model=..., contents=..., config=...
        )
    과 동일한 응답 객체를 반환. config 미지정 시 생략.

    실패 케이스:
    - 키 없음 → RuntimeError
    - 체인 모든 키 소진 → 마지막 APIError 재throw
    - 비-quota 에러 → 즉시 재throw (재시도 안 함)
    """
    chain_len = max(1, len(_key_chain()))
    last_err: Optional[Exception] = None
    for _attempt in range(chain_len):
        client = get_client()
        if client is None:
            raise RuntimeError("Gemini 키 미설정 또는 genai 모듈 미설치")
        try:
            kwargs: dict = {"model": model, "contents": contents}
            if config is not None:
                kwargs["config"] = config
            return client.models.generate_content(**kwargs)
        except APIError as e:
            last_err = e
            if is_quota_error(e) and mark_quota_exhausted():
                logger.info("[Gemini] 다음 키로 재시도")
                continue
            raise
    if last_err is not None:
        raise last_err
    raise RuntimeError("Gemini 호출 실패")

"""Gemini API 호출 공통 유틸 + 자동 키 fallback.

구성:
- 주 키: 환경변수 `GEMINI_API_KEY`
- 백업 키 (선택): 환경변수 `GEMINI_API_KEY_BACKUP`

주 키 호출이 429(RESOURCE_EXHAUSTED) / quota 에러 반환 시 자동으로 백업 키로 전환.
전환 플래그는 프로세스 수명 동안 유지되며, 프로세스 재시작 시 주 키부터 다시 시도.

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
_use_backup = False  # 프로세스 레벨 전환 플래그


def _primary_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


def _backup_key() -> str:
    return (os.environ.get("GEMINI_API_KEY_BACKUP") or "").strip()


def get_active_key() -> str:
    """현재 활성 키 반환. 전환 상태 반영."""
    primary = _primary_key()
    backup = _backup_key()
    if _use_backup and backup:
        return backup
    return primary or backup


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
    """현재 활성 키를 소진 표시. 전환 성공 시 True.

    - 이미 백업 사용 중이거나 백업 키 없으면 False (전환 불가).
    - 동시 접근 안전 (threading.Lock).
    """
    global _use_backup
    with _lock:
        if _use_backup:
            return False
        if not _backup_key():
            return False
        _use_backup = True
    logger.warning(
        "[Gemini] 주 키 쿼터 소진 → 백업 키로 전환 "
        "(프로세스 재시작 시 주 키부터 재시도)"
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
    """Gemini generate_content 호출 + 429 시 자동 백업 키 재시도.

    호출 계약:
        genai.Client(api_key=...).models.generate_content(
            model=..., contents=..., config=...
        )
    과 동일한 응답 객체를 반환. config 미지정 시 생략.

    실패 케이스:
    - 키 없음 → RuntimeError
    - 주/백업 모두 소진 → 마지막 APIError 재throw
    - 비-quota 에러 → 즉시 재throw (재시도 안 함)
    """
    last_err: Optional[Exception] = None
    for _attempt in range(2):  # 최대 2회 (주 → 백업)
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
                logger.info("[Gemini] 백업 키로 재시도")
                continue
            raise
    if last_err is not None:
        raise last_err
    raise RuntimeError("Gemini 호출 실패")

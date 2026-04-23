"""
구글 캘린더 연동 모듈.

구글 캘린더 API에 종일 일정을 등록/삭제한다.
운영자용(캘린더 A)과 알바용(캘린더 B) 두 곳에 동시에 기록하며,
D-7 정오, D-1 정오 팝업 알림이 설정된다.

외부 API 호출 실패는 예외로 던지지 않고 로그만 남긴 뒤 None을 반환한다.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from modules.config_loader import load_config


logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_google_calendar_id_cache: dict[str, str] = {}


# =============================================================
# 이름 정규화
# =============================================================

def _normalize_korean_name(name: str) -> str:
    """한국어 이름의 '이름 성' 순서를 '성 이름'으로 변환. 영문은 그대로 유지."""
    parts = name.split()
    if len(parts) == 2 and all(re.match(r'^[가-힣]+$', p) for p in parts):
        given, family = parts
        if len(family) == 1 and len(given) >= 1:
            return f"{family} {given}"
    return name


# =============================================================
# 구글 캘린더 API
# =============================================================

_cached_service = None
_cached_creds = None


def _get_google_calendar_service():
    """구글 캘린더 API 서비스 객체 반환. 유효한 캐시가 있으면 재사용."""
    global _cached_service, _cached_creds
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_path = _PROJECT_ROOT / "token_calendar.json"

    if _cached_creds and _cached_creds.valid and _cached_service:
        return _cached_service

    if _cached_creds and _cached_creds.expired and _cached_creds.refresh_token:
        _cached_creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(_cached_creds.to_json())
    else:
        _cached_creds = Credentials.from_authorized_user_file(str(token_path), _GOOGLE_SCOPES)
        if _cached_creds.expired and _cached_creds.refresh_token:
            _cached_creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(_cached_creds.to_json())

    _cached_service = build("calendar", "v3", credentials=_cached_creds, cache_discovery=False)
    return _cached_service


def _resolve_google_calendar_id(service, name: str) -> Optional[str]:
    """구글 캘린더 이름으로 calendarId 조회."""
    if name in _google_calendar_id_cache:
        return _google_calendar_id_cache[name]

    try:
        result = service.calendarList().list().execute()
        for cal in result.get("items", []):
            _google_calendar_id_cache[cal["summary"]] = cal["id"]
    except Exception as e:
        logger.error("[Google] 캘린더 목록 조회 실패: %s", e)
        return None

    cal_id = _google_calendar_id_cache.get(name)
    if not cal_id:
        logger.error("[Google] '%s' 캘린더를 찾을 수 없음", name)
    return cal_id


def _create_google_event(
    service,
    calendar_name: str,
    summary: str,
    checkin: date,
    checkout: date,
) -> Optional[str]:
    """구글 캘린더에 종일 일정 생성. 반환: eventId 또는 None."""
    cal_id = _resolve_google_calendar_id(service, calendar_name)
    if not cal_id:
        return None

    event_body = {
        "summary": summary,
        "start": {"date": checkin.isoformat()},
        "end": {"date": checkout.isoformat()},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 9360},
                {"method": "popup", "minutes": 720},
            ],
        },
    }

    try:
        event = service.events().insert(calendarId=cal_id, body=event_body).execute()
        event_id = event.get("id")
        logger.info("[Google] 일정 등록 OK (calendar=%s, eventId=%s, summary=%s)",
                     calendar_name, event_id, summary)
        return event_id
    except Exception as e:
        logger.error("[Google] 일정 생성 실패 (calendar=%s): %s", calendar_name, e)
        return None


def _delete_google_event(service, calendar_name: str, event_id: str) -> bool:
    """구글 캘린더에서 일정 삭제."""
    cal_id = _resolve_google_calendar_id(service, calendar_name)
    if not cal_id:
        return False

    try:
        service.events().delete(calendarId=cal_id, eventId=event_id).execute()
        logger.info("[Google] 일정 삭제 OK (calendar=%s, eventId=%s)", calendar_name, event_id)
        return True
    except Exception as e:
        logger.error("[Google] 일정 삭제 실패 (calendar=%s, eventId=%s): %s",
                     calendar_name, event_id, e)
        return False


# =============================================================
# 통합 일정 생성/삭제
# =============================================================

def create_events(reservation: dict) -> dict:
    """구글 캘린더 A, B에 종일 일정을 생성.

    반환: {"google_a": str|None, "google_b": str|None}
    """
    result = {"google_a": None, "google_b": None}

    try:
        config = load_config()
    except Exception as e:
        logger.error("[Calendar] config 로드 실패: %s", e)
        return result

    platform = reservation.get("platform", "")
    prefix = config.get("platform_prefix", {}).get(platform, "")
    raw_name = reservation.get("guest_name") or "예약자"
    guest_name = "(예약됨)" if raw_name == "Reserved" else _normalize_korean_name(raw_name)
    guests = reservation.get("guests")
    guests_str = f"{guests}" if guests is not None else "1"
    checkin = reservation.get("checkin")
    checkout = reservation.get("checkout")

    if not (isinstance(checkin, date) and isinstance(checkout, date)):
        logger.error("[Calendar] 유효하지 않은 checkin/checkout: %s / %s", checkin, checkout)
        return result

    nights = (checkout - checkin).days
    summary_a = f"{prefix}. {guest_name}. {guests_str}인"
    staff_name = config.get("staff_name", "")
    if nights > 1:
        summary_b = f"{staff_name} / 성인 {guests_str}명 (연박{nights}배)"
    else:
        summary_b = f"{staff_name} / 성인 {guests_str}명"

    owner_cal = config.get("naver_owner_calendar", "")
    staff_cal = config.get("naver_staff_calendar", "")

    # 캘린더 B: 체크인 당일 하루만
    from datetime import timedelta
    checkin_one_day = checkin + timedelta(days=1)

    try:
        gsvc = _get_google_calendar_service()
        result["google_a"] = _create_google_event(gsvc, owner_cal, summary_a, checkin, checkout)
        result["google_b"] = _create_google_event(gsvc, staff_cal, summary_b, checkin, checkin_one_day)
    except Exception as e:
        logger.error("[Google] 캘린더 서비스 초기화 실패: %s", e)

    return result


def update_event_summary(
    google_event_id: Optional[str],
    calendar_name: str,
    new_summary: str,
) -> bool:
    """구글 캘린더 일정 제목 업데이트."""
    if not google_event_id:
        return False

    try:
        gsvc = _get_google_calendar_service()
    except Exception as e:
        logger.error("[Google] 서비스 초기화 실패: %s", e)
        return False

    cal_id = _resolve_google_calendar_id(gsvc, calendar_name)
    if not cal_id:
        return False

    try:
        event = gsvc.events().get(calendarId=cal_id, eventId=google_event_id).execute()
        event["summary"] = new_summary
        gsvc.events().update(calendarId=cal_id, eventId=google_event_id, body=event).execute()
        logger.info("[Google] 일정 제목 업데이트 OK (calendar=%s, summary=%s)", calendar_name, new_summary)
        return True
    except Exception as e:
        logger.error("[Google] 일정 업데이트 실패 (calendar=%s): %s", calendar_name, e)
        return False


def update_event_dates(
    google_event_id: Optional[str],
    calendar_name: str,
    new_checkin: date,
    new_checkout: date,
    new_summary: Optional[str] = None,
) -> bool:
    """구글 캘린더 일정 날짜(+제목) 업데이트."""
    if not google_event_id:
        return False

    try:
        gsvc = _get_google_calendar_service()
    except Exception as e:
        logger.error("[Google] 서비스 초기화 실패: %s", e)
        return False

    cal_id = _resolve_google_calendar_id(gsvc, calendar_name)
    if not cal_id:
        return False

    try:
        event = gsvc.events().get(calendarId=cal_id, eventId=google_event_id).execute()
        event["start"] = {"date": new_checkin.isoformat()}
        event["end"] = {"date": new_checkout.isoformat()}
        if new_summary:
            event["summary"] = new_summary
        gsvc.events().update(calendarId=cal_id, eventId=google_event_id, body=event).execute()
        logger.info("[Google] 일정 날짜 업데이트 OK (calendar=%s, %s~%s)",
                     calendar_name, new_checkin, new_checkout)
        return True
    except Exception as e:
        logger.error("[Google] 일정 날짜 업데이트 실패 (calendar=%s): %s", calendar_name, e)
        return False


def delete_events(
    google_event_id_a: Optional[str] = None,
    google_event_id_b: Optional[str] = None,
) -> None:
    """구글 캘린더에서 일정 삭제."""
    if not google_event_id_a and not google_event_id_b:
        return

    try:
        gsvc = _get_google_calendar_service()
        config = load_config()
    except Exception as e:
        logger.error("[Google] 삭제 초기화 실패: %s", e)
        return

    if google_event_id_a:
        _delete_google_event(gsvc, config.get("naver_owner_calendar", ""), google_event_id_a)
    if google_event_id_b:
        _delete_google_event(gsvc, config.get("naver_staff_calendar", ""), google_event_id_b)


# =============================================================
# 특정 날짜 이벤트 조회
# =============================================================

def list_events_on_date(calendar_name: str, target_date: date) -> list[dict]:
    """지정 캘린더에서 target_date에 시작하는 일정 목록을 반환.

    반환 각 항목: {"event_id", "summary", "start_date"}.
    실패 시 빈 리스트.
    """
    import socket
    socket.setdefaulttimeout(30)

    try:
        gsvc = _get_google_calendar_service()
    except Exception:
        logger.exception("[Calendar] 서비스 초기화 실패")
        return []

    cal_id = _resolve_google_calendar_id(gsvc, calendar_name)
    if not cal_id:
        return []

    next_day = target_date + timedelta(days=1)
    try:
        resp = gsvc.events().list(
            calendarId=cal_id,
            timeMin=target_date.isoformat() + "T00:00:00+09:00",
            timeMax=next_day.isoformat() + "T00:00:00+09:00",
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
    except Exception:
        logger.exception("[Calendar] 이벤트 조회 실패 (calendar=%s, date=%s)",
                         calendar_name, target_date)
        return []

    result = []
    for ev in resp.get("items", []):
        start = ev.get("start", {})
        result.append({
            "event_id": ev.get("id", ""),
            "summary": ev.get("summary", "") or "",
            "start_date": start.get("date") or start.get("dateTime", ""),
        })
    return result


# =============================================================
# 재고 메모 조회 (stock 자동주문)
# =============================================================

def read_stock_memos(calendar_name: str, lookback_days: int) -> list[dict]:
    """지정 캘린더의 최근 N일 + 향후 1일 일정 중 description이 있는 것만 반환.

    기존 _get_google_calendar_service()를 재활용하여 구글 캘린더에서 일정을 조회한다.
    반환 각 항목: {"event_id", "summary", "description", "start_date"}
    description이 빈 문자열이거나 None인 일정은 제외한다.
    """
    # 안정성: Google API 호출이 소켓 레벨에서 영원히 매달리지 않도록 30초 한도 지정.
    # 프로세스 전역 기본값을 변경하므로 최초 1회만 설정되면 이후 호출에도 적용된다.
    import socket
    socket.setdefaulttimeout(30)

    try:
        gsvc = _get_google_calendar_service()
    except Exception:
        logger.exception("[Stock] 구글 캘린더 서비스 초기화 실패")
        return []

    cal_id = _resolve_google_calendar_id(gsvc, calendar_name)
    if not cal_id:
        logger.error("[Stock] 캘린더를 찾을 수 없음: %s", calendar_name)
        return []

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=lookback_days)).isoformat()
    time_max = (now + timedelta(days=1)).isoformat()

    try:
        resp = gsvc.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
        ).execute()
    except Exception:
        logger.exception("[Stock] 캘린더 이벤트 조회 실패: %s", calendar_name)
        return []

    memos: list[dict] = []
    for ev in resp.get("items", []):
        desc = (ev.get("description") or "").strip()
        if not desc:
            continue
        start = ev.get("start", {})
        start_date = start.get("date") or start.get("dateTime") or ""
        memos.append({
            "event_id": ev.get("id", ""),
            "summary": ev.get("summary", ""),
            "description": desc,
            "start_date": start_date,
        })

    logger.info("[Stock] 메모 조회 완료: %d건 (캘린더=%s)", len(memos), calendar_name)
    return memos

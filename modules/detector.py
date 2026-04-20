"""
예약 감지 모듈.

에어비앤비(iCal 구독 URL)와 네이버 플레이스(Gmail 알림 메일)에서
신규 예약 및 취소를 감지해 통일된 dict 포맷으로 반환한다.

반환 dict 구조:
    {
        "platform":   "airbnb" | "naver",
        "booking_id": str,
        "guest_name": str | None,
        "guests":     int | None,
        "checkin":    datetime.date | None,
        "checkout":   datetime.date | None,
        "action":     "new" | "cancel",
    }
"""

import base64
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from icalendar import Calendar

from modules.db import get_connection
from modules.env_loader import load_env


logger = logging.getLogger(__name__)


# =============================================================
# 공통 헬퍼
# =============================================================

def _get_known_reservations(platform: str) -> dict[str, dict]:
    """DB에 저장된 특정 플랫폼 예약을 booking_id → row 매핑으로 반환."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, guest_name, guests, checkin, checkout, status "
            "FROM reservations WHERE platform = ?",
            (platform,),
        )
        return {row["booking_id"]: dict(row) for row in cur.fetchall()}


def _to_date(value) -> Optional[date]:
    """datetime/date/문자열을 date 객체로 통일 변환."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        txt = value.replace(".", "-").replace("/", "-").strip()
        try:
            return datetime.strptime(txt, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# =============================================================
# 에어비앤비 (iCal)
# =============================================================

# 에어비앤비가 차단 기간에 내려주는 더미 이벤트 (실제 예약 아님)
_AIRBNB_BLOCKED_KEYWORDS = ("Not available", "Airbnb (Not available)")


def _download_airbnb_ical(url: str) -> Optional[bytes]:
    """iCal URL에서 파일 다운로드. 네트워크 오류 시 None 반환."""
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        logger.error("[Airbnb] iCal 다운로드 실패: %s", e)
        return None


def _parse_guests_from_description(description: str) -> Optional[int]:
    """DESCRIPTION 필드에서 인원 수를 추출. 패턴 미발견 시 None."""
    # 'Guests: 3', '인원: 3명', 'Guest 3' 등 유연하게 매칭
    match = re.search(r"(?:Guests?|인원)\s*[:：]?\s*(\d+)", description, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _parse_airbnb_ical(ical_bytes: bytes) -> list[dict]:
    """iCal 바이트를 파싱해 예약 이벤트 리스트 반환."""
    cal = Calendar.from_ical(ical_bytes)
    events: list[dict] = []

    for component in cal.walk("VEVENT"):
        summary = str(component.get("SUMMARY", ""))

        # 차단(blocked) 이벤트는 실제 예약이 아니므로 건너뜀
        if any(kw in summary for kw in _AIRBNB_BLOCKED_KEYWORDS):
            continue

        uid = str(component.get("UID", "")).strip()
        if not uid:
            continue

        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")
        description = str(component.get("DESCRIPTION", ""))

        events.append({
            "booking_id": uid,
            "guest_name": summary or None,
            "guests": _parse_guests_from_description(description),
            "checkin": _to_date(dtstart.dt if dtstart else None),
            "checkout": _to_date(dtend.dt if dtend else None),
        })

    return events


def _parse_checkin_from_body(body: str) -> Optional[date]:
    """본문에서 체크인 날짜 파싱.
    형식: '체크인 ... 체크아웃' 줄 다음에 '4월 17일 (금)   4월 18일 (토)'
    또는 '님이 4월 17일에 체크인할'
    """
    # 패턴1: "체크인  체크아웃" 줄 다음에 "N월 N일"
    match = re.search(
        r"체크인\s+체크아웃\s*[\r\n]+\s*(\d{1,2})월\s*(\d{1,2})일",
        body,
    )
    # 패턴2: "님이 N월 N일에 체크인할"
    if not match:
        match = re.search(r"님이\s*(\d{1,2})월\s*(\d{1,2})일에\s*체크인", body)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    year = date.today().year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None
    # 6개월 이상 과거면 다음 해로 추정
    if (date.today() - candidate).days > 180:
        candidate = date(year + 1, month, day)
    return candidate


def _parse_name_from_subject(subject: str) -> Optional[str]:
    """제목에서 예약자 전체 이름 추출.
    형식: '예약 확정 - 근영 조 님이 4월 30일에 체크인할 예정입니다'
    """
    match = re.search(r"예약\s*확정\s*-\s*(.+?)\s*님이", subject)
    if match:
        return match.group(1).strip()
    return None


def _extract_airbnb_info_from_gmail(checkin: Optional[date]) -> Optional[dict]:
    """Gmail에서 에어비앤비 예약 확정 이메일을 조회하여 예약자 정보를 추출.

    제목에서 전체 이름(성 포함), 본문에서 인원수/예약코드를 추출한다.
    반환: {"guest_name": str, "guests": int, "reservation_code": str} 또는 None
    """
    if not checkin:
        return None

    try:
        service = _get_gmail_service()
    except Exception:
        return None

    query = 'from:automated@airbnb.com subject:"예약 확정"'

    try:
        list_resp = service.users().messages().list(
            userId="me", q=query, maxResults=30
        ).execute()
        message_refs = list_resp.get("messages", [])
    except Exception as e:
        logger.warning("[Airbnb] Gmail 예약 확정 메일 조회 실패: %s", e)
        return None

    for ref in message_refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
        except Exception:
            continue

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("Subject", "")
        body = _decode_gmail_body(msg.get("payload", {}))
        if not body:
            continue

        email_checkin = _parse_checkin_from_body(body)
        if not email_checkin or email_checkin != checkin:
            continue

        info: dict = {}

        subject_name = _parse_name_from_subject(subject)
        if subject_name:
            info["guest_name"] = subject_name

        guests_match = re.search(r"성인\s*(\d+)\s*명", body)
        if guests_match:
            info["guests"] = int(guests_match.group(1))

        code_match = re.search(r"예약\s*코드\s*[\r\n]+\s*([A-Z0-9]{6,})", body)
        if code_match:
            info["reservation_code"] = code_match.group(1).strip()

        if info.get("guest_name"):
            logger.info(
                "[Airbnb] Gmail에서 예약 정보 추출: 이름=%s, 인원=%s, 코드=%s",
                info.get("guest_name"), info.get("guests"), info.get("reservation_code"),
            )
            return info

    return None


def detect_airbnb() -> list[dict]:
    """에어비앤비 신규/취소 예약 감지."""
    env = load_env()
    url = env.get("AIRBNB_ICAL_URL", "")
    if not url:
        logger.warning("[Airbnb] AIRBNB_ICAL_URL 환경변수가 비어있음")
        return []

    ical_bytes = _download_airbnb_ical(url)
    if ical_bytes is None:
        return []

    try:
        current_events = _parse_airbnb_ical(ical_bytes)
    except Exception as e:
        logger.error("[Airbnb] iCal 파싱 실패: %s", e)
        return []
    finally:
        del ical_bytes

    known = _get_known_reservations("airbnb")
    current_ids = {e["booking_id"] for e in current_events}
    today = date.today()

    results: list[dict] = []

    # 체크인 날짜 → booking_id 매핑 (웹훅 임시 저장 건 포함)
    known_by_checkin: dict[str, str] = {}
    for kid, krow in known.items():
        ci = krow.get("checkin", "")
        if ci and krow.get("status") != "cancelled":
            known_by_checkin[ci] = kid

    # 신규: iCal에 있지만 DB에 없음
    for event in current_events:
        checkin_str = event["checkin"].isoformat() if event.get("checkin") else ""

        # booking_id로 이미 DB에 있으면 skip
        if event["booking_id"] in known:
            continue
        # 같은 체크인 날짜에 이미 처리된 건 있으면 skip (웹훅 임시 저장 건은 main.py에서 업그레이드)
        if checkin_str in known_by_checkin:
            continue

        guest_name = event.get("guest_name")
        if not guest_name or guest_name == "Reserved":
            gmail_info = _extract_airbnb_info_from_gmail(event.get("checkin"))
            if gmail_info:
                event["guest_name"] = gmail_info.get("guest_name", "(예약됨)")
                if gmail_info.get("guests") is not None:
                    event["guests"] = gmail_info["guests"]
            else:
                event["guest_name"] = "(예약됨)"
        results.append({"platform": "airbnb", "action": "new", **event})

    # 취소: DB에 있지만 iCal에서 사라짐 (이미 취소 처리된 건 제외)
    for booking_id, row in known.items():
        if booking_id in current_ids:
            continue
        if row.get("status") == "cancelled":
            continue

        checkout = _to_date(row.get("checkout"))
        if checkout and checkout < today:
            continue

        results.append({
            "platform": "airbnb",
            "action": "cancel",
            "booking_id": booking_id,
            "guest_name": row.get("guest_name"),
            "guests": row.get("guests"),
            "checkin": _to_date(row.get("checkin")),
            "checkout": checkout,
        })

    return results


# =============================================================
# 네이버 플레이스 (Gmail 파싱)
# =============================================================

_NAVER_GMAIL_QUERY = (
    'newer_than:1d '
    '(from:noreply@naver.com OR from:naverbooking_noreply@navercorp.com OR subject:"네이버 예약")'
)


_cached_gmail_service = None


def _get_gmail_service():
    """Gmail API 서비스 객체 반환. 유효한 캐시가 있으면 재사용."""
    global _cached_gmail_service
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if _cached_gmail_service:
        return _cached_gmail_service

    project_root = Path(__file__).resolve().parent.parent
    token_path = project_root / "token_gmail.json"
    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]

    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    _cached_gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _cached_gmail_service


def _decode_gmail_body(payload: dict) -> str:
    """Gmail payload 트리를 재귀적으로 훑어 본문 텍스트 반환."""
    # 단일 part 메시지
    data = payload.get("body", {}).get("data")
    if data and payload.get("mimeType", "").startswith("text/"):
        decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        if payload["mimeType"] == "text/html":
            decoded = re.sub(r"<[^>]+>", " ", decoded)
        return decoded

    # multipart: text/plain 우선 탐색
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            body = _decode_gmail_body(part)
            if body:
                return body

    # text/plain이 없으면 text/html 또는 중첩 multipart 탐색
    for part in payload.get("parts", []):
        body = _decode_gmail_body(part)
        if body:
            return body

    return ""


def _parse_naver_email(msg: dict) -> Optional[dict]:
    """네이버 예약 알림 메일 1건을 파싱해 예약 정보 dict 반환."""
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "")

    body = _decode_gmail_body(msg.get("payload", {}))
    if not body:
        return None

    # 예약번호 (필수) — 없으면 해당 메일은 스킵
    booking_match = re.search(r"예약번호\s*[:：]?\s*(\d+)", body)
    if not booking_match:
        return None
    booking_id = booking_match.group(1)

    # 예약자명 (한글/영문 허용, 개행 또는 2칸 이상 공백 전까지)
    name_match = re.search(
        r"예약자(?:명|\s*성함)?\s*[:：]?\s*([가-힣A-Za-z][가-힣A-Za-z\s]*?)(?=\n|\r|\s{2,}|$)",
        body,
    )
    guest_name = name_match.group(1).strip() if name_match else None

    # 인원: "인원추가 [13세 이상](2)" 등에서 괄호 안 숫자를 합산 + 기본 인원
    from modules.config_loader import load_config
    base_guests = load_config().get("base_guests", 2)
    extra_matches = re.findall(r"인원추가\s*\[.*?\]\s*\((\d+)\)", body)
    if extra_matches:
        guests = base_guests + sum(int(n) for n in extra_matches)
    else:
        guests_match = re.search(r"인원\s*[:：]?\s*(\d+)", body)
        guests = int(guests_match.group(1)) if guests_match else base_guests

    # 이용일시 "2026.05.27.(수)~2026.05.28.(목)(1박 2일)"
    checkin_date = None
    checkout_date = None
    usage_match = re.search(
        r"이용일시\s*(\d{4}\.\d{2}\.\d{2})\.\([가-힣]\)\s*~\s*(\d{4}\.\d{2}\.\d{2})",
        body,
    )
    if usage_match:
        checkin_date = _to_date(usage_match.group(1))
        checkout_date = _to_date(usage_match.group(2))
    else:
        date_pat = r"(\d{4}[-./]\d{1,2}[-./]\d{1,2})"
        checkin_match = re.search(rf"체크인\s*[:：]?\s*{date_pat}", body)
        checkout_match = re.search(rf"체크아웃\s*[:：]?\s*{date_pat}", body)
        checkin_date = _to_date(checkin_match.group(1)) if checkin_match else None
        checkout_date = _to_date(checkout_match.group(1)) if checkout_match else None

    return {
        "booking_id": booking_id,
        "guest_name": guest_name,
        "guests": guests,
        "checkin": checkin_date,
        "checkout": checkout_date,
        "action": "cancel" if "취소" in subject else "new",
    }


def detect_naver() -> list[dict]:
    """네이버 플레이스 신규/취소 예약 감지 (Gmail 파싱)."""
    try:
        service = _get_gmail_service()
    except FileNotFoundError:
        logger.warning("[Naver] token.json이 없어 Gmail 조회를 건너뜀")
        return []
    except Exception as e:
        logger.error("[Naver] Gmail 서비스 초기화 실패: %s", e)
        return []

    try:
        list_resp = service.users().messages().list(
            userId="me", q=_NAVER_GMAIL_QUERY
        ).execute()
        message_refs = list_resp.get("messages", [])
    except Exception as e:
        logger.error("[Naver] Gmail 메일 목록 조회 실패: %s", e)
        return []

    known = _get_known_reservations("naver")
    # 체크인 날짜 → booking_id 매핑 (웹훅 예약 중복 체크용)
    known_by_checkin: dict[str, str] = {}
    for kid, krow in known.items():
        ci = krow.get("checkin", "")
        if ci and krow.get("status") != "cancelled":
            known_by_checkin[ci] = kid

    results: list[dict] = []

    for ref in message_refs:
        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
        except Exception as e:
            logger.error("[Naver] Gmail 메일 상세 조회 실패 (id=%s): %s", ref["id"], e)
            continue

        parsed = _parse_naver_email(msg)
        if not parsed:
            continue

        booking_id = parsed["booking_id"]
        action = parsed["action"]
        checkin_str = parsed.get("checkin").isoformat() if parsed.get("checkin") else ""

        if action == "new":
            # 예약번호로 중복 체크
            if booking_id in known:
                continue
            # 같은 체크인 날짜로 웹훅 예약 중복 체크
            if checkin_str in known_by_checkin:
                continue
            # guest_name 파싱 실패 시 '확인필요'
            if not parsed.get("guest_name"):
                parsed["guest_name"] = "확인필요"
            results.append({"platform": "naver", **parsed})
            continue

        if action == "cancel":
            # 예약번호로 직접 매칭
            if booking_id in known and known[booking_id].get("status") != "cancelled":
                results.append({"platform": "naver", **parsed})
                continue
            # 체크인 날짜로 웹훅 예약 매칭
            matched_id = known_by_checkin.get(checkin_str)
            if matched_id:
                parsed["booking_id"] = matched_id
                results.append({"platform": "naver", **parsed})
            continue

    return results


# =============================================================
# 통합 엔트리포인트
# =============================================================

def detect_new_reservations() -> list[dict]:
    """에어비앤비 + 네이버의 신규/취소 예약을 합쳐서 반환."""
    return detect_airbnb() + detect_naver()

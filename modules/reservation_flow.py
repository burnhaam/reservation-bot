"""
예약 확정 플로우 — 웹훅/iCal 트리거와 Gmail 정보를 '합친 뒤' 확정 처리.

핵심 원칙: 트리거(웹훅 또는 iCal diff)만으로는 캘린더/알림을 만들지 않는다.
트리거 시점에 pending_email 로 DB에 claim 해두고, Gmail에서 인원수(네이버)/
이름·인원(에어비앤비)을 확인하면 그때 캘린더 생성 + Discord 알림 + (네이버는)
에어비앤비 차단을 수행한다.

재시도 구조:
- 0~5분: webhook_server 의 60초 워처가 1분 간격으로 finalize 시도 (fast).
- 5~60분: main.py 5분 파이프라인이 finalize 시도.
- 60분 경과: 파이프라인이 기본 인원(base_guests)으로 강제 확정.

webhook_server.py(실시간)와 main.py(파이프라인)가 공유한다. 모든 finalize 는
조건부 UPDATE(rowcount) 로 경합에 안전 — 동시에 여러 곳에서 호출돼도 1건만 확정된다.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from modules import blocker, calendar as cal_mod, notifier
from modules.config_loader import load_config
from modules.db import get_connection


logger = logging.getLogger(__name__)


# 인원/정보 메일 대기 한도(분). 초과 시 기본 인원으로 강제 확정.
PENDING_TTL_MINUTES = 60
# webhook_server 워처가 담당하는 빠른 재시도 창(분).
FAST_WINDOW_MINUTES = 5


def _to_date(value) -> Optional[date]:
    """문자열/date/datetime → date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        txt = value.replace(".", "-").replace("/", "-").strip()
        try:
            return datetime.strptime(txt[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _now_utc_naive() -> datetime:
    """SQLite CURRENT_TIMESTAMP(UTC naive)와 비교하기 위한 현재 UTC(naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _age_minutes(created_at: Optional[str]) -> Optional[float]:
    """created_at(UTC naive 문자열) 이후 경과 분. 파싱 실패 시 None."""
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError:
        try:
            dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (_now_utc_naive() - dt).total_seconds() / 60.0


# =============================================================
# 트리거 → pending_email 로 claim (중복 방지)
# =============================================================

def claim_pending(reservation: dict) -> bool:
    """트리거된 예약을 pending_email 상태로 원자적 등록.

    같은 체크인의 비취소 예약이 이미 있으면(다른 폰 웹훅, 또는 웹훅+iCal 중복 트리거)
    중복으로 보고 False. 신규 등록에 성공하면 True.

    단일 숙소 가정: 같은 체크인 날짜에 동시에 두 건의 서로 다른 예약은 없다.
    """
    platform = reservation.get("platform")
    booking_id = reservation.get("booking_id")
    checkin = reservation.get("checkin")
    checkout = reservation.get("checkout")
    if not (platform and booking_id and isinstance(checkin, date) and isinstance(checkout, date)):
        logger.error("[Flow] claim_pending: 유효하지 않은 예약 %s", reservation)
        return False

    checkin_iso = checkin.isoformat()
    checkout_iso = checkout.isoformat()

    conn = get_connection()
    try:
        # BEGIN IMMEDIATE 로 쓰기 락을 선점해 SELECT+INSERT 를 프로세스 간 직렬화.
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT booking_id, status FROM reservations "
            "WHERE platform = ? AND checkin = ? AND status != 'cancelled'",
            (platform, checkin_iso),
        ).fetchone()
        if existing:
            conn.execute("ROLLBACK")
            logger.info(
                "[Flow] 중복 트리거 skip: %s/%s (기존 %s/%s)",
                platform, booking_id, existing["booking_id"], existing["status"],
            )
            return False

        # 같은 booking_id가 취소 상태로 남아있으면(동일 건 재예약) 되살린다.
        # (위에서 비취소 동일 체크인은 이미 걸렀으므로 여기 걸리는 건 취소된 행뿐)
        same_id = conn.execute(
            "SELECT status FROM reservations WHERE booking_id = ?", (booking_id,)
        ).fetchone()
        if same_id:
            conn.execute(
                "UPDATE reservations SET status = 'pending_email', guest_name = ?, "
                "  guests = NULL, checkin = ?, checkout = ?, "
                "  google_event_id_a = NULL, google_event_id_b = NULL, "
                "  samhaengsi_sent = 0, unprocessed_alert_sent = 0, "
                "  created_at = CURRENT_TIMESTAMP "
                "WHERE booking_id = ?",
                (reservation.get("guest_name"), checkin_iso, checkout_iso, booking_id),
            )
            conn.execute("COMMIT")
            logger.info("[Flow] 취소건 재예약 되살림: %s/%s (체크인 %s)",
                        platform, booking_id, checkin_iso)
            return True

        conn.execute(
            "INSERT INTO reservations "
            "(platform, booking_id, guest_name, guests, checkin, checkout, status) "
            "VALUES (?, ?, ?, NULL, ?, ?, 'pending_email')",
            (platform, booking_id, reservation.get("guest_name"), checkin_iso, checkout_iso),
        )
        conn.execute("COMMIT")
        logger.info("[Flow] pending 등록: %s/%s (체크인 %s)", platform, booking_id, checkin_iso)
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("[Flow] claim_pending 실패: %s/%s", platform, booking_id)
        return False
    finally:
        conn.close()


# =============================================================
# Gmail 정보 병합 → 확정 (캘린더 + 알림 + 차단)
# =============================================================

def _lookup_email_info(platform: str, checkin_iso: str, checkin: date) -> Optional[dict]:
    """플랫폼별 Gmail 확정 정보 조회. 없으면 None.

    네이버: 인원수(이름은 마스킹되므로 웹훅 값 유지).
    에어비앤비: 이름(풀네임) + 인원수.
    """
    from modules import detector
    try:
        if platform == "naver":
            return detector.collect_naver_email_data().get(checkin_iso)
        return detector._extract_airbnb_info_from_gmail(checkin)
    except Exception:
        logger.exception("[Flow] %s Gmail 정보 조회 실패 (checkin=%s)", platform, checkin_iso)
        return None


def finalize_if_ready(booking_id: str, *, force_base: bool = False) -> str:
    """pending_email 예약을 Gmail 정보와 합쳐 확정. 경합 안전.

    반환: 'confirmed' | 'pending' | 'skip'(대상 없음/경합 패배).
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT platform, booking_id, guest_name, checkin, checkout "
            "FROM reservations WHERE booking_id = ? AND status = 'pending_email'",
            (booking_id,),
        ).fetchone()
    if not row:
        return "skip"

    platform = row["platform"]
    checkin = _to_date(row["checkin"])
    checkout = _to_date(row["checkout"])
    if not (checkin and checkout):
        logger.error("[Flow] finalize: 날짜 파싱 실패 %s", booking_id)
        return "skip"

    config = load_config()
    base_guests = config.get("base_guests", 2)

    name = row["guest_name"]
    info = _lookup_email_info(platform, row["checkin"], checkin)

    estimated = False
    if info is not None:
        guests = info.get("guests") or base_guests
        if platform == "airbnb" and info.get("guest_name"):
            name = info["guest_name"]
    elif force_base:
        guests = base_guests
        estimated = True
    else:
        return "pending"

    if not name:
        name = "(예약됨)"

    reservation = {
        "platform": platform,
        "booking_id": booking_id,
        "guest_name": name,
        "guests": guests,
        "checkin": checkin,
        "checkout": checkout,
    }

    # 캘린더 생성 (확정 직전). 경합에서 패배하면 아래에서 롤백.
    cal_ids = cal_mod.create_events(reservation)

    # pending_email → confirmed 원자적 전환. 먼저 전환한 쪽만 rowcount=1.
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE reservations "
            "SET status = 'confirmed', guest_name = ?, guests = ?, "
            "    google_event_id_a = ?, google_event_id_b = ? "
            "WHERE booking_id = ? AND status = 'pending_email'",
            (name, guests, cal_ids.get("google_a"), cal_ids.get("google_b"), booking_id),
        )
        conn.commit()
        won = cur.rowcount == 1

    if not won:
        # 다른 호출이 먼저 확정 → 방금 만든 캘린더 이벤트 롤백.
        logger.info("[Flow] 경합 패배 — 캘린더 롤백: %s", booking_id)
        try:
            cal_mod.delete_events(cal_ids.get("google_a"), cal_ids.get("google_b"))
        except Exception:
            logger.exception("[Flow] 롤백 삭제 실패: %s", booking_id)
        return "skip"

    # 네이버 예약 → 에어비앤비 날짜 차단(게시). 실제 반영 확인은 confirm_airbnb_blocks.
    if platform == "naver":
        try:
            blocker.block_airbnb(reservation)
        except Exception:
            logger.exception("[Flow] 에어비앤비 차단 게시 실패: %s", booking_id)

    # Discord 예약 알림 (네이버는 차단줄 없음 / 에어비앤비는 네이버 수동차단 경고 포함).
    notifier.send_notification(reservation, "created")

    if estimated:
        notifier._send_message(
            f"⚠️ {name}님 ({checkin}~{checkout}) 인원수 메일 {PENDING_TTL_MINUTES}분 미확인 "
            f"— 기본 {base_guests}인으로 확정. 실제 인원 확인 시 캘린더 수정 필요.",
            dedup_key=f"guests_estimated:{booking_id}",
        )

    logger.info(
        "[Flow] 확정: %s/%s — %s / %s인%s",
        platform, booking_id, name, guests, " (추정)" if estimated else "",
    )
    return "confirmed"


# =============================================================
# 재시도 (워처 fast / 파이프라인 full)
# =============================================================

def retry_pending(max_age_minutes: Optional[float] = None) -> int:
    """pending_email 예약 재시도.

    max_age_minutes: 지정 시 그보다 오래된 행은 건너뜀(워처의 0~5분 창 한정용).
                     None 이면 전 구간 처리하며 PENDING_TTL_MINUTES 초과분은
                     기본 인원으로 강제 확정.
    반환: 확정 처리된 건수.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT booking_id, created_at FROM reservations WHERE status = 'pending_email'"
        ).fetchall()

    confirmed = 0
    for r in rows:
        age = _age_minutes(r["created_at"])
        if max_age_minutes is not None and (age is None or age > max_age_minutes):
            continue
        force_base = max_age_minutes is None and age is not None and age >= PENDING_TTL_MINUTES
        try:
            if finalize_if_ready(r["booking_id"], force_base=force_base) == "confirmed":
                confirmed += 1
        except Exception:
            logger.exception("[Flow] retry_pending finalize 예외: %s", r["booking_id"])
    return confirmed


# =============================================================
# 에어비앤비 차단 '실제 반영' 확인 → 차단 완료 알림
# =============================================================

def _airbnb_blocked_ranges() -> Optional[list[tuple[date, date]]]:
    """에어비앤비 export iCal 에서 'Not available' 차단 구간 목록을 수집.

    네트워크/설정 문제로 못 읽으면 None (이번 사이클 판단 보류).
    """
    from modules.env_loader import load_env
    from modules import detector

    url = load_env().get("AIRBNB_ICAL_URL", "")
    if not url:
        return None
    raw = detector._download_airbnb_ical(url)
    if not raw:
        return None
    try:
        from icalendar import Calendar
        cal = Calendar.from_ical(raw)
    except Exception:
        logger.exception("[Flow] 에어비앤비 iCal 파싱 실패")
        return None

    ranges: list[tuple[date, date]] = []
    for comp in cal.walk("VEVENT"):
        summary = str(comp.get("SUMMARY", ""))
        if not any(kw in summary for kw in detector._AIRBNB_BLOCKED_KEYWORDS):
            continue
        ds = comp.get("DTSTART")
        de = comp.get("DTEND")
        ci = detector._to_date(ds.dt if ds else None)
        co = detector._to_date(de.dt if de else None)
        if ci and co:
            ranges.append((ci, co))
    return ranges


def _range_covered(checkin: date, checkout: date, ranges: list[tuple[date, date]]) -> bool:
    """차단 구간 중 [checkin, checkout) 를 덮는 것이 있는지."""
    return any(rs <= checkin and checkout <= re for rs, re in ranges)


def confirm_airbnb_blocks() -> int:
    """확정된 네이버 예약 중, 에어비앤비가 실제로 차단을 반영(iCal 'Not available')한
    건에 '🚫 차단 완료' 알림을 1회 발송. 반환: 발송 건수.
    """
    today = date.today()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT booking_id, guest_name, guests, checkin, checkout FROM reservations "
            "WHERE platform = 'naver' AND status = 'confirmed' AND checkout >= ?",
            (today.isoformat(),),
        ).fetchall()
    if not rows:
        return 0

    # 아직 차단완료 알림을 안 보낸 건만 (notify_state 영구 dedup 기준).
    pending = [
        r for r in rows
        if notifier._should_send(f"airbnb_block_confirmed:{r['booking_id']}", None)
    ]
    if not pending:
        return 0

    ranges = _airbnb_blocked_ranges()
    if not ranges:
        return 0

    sent = 0
    for r in pending:
        ci = _to_date(r["checkin"])
        co = _to_date(r["checkout"])
        if ci and co and _range_covered(ci, co, ranges):
            notifier.send_airbnb_block_confirmed({
                "platform": "naver",
                "booking_id": r["booking_id"],
                "guest_name": r["guest_name"],
                "guests": r["guests"],
                "checkin": ci,
                "checkout": co,
            })
            sent += 1
    return sent

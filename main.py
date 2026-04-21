"""
숙박업 예약 자동화 진입점.

매 시간 cron(Mac/Linux) 또는 Task Scheduler(Windows)에 의해 실행되며,
다음 파이프라인을 순차적으로 수행한다.

    1) config.json / .env 로드
    2) SQLite DB 초기화 (reservations 테이블 없으면 생성)
    3) detector.detect_new_reservations() — 네이버/에어비앤비 감지
    4) 각 예약에 대해:
        - 신규:  캘린더 등록 → DB INSERT → 반대 플랫폼 차단 → 카카오 알림
        - 취소:  캘린더 삭제 → DB UPDATE → 반대 플랫폼 해제 → 카카오 알림
    5) 처리 건수 요약 로그

개별 예약 처리 중 예외가 발생해도 나머지는 계속 처리되며,
logs/YYYY-MM-DD.log 파일에 스택트레이스와 함께 기록된다.

실행 옵션
    python main.py             # 파이프라인 실행
    python main.py --check     # 설정 점검만 수행
    python main.py --install   # 자동 실행 등록 명령 출력
"""

import argparse
import gc
import glob
import logging
import os
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Windows 콘솔(cp949)에서 한글/기호 깨짐 방지 — 모듈 import보다 먼저 실행
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# =============================================================
# 경로 / 로거 초기화
# =============================================================

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"


def _setup_logger() -> None:
    """logs/YYYY-MM-DD.log 파일과 콘솔에 동시 기록하는 루트 로거 구성.

    형식: [YYYY-MM-DD HH:MM:SS] [LEVEL] 메시지
    에러는 logger.exception 사용 시 자동으로 스택트레이스 포함.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().isoformat()}.log"

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 재실행/테스트 시 중복 핸들러 방지
    root.handlers.clear()

    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)


_setup_logger()
logger = logging.getLogger(__name__)


# =============================================================
# 모듈 import (로거 초기화 이후)
# =============================================================

from modules import blocker, calendar, detector, notifier  # noqa: E402
from modules.config_loader import load_config  # noqa: E402
from modules.db import DB_PATH, get_connection, init_db  # noqa: E402
from modules.env_loader import ENV_KEYS, load_env  # noqa: E402



# =============================================================
# 개별 예약 처리
# =============================================================

def _handle_new(reservation: dict) -> bool:
    """신규 예약 1건 처리 (캘린더 등록 → DB INSERT → 반대 플랫폼 차단 → 알림)."""
    booking_id = reservation["booking_id"]
    platform = reservation["platform"]

    checkin = reservation.get("checkin")
    checkout = reservation.get("checkout")
    if not checkin or not checkout:
        logger.warning("[신규] checkin/checkout 누락 — skip: %s/%s", platform, booking_id)
        return False

    checkin_str = checkin.isoformat() if hasattr(checkin, 'isoformat') else checkin

    # 에어비앤비: 웹훅으로 임시 저장된 건이 있으면 정식 처리로 업그레이드
    existing_pending = None
    if platform == "airbnb":
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT booking_id FROM reservations "
                "WHERE platform = 'airbnb' AND checkin = ? AND status = 'confirmed' "
                "  AND google_event_id_a IS NULL AND guest_name IN ('확인필요', '?', '')",
                (checkin_str,),
            )
            row = cur.fetchone()
            if row:
                existing_pending = row["booking_id"]

    # a) 구글 캘린더 A/B 일정 생성
    cal_ids = calendar.create_events(reservation)

    if existing_pending:
        # 임시 저장 건 업그레이드
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE reservations SET
                    guest_name = ?, guests = ?, checkout = ?,
                    google_event_id_a = ?, google_event_id_b = ?
                WHERE booking_id = ?
                """,
                (
                    reservation.get("guest_name"),
                    reservation.get("guests"),
                    reservation["checkout"].isoformat(),
                    cal_ids.get("google_a"),
                    cal_ids.get("google_b"),
                    existing_pending,
                ),
            )
            conn.commit()
        logger.info("[신규] 임시 저장 업그레이드: %s → %s", existing_pending, reservation.get("guest_name"))
    else:
        # b) DB INSERT (동일 booking_id 중복 삽입 방지 위해 OR IGNORE)
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reservations
                    (platform, booking_id, guest_name, guests,
                     checkin, checkout, status,
                     google_event_id_a, google_event_id_b)
                VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)
                """,
                (
                    platform,
                    booking_id,
                    reservation.get("guest_name"),
                    reservation.get("guests"),
                    checkin_str,
                    reservation["checkout"].isoformat(),
                    cal_ids.get("google_a"),
                    cal_ids.get("google_b"),
                ),
            )
            conn.commit()

    # c) 반대 플랫폼 차단
    if platform == "naver":
        blocker.block_airbnb(reservation)

    # d) 카카오 알림
    notifier.send_notification(reservation, "created")

    logger.info("[신규] %s/%s 처리 완료", platform, booking_id)
    return True


def _handle_cancel(reservation: dict) -> bool:
    """취소 예약 1건 처리 (캘린더 삭제 → DB UPDATE → 반대 플랫폼 해제 → 알림)."""
    booking_id = reservation["booking_id"]
    platform = reservation["platform"]

    # a) DB에서 저장된 이벤트 ID 조회
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT google_event_id_a, google_event_id_b "
            "FROM reservations WHERE booking_id = ? AND platform = ?",
            (booking_id, platform),
        )
        row = cur.fetchone()

    if not row:
        logger.warning("[취소] DB에 원본 예약 없음: %s/%s", platform, booking_id)
        return False

    # b) 구글 캘린더 삭제
    calendar.delete_events(
        google_event_id_a=row["google_event_id_a"],
        google_event_id_b=row["google_event_id_b"],
    )

    # c) DB status → 'cancelled'
    with get_connection() as conn:
        conn.execute(
            "UPDATE reservations SET status = 'cancelled' "
            "WHERE booking_id = ? AND platform = ?",
            (booking_id, platform),
        )
        conn.commit()

    # d) 반대 플랫폼 해제
    if platform == "naver":
        blocker.unblock_airbnb(booking_id)
    elif platform == "airbnb":
        blocker.unblock_naver(reservation)

    # e) 카카오 알림
    notifier.send_notification(reservation, "deleted")
    logger.info("[취소] %s/%s 처리 완료", platform, booking_id)
    return True


# =============================================================
# 에어비앤비 예약 변경 처리
# =============================================================

def _handle_airbnb_modifications() -> int:
    """에어비앤비 예약 변경 2단계 처리."""
    modifications = detector.detect_airbnb_modifications()
    if not modifications:
        return 0

    config = load_config()
    owner_cal = config.get("naver_owner_calendar", "")
    staff_cal = config.get("naver_staff_calendar", "")
    processed = 0

    for mod in modifications:
        guest_name = mod.get("guest_name")
        if not guest_name:
            continue

        # DB에서 예약 조회 (이름 + 기존 체크인 날짜로 정확 매칭)
        old_checkin_hint = mod.get("old_checkin")
        with get_connection() as conn:
            if old_checkin_hint:
                cur = conn.execute(
                    "SELECT booking_id, checkin, checkout, guests, "
                    "       google_event_id_a, google_event_id_b, "
                    "       pending_checkin, pending_checkout "
                    "FROM reservations "
                    "WHERE platform = 'airbnb' AND status = 'confirmed' "
                    "  AND guest_name LIKE ? AND checkin = ?",
                    (f"%{guest_name}%", old_checkin_hint.isoformat()),
                )
            else:
                cur = conn.execute(
                    "SELECT booking_id, checkin, checkout, guests, "
                    "       google_event_id_a, google_event_id_b, "
                    "       pending_checkin, pending_checkout "
                    "FROM reservations "
                    "WHERE platform = 'airbnb' AND status = 'confirmed' "
                    "  AND guest_name LIKE ?",
                    (f"%{guest_name}%",),
                )
            row = cur.fetchone()

        if not row:
            logger.warning("[변경] DB에서 %s 예약 못 찾음", guest_name)
            continue

        bid = row["booking_id"]

        # --- 1단계: 변경 요청 → pending에 날짜 저장 ---
        if mod["type"] == "request":
            new_ci = mod.get("new_checkin")
            new_co = mod.get("new_checkout")
            if not new_ci:
                continue

            with get_connection() as conn:
                conn.execute(
                    "UPDATE reservations SET pending_checkin = ?, pending_checkout = ? "
                    "WHERE booking_id = ?",
                    (new_ci.isoformat(), new_co.isoformat() if new_co else None, bid),
                )
                conn.commit()

            logger.info("[변경 요청] %s: pending %s~%s 저장", guest_name, new_ci, new_co)
            processed += 1
            continue

        # --- 2단계: 변경 확정 → pending에서 실제 반영 ---
        if mod["type"] == "confirmed":
            pending_ci = row["pending_checkin"]
            pending_co = row["pending_checkout"]

            if not pending_ci:
                logger.warning("[변경 확정] %s: pending 날짜 없음 — skip", guest_name)
                continue

            from modules.detector import _to_date
            new_checkin = _to_date(pending_ci)
            new_checkout = _to_date(pending_co) if pending_co else new_checkin
            if not new_checkin:
                continue

            old_checkin = row["checkin"]
            old_checkout = row["checkout"]
            nights = (new_checkout - new_checkin).days

            # 캘린더 A: 전체 기간
            calendar.update_event_dates(
                row["google_event_id_a"], owner_cal, new_checkin, new_checkout
            )

            # 캘린더 B: 체크인 하루 + 연박 제목
            staff_name = config.get("staff_name", "")
            guests_str = str(row["guests"] or 2)
            if nights > 1:
                summary_b = f"{staff_name} / 성인 {guests_str}명 (연박{nights}배)"
            else:
                summary_b = f"{staff_name} / 성인 {guests_str}명"
            calendar.update_event_dates(
                row["google_event_id_b"], staff_cal,
                new_checkin, new_checkin + timedelta(days=1), summary_b
            )

            # DB 업데이트 + pending 초기화
            with get_connection() as conn:
                conn.execute(
                    "UPDATE reservations SET checkin = ?, checkout = ?, "
                    "  pending_checkin = NULL, pending_checkout = NULL "
                    "WHERE booking_id = ?",
                    (new_checkin.isoformat(), new_checkout.isoformat(), bid),
                )
                conn.commit()

            # 카카오 알림
            notifier._send_kakao_message(
                f"[예약 변경 확정] {guest_name}님\n"
                f"📅 기존: {old_checkin}~{old_checkout}\n"
                f"📅 변경: {new_checkin}~{new_checkout}\n"
                "✅ 캘린더 업데이트 완료\n"
                "⚠️ 네이버 플레이스 수동 차단 해제 필요"
            )

            logger.info("[변경 확정] %s: %s~%s → %s~%s",
                        guest_name, old_checkin, old_checkout, new_checkin, new_checkout)
            processed += 1

    return processed


# =============================================================
# 48시간 pending 자동 초기화
# =============================================================

def _cleanup_stale_pending() -> int:
    """48시간 이상 된 pending 날짜를 초기화 (변경 요청 → 거절된 경우)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, guest_name, pending_checkin FROM reservations "
            "WHERE pending_checkin IS NOT NULL "
            "  AND created_at <= datetime('now', '-48 hours')"
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return 0

    with get_connection() as conn:
        conn.execute(
            "UPDATE reservations SET pending_checkin = NULL, pending_checkout = NULL "
            "WHERE pending_checkin IS NOT NULL "
            "  AND created_at <= datetime('now', '-48 hours')"
        )
        conn.commit()

    for r in rows:
        logger.info("[Pending 초기화] %s: pending %s 만료", r["guest_name"], r["pending_checkin"])

    return len(rows)


# =============================================================
# iCal 날짜 변경 감지
# =============================================================

def _detect_ical_date_changes() -> int:
    """iCal의 날짜와 DB 날짜를 비교하여 변경 감지 및 업데이트."""
    env = load_env()
    url = env.get("AIRBNB_ICAL_URL", "")
    if not url:
        return 0

    from modules.detector import _download_airbnb_ical, _parse_airbnb_ical, _to_date

    ical_bytes = _download_airbnb_ical(url)
    if not ical_bytes:
        return 0

    try:
        ical_events = _parse_airbnb_ical(ical_bytes)
    except Exception:
        return 0
    finally:
        del ical_bytes

    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, guest_name, checkin, checkout, guests, "
            "       google_event_id_a, google_event_id_b "
            "FROM reservations "
            "WHERE platform = 'airbnb' AND status = 'confirmed'"
        )
        db_rows = {r["booking_id"]: dict(r) for r in cur.fetchall()}

    if not db_rows:
        return 0

    config = load_config()
    owner_cal = config.get("naver_owner_calendar", "")
    staff_cal = config.get("naver_staff_calendar", "")
    updated = 0

    for ev in ical_events:
        bid = ev["booking_id"]
        if bid not in db_rows:
            continue

        row = db_rows[bid]
        ical_ci = ev.get("checkin")
        ical_co = ev.get("checkout")
        db_ci = _to_date(row["checkin"])
        db_co = _to_date(row["checkout"])

        if not ical_ci or not ical_co or not db_ci or not db_co:
            continue
        if ical_ci == db_ci and ical_co == db_co:
            continue

        # 날짜 변경 감지
        nights = (ical_co - ical_ci).days
        staff_name = config.get("staff_name", "")
        guests_str = str(row["guests"] or 2)

        calendar.update_event_dates(
            row["google_event_id_a"], owner_cal, ical_ci, ical_co
        )

        if nights > 1:
            summary_b = f"{staff_name} / 성인 {guests_str}명 (연박{nights}배)"
        else:
            summary_b = f"{staff_name} / 성인 {guests_str}명"
        calendar.update_event_dates(
            row["google_event_id_b"], staff_cal,
            ical_ci, ical_ci + timedelta(days=1), summary_b
        )

        with get_connection() as conn:
            conn.execute(
                "UPDATE reservations SET checkin = ?, checkout = ?, "
                "  pending_checkin = NULL, pending_checkout = NULL "
                "WHERE booking_id = ?",
                (ical_ci.isoformat(), ical_co.isoformat(), bid),
            )
            conn.commit()

        guest_name = row["guest_name"] or "게스트"
        notifier._send_kakao_message(
            f"[예약 변경 확정] {guest_name}님\n"
            f"📅 기존: {db_ci}~{db_co}\n"
            f"📅 변경: {ical_ci}~{ical_co}\n"
            "✅ 캘린더 업데이트 완료\n"
            "⚠️ 네이버 플레이스 수동 차단 해제 필요"
        )
        logger.info("[iCal 변경] %s: %s~%s → %s~%s", guest_name, db_ci, db_co, ical_ci, ical_co)
        updated += 1

    return updated


# =============================================================
# 24시간 미해결 알림
# =============================================================

def _alert_stale_reservations() -> None:
    """24시간 이상 이름/인원 미확인 예약이 있으면 카카오 알림 (일회성)."""
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, guest_name, checkin FROM reservations "
            "WHERE status = 'confirmed' "
            "  AND (guest_name IN ('?', '', '확인필요', '(예약됨)') OR google_event_id_a IS NULL) "
            "  AND created_at <= datetime('now', '-24 hours') "
            "  AND (unprocessed_alert_sent IS NULL OR unprocessed_alert_sent = 0)"
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return

    names = ", ".join(f"{r['checkin']} {r['guest_name']}" for r in rows[:5])
    notifier._send_kakao_message(
        f"[미처리 예약 {len(rows)}건] 24시간 경과, 수동 확인 필요: {names}"
    )

    with get_connection() as conn:
        for r in rows:
            conn.execute(
                "UPDATE reservations SET unprocessed_alert_sent = 1 WHERE booking_id = ?",
                (r["booking_id"],),
            )
        conn.commit()

    logger.warning("[알림] 24시간 미처리 예약 %d건 (알림 완료): %s", len(rows), names)


# =============================================================
# cancelled 예약의 잔여 캘린더 일정 정리
# =============================================================

def _cleanup_cancelled_events() -> int:
    """DB에서 cancelled인데 구글 캘린더 ID가 남아있는 건의 일정을 삭제."""
    cleaned = 0

    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, google_event_id_a, google_event_id_b "
            "FROM reservations "
            "WHERE status = 'cancelled' "
            "  AND (google_event_id_a IS NOT NULL OR google_event_id_b IS NOT NULL)"
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return 0

    for row in rows:
        calendar.delete_events(
            google_event_id_a=row["google_event_id_a"],
            google_event_id_b=row["google_event_id_b"],
        )

        with get_connection() as conn:
            conn.execute(
                "UPDATE reservations SET google_event_id_a = NULL, google_event_id_b = NULL "
                "WHERE booking_id = ?",
                (row["booking_id"],),
            )
            conn.commit()

        logger.info("[Cleanup] 잔여 캘린더 삭제: %s", row["booking_id"])
        cleaned += 1

    return cleaned


# =============================================================
# 체크인 당일 3행시 발송 (오전 9시~10시)
# =============================================================

def _send_checkin_day_samhaengsi() -> int:
    """내일 체크인인 예약에 3행시 전송. 오전 8~9시 사이에만 실행."""
    now = datetime.now()
    if not (8 <= now.hour < 9):
        return 0

    from datetime import timedelta
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()
    sent = 0

    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, guest_name FROM reservations "
            "WHERE checkin = ? AND status = 'confirmed' "
            "  AND (samhaengsi_sent IS NULL OR samhaengsi_sent = 0)",
            (tomorrow_str,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    for row in rows:
        guest_name = row.get("guest_name", "")
        if not guest_name or guest_name == "(예약됨)":
            continue

        notifier.send_samhaengsi(guest_name)

        with get_connection() as conn:
            conn.execute(
                "UPDATE reservations SET samhaengsi_sent = 1 WHERE booking_id = ?",
                (row["booking_id"],),
            )
            conn.commit()

        logger.info("[3행시] 체크인 당일 발송 완료: %s", guest_name)
        sent += 1

    return sent


# =============================================================
# 파이프라인
# =============================================================

def _update_reservations_from_gmail() -> int:
    """웹훅으로 생성된 예약의 인원수/이름을 Gmail에서 실제 정보로 업데이트."""
    config = load_config()
    base_guests = config.get("base_guests", 2)
    owner_cal = config.get("naver_owner_calendar", "")
    staff_cal = config.get("naver_staff_calendar", "")
    updated = 0

    # 업데이트 필요한 예약: 인원수가 기본값이거나 이름이 미확인
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, platform, guest_name, guests, checkin, checkout, "
            "       google_event_id_a, google_event_id_b "
            "FROM reservations "
            "WHERE status = 'confirmed' "
            "  AND (guests = ? OR guest_name IN ('?', '', '확인필요', 'Reserved', '(예약됨)'))",
            (base_guests,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return 0

    # 네이버 메일 정보 수집 (네이버 예약이 있을 때만)
    naver_by_checkin: dict[str, dict] = {}
    naver_rows = [r for r in rows if r["platform"] == "naver"]
    if naver_rows:
        try:
            for em in detector.detect_naver():
                ci = em.get("checkin")
                if ci:
                    naver_by_checkin[ci.isoformat() if hasattr(ci, 'isoformat') else ci] = em
        except Exception as e:
            logger.warning("[업데이트] 네이버 메일 조회 실패: %s", e)

    # 에어비앤비 메일 정보 수집 (에어비앤비 예약이 있을 때만)
    airbnb_by_checkin: dict[str, dict] = {}
    airbnb_rows = [r for r in rows if r["platform"] == "airbnb"]
    if airbnb_rows:
        try:
            from modules.detector import _extract_airbnb_info_from_gmail, _to_date
            for row in airbnb_rows:
                checkin_date = _to_date(row["checkin"])
                if checkin_date and row["checkin"] not in airbnb_by_checkin:
                    info = _extract_airbnb_info_from_gmail(checkin_date)
                    if info:
                        airbnb_by_checkin[row["checkin"]] = info
        except Exception as e:
            logger.warning("[업데이트] 에어비앤비 메일 조회 실패: %s", e)

    for row in rows:
        platform = row["platform"]
        checkin = row["checkin"]
        old_name = row["guest_name"] or ""
        prefix = config.get("platform_prefix", {}).get(platform, "")

        email_data = None
        if platform == "naver":
            email_data = naver_by_checkin.get(checkin)
        elif platform == "airbnb":
            email_data = airbnb_by_checkin.get(checkin)

        if not email_data:
            continue

        new_guests = email_data.get("guests")
        new_name = email_data.get("guest_name")
        needs_update = False
        updates: dict = {}

        if new_guests and new_guests != row["guests"]:
            updates["guests"] = new_guests
            needs_update = True

        _placeholder_names = {"?", "", "확인필요", "Reserved", "(예약됨)"}
        if new_name and old_name in _placeholder_names and new_name not in _placeholder_names:
            updates["guest_name"] = new_name
            needs_update = True

        if not needs_update:
            continue

        final_name = updates.get("guest_name", old_name)
        final_guests = updates.get("guests", row["guests"])

        from modules.detector import _to_date
        ci = _to_date(row["checkin"])
        co = _to_date(row["checkout"])
        nights = (co - ci).days if ci and co else 1
        staff_name = config.get("staff_name", "")

        summary_a = f"{prefix}. {final_name}. {final_guests}인"
        if nights > 1:
            summary_b = f"{staff_name} / 성인 {final_guests}명 (연박{nights}배)"
        else:
            summary_b = f"{staff_name} / 성인 {final_guests}명"

        calendar.update_event_summary(row["google_event_id_a"], owner_cal, summary_a)
        calendar.update_event_summary(row["google_event_id_b"], staff_cal, summary_b)

        set_clauses = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [row["booking_id"]]
        with get_connection() as conn:
            conn.execute(
                f"UPDATE reservations SET {set_clauses} WHERE booking_id = ?",
                values,
            )
            conn.commit()

        logger.info("[업데이트] %s/%s: %s", platform, final_name, updates)
        updated += 1

    return updated


def run_pipeline() -> int:
    """전체 파이프라인 1회 실행. 반환: 0=모두 성공, 1=개별 실패 존재."""
    logger.info("=" * 60)
    logger.info("예약 자동화 파이프라인 시작")
    logger.info("=" * 60)

    try:
        load_config()
        load_env()
        init_db()
    except Exception:
        logger.exception("초기화 단계 실패 — 중단")
        return 1

    reservations = detector.detect_new_reservations()
    logger.info("감지된 예약 이벤트: %d건", len(reservations))

    stat_new, stat_cancel, stat_fail = 0, 0, 0

    for r in reservations:
        action = r.get("action")
        booking_id = r.get("booking_id")
        try:
            if action == "new":
                if _handle_new(r):
                    stat_new += 1
                else:
                    stat_fail += 1
            elif action == "cancel":
                if _handle_cancel(r):
                    stat_cancel += 1
                else:
                    stat_fail += 1
            else:
                logger.warning("알 수 없는 action='%s' (booking_id=%s)", action, booking_id)
                stat_fail += 1
        except Exception:
            # 개별 실패가 전체를 중단시키지 않도록 포착 후 계속
            stat_fail += 1
            logger.exception("예약 처리 중 예외 (booking_id=%s)", booking_id)

    # 예약 정보 업데이트 (Gmail 메일 기반 — 인원수/이름)
    try:
        stat_update = _update_reservations_from_gmail()
    except Exception:
        stat_update = 0
        logger.exception("예약 정보 업데이트 중 예외")

    # 에어비앤비 예약 변경 처리
    try:
        stat_modify = _handle_airbnb_modifications()
    except Exception:
        stat_modify = 0
        logger.exception("예약 변경 처리 중 예외")

    # 체크인 D-1 3행시 발송 (8~9시)
    try:
        stat_samhaengsi = _send_checkin_day_samhaengsi()
    except Exception:
        stat_samhaengsi = 0
        logger.exception("3행시 발송 중 예외")

    # cancelled 예약 잔여 캘린더 정리
    try:
        stat_cleanup = _cleanup_cancelled_events()
    except Exception:
        stat_cleanup = 0
        logger.exception("캘린더 정리 중 예외")

    # iCal 날짜 변경 감지 (호스트 직접 변경 등)
    try:
        stat_ical_change = _detect_ical_date_changes()
    except Exception:
        stat_ical_change = 0
        logger.exception("iCal 날짜 변경 감지 중 예외")

    # 48시간 pending 자동 초기화
    try:
        _cleanup_stale_pending()
    except Exception:
        logger.exception("pending 초기화 중 예외")

    # 24시간 미해결 예약 알림
    try:
        _alert_stale_reservations()
    except Exception:
        logger.exception("미해결 예약 알림 중 예외")

    # blocked.ics GitHub 동기화 확인
    try:
        if blocker.sync_github_if_needed():
            logger.info("[GitHub] blocked.ics 동기화 완료")
    except Exception:
        logger.exception("GitHub 동기화 중 예외")

    # 30일 이상 오래된 로그 삭제
    _cleanup_old_logs()

    # 메모리 해제
    gc.collect()

    logger.info(
        "처리 요약 — 신규 %d건 / 취소 %d건 / 변경 %d건 / 업데이트 %d건 / 3행시 %d건 / 정리 %d건 / 실패 %d건",
        stat_new, stat_cancel, stat_modify, stat_update, stat_samhaengsi, stat_cleanup, stat_fail,
    )
    return 0 if stat_fail == 0 else 1


def _cleanup_old_logs(days: int = 30) -> None:
    """30일 이상 된 로그 파일 삭제."""
    cutoff = date.today() - timedelta(days=days)
    for log_file in LOG_DIR.glob("*.log*"):
        try:
            if log_file.stat().st_mtime < cutoff.toordinal() * 86400:
                log_file.unlink()
        except OSError:
            pass


# =============================================================
# 설정 점검 (--check)
# =============================================================

def setup_check() -> int:
    """환경 전반 점검 후 문제 항목을 안내. 반환: 0=정상, 1=문제 있음."""
    issues: list[str] = []

    print("=" * 60)
    print(" 예약 자동화 설정 점검")
    print("=" * 60)

    # 1) .env 필수 항목
    print("\n[1/4] .env 필수 환경변수")
    env = load_env()
    for key in ENV_KEYS:
        value = env.get(key, "")
        if value:
            masked = value[:4] + "..." if len(value) > 4 else "..."
            print(f"   [OK]   {key} = {masked}")
        else:
            print(f"   [FAIL] {key} 미설정")
            issues.append(f".env의 {key}를 채워주세요.")

    # 2) DB 접근
    print("\n[2/4] SQLite DB 접근")
    try:
        init_db()
        with get_connection() as conn:
            conn.execute("SELECT 1 FROM reservations LIMIT 1").fetchall()
        print(f"   [OK]   {DB_PATH}")
    except Exception as e:
        print(f"   [FAIL] DB 접근 실패: {e}")
        issues.append("DB 파일 권한 또는 스키마를 확인해주세요.")

    # 3) 구글 캘린더 API 토큰 유효성
    print("\n[3/4] 구글 캘린더 API 토큰")
    try:
        gsvc = calendar._get_google_calendar_service()  # noqa: SLF001
        cals = gsvc.calendarList().list(maxResults=1).execute()
        print("   [OK]   token_calendar.json 유효")
    except FileNotFoundError:
        print("   [FAIL] token_calendar.json 없음")
        issues.append("python generate_calendar_token.py 실행 필요.")
    except Exception as e:
        print(f"   [FAIL] 예외: {e}")
        issues.append("구글 캘린더 토큰 점검 중 예외 발생.")

    # 4) 카카오 메모 API 토큰 유효성
    print("\n[4/4] 카카오 메모 API 토큰")
    try:
        token = notifier._refresh_kakao_access_token()  # noqa: SLF001
        if token:
            print("   [OK]   refresh_token → access_token 갱신 성공")
        else:
            print("   [FAIL] 토큰 갱신 실패 (로그 확인)")
            issues.append("KAKAO_REST_API_KEY / KAKAO_REFRESH_TOKEN 확인.")
    except Exception as e:
        print(f"   [FAIL] 예외: {e}")
        issues.append("카카오 토큰 점검 중 예외 발생.")

    # 결과 요약
    print("\n" + "=" * 60)
    if not issues:
        print(" 점검 완료: 모든 항목 정상")
        print("=" * 60)
        return 0

    print(f" 점검 완료: {len(issues)}개 문제 발견")
    print("=" * 60)
    for i, msg in enumerate(issues, 1):
        print(f"  {i}. {msg}")
    return 1


# =============================================================
# 자동 실행 등록 명령 출력 (--install)
# =============================================================

def _cron_expr(minutes: int) -> str:
    """폴링 주기(분) → cron 표현식."""
    if minutes < 60:
        return f"*/{minutes} * * * *"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    return f"*/{minutes} * * * *"


def print_install_commands() -> int:
    """OS별 자동 실행 등록 방법을 실제 절대경로로 채워서 출력."""
    config = load_config()
    interval = int(config.get("polling_interval_minutes", 60))

    py_path = sys.executable
    main_path = str(PROJECT_ROOT / "main.py")
    cron_log = str(LOG_DIR / "cron.log")
    cron_expr = _cron_expr(interval)

    print("=" * 60)
    print(f" 자동 실행 등록 안내 (폴링 주기: {interval}분)")
    print("=" * 60)

    print("\n[Mac / Linux] crontab -e 실행 후 아래 한 줄 추가:\n")
    print(f"  {cron_expr} {py_path} {main_path} >> {cron_log} 2>&1")

    print("\n[Windows] PowerShell(관리자)에서 아래 명령 실행:\n")
    ps = (
        f"$action  = New-ScheduledTaskAction -Execute '{py_path}' "
        f"-Argument '\"{main_path}\"'\n"
        f"$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {interval})\n"
        f"Register-ScheduledTask -TaskName 'ReservationBot' "
        f"-Action $action -Trigger $trigger "
        f"-Description '숙박업 예약 자동화' -Force"
    )
    print(ps)
    print()
    return 0


# =============================================================
# 엔트리포인트
# =============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="숙박업 예약 자동화")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="설정 점검만 수행")
    group.add_argument(
        "--install", action="store_true",
        help="OS별 자동 실행 등록 명령 출력",
    )
    args = parser.parse_args()

    if args.check:
        return setup_check()
    if args.install:
        return print_install_commands()
    return run_pipeline()


if __name__ == "__main__":
    sys.exit(main())

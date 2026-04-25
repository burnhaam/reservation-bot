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
import json
import logging
import os
import re
import sys
import time
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
    """logs/YYYY-MM-DD.log(전체) + logs/error_YYYY-MM.log(에러만) + 콘솔에 동시 기록.

    형식: [YYYY-MM-DD HH:MM:SS] [LEVEL] 메시지
    에러는 logger.exception 사용 시 자동으로 스택트레이스 포함.
    에러 전용 파일은 월 단위로 쌓여 장기 트렌드 분석에 유리.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{date.today().isoformat()}.log"
    error_log_path = LOG_DIR / f"error_{date.today().strftime('%Y-%m')}.log"

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 재실행/테스트 시 중복 핸들러 방지
    root.handlers.clear()

    # 1) 전체 로그 (일별 파일, 10MB × 3 로테이션)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 2) 에러 전용 로그 (월별 파일, 5MB × 3 로테이션) — ERROR 이상만 기록
    error_handler = RotatingFileHandler(
        error_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    # 3) 콘솔
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


# 재고 자동주문 상수
_STOCK_DUPLICATE_WINDOW_DAYS_DEFAULT = 3
_STOCK_MAX_DAILY_ORDERS_DEFAULT = 5


# =============================================================
# 최상위 글로벌 예외 핸들러
# =============================================================

def _global_exception_handler(exc_type, exc_value, exc_traceback):
    """sys.excepthook. 예기치 못한 예외를 critical 로그 + 카카오 긴급 알림으로 보고.

    KeyboardInterrupt(사용자 Ctrl+C)는 기본 처리로 위임한다.
    알림 자체가 실패해도 재귀 예외로 번지지 않도록 swallow.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical(
        "Unhandled exception",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    try:
        from modules.notifier import send_stock_alert
        send_stock_alert(
            f"[긴급] 시스템 크래시: {exc_type.__name__}: {exc_value}",
            dedup_key=f"sys_crash:{exc_type.__name__}:{exc_value}",
            cooldown_hours=None,
        )
    except Exception:
        pass


sys.excepthook = _global_exception_handler



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

            # 카카오 알림 (영구 1회)
            notifier._send_kakao_message(
                f"[예약 변경 확정] {guest_name}님\n"
                f"📅 기존: {old_checkin}~{old_checkout}\n"
                f"📅 변경: {new_checkin}~{new_checkout}\n"
                "✅ 캘린더 업데이트 완료\n"
                "⚠️ 네이버 플레이스 수동 차단 해제 필요",
                dedup_key=f"modify:{bid}:{old_checkin}~{old_checkout}->{new_checkin}~{new_checkout}",
                cooldown_hours=None,
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
            "⚠️ 네이버 플레이스 수동 차단 해제 필요",
            dedup_key=f"modify_ical:{bid}:{db_ci}~{db_co}->{ical_ci}~{ical_co}",
            cooldown_hours=None,
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
# 체크인 D-1 3행시 발송 (오전 8~9시)
# =============================================================

# DB에 없는 수기 등록 예약도 태우기 위해 소유 캘린더도 스캔한다.
# 제목 포맷 예: "네. 최진 2인", "에. 김철수. 3인", "알. Junyeon 2명"
_SAMHAENGSI_CALENDAR_TITLE_PAT = re.compile(
    r"^\s*([네에알])\s*[.．]\s*(.+?)\s*[.．]?\s*(\d+)\s*[명인]\s*$"
)
_SAMHAENGSI_STATE_PATH = PROJECT_ROOT / "data" / "samhaengsi_sent_events.json"


def _load_samhaengsi_calendar_state() -> dict:
    try:
        with open(_SAMHAENGSI_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_samhaengsi_calendar_state(state: dict) -> None:
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    _SAMHAENGSI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_SAMHAENGSI_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(pruned, f, ensure_ascii=False, indent=2)


def _send_samhaengsi_from_calendar(target_date: date) -> int:
    """소유 캘린더에서 target_date 체크인 예약 이벤트를 찾아 3행시 발송. DB 외 건 보강용."""
    config = load_config()
    owner_cal = config.get("naver_owner_calendar", "")
    if not owner_cal:
        return 0

    events = calendar.list_events_on_date(owner_cal, target_date)
    if not events:
        return 0

    state = _load_samhaengsi_calendar_state()
    sent = 0

    for ev in events:
        event_id = ev.get("event_id", "")
        if not event_id or event_id in state:
            continue
        m = _SAMHAENGSI_CALENDAR_TITLE_PAT.match(ev.get("summary", ""))
        if not m:
            continue
        name = m.group(2).strip()
        if not name or name == "(예약됨)":
            continue

        notifier.send_samhaengsi(name)
        state[event_id] = target_date.isoformat()
        _save_samhaengsi_calendar_state(state)
        logger.info("[3행시] 체크인 당일 발송 완료 (캘린더): %s (event=%s…)",
                    name, event_id[:24])
        sent += 1

    return sent


def _send_checkin_day_samhaengsi() -> int:
    """내일 체크인 예약에 3행시 전송. 오전 8~9시에만 실행 (FORCE_SAMHAENGSI=1 시 우회)."""
    now = datetime.now()
    if not (8 <= now.hour < 9) and not os.environ.get("FORCE_SAMHAENGSI"):
        return 0

    tomorrow = date.today() + timedelta(days=1)
    tomorrow_str = tomorrow.isoformat()
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

        logger.info("[3행시] 체크인 당일 발송 완료 (DB): %s", guest_name)
        sent += 1

    try:
        sent += _send_samhaengsi_from_calendar(tomorrow)
    except Exception:
        logger.exception("[3행시/캘린더] 보조 소스 처리 중 예외")

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
    """전체 파이프라인 1회 실행. 반환: 0=모두 성공, 1=개별 실패 존재.

    어떠한 예외도 프로세스를 죽이지 않도록 전체 바디를 try/except/finally로 감싼다.
    finally 블록에서 정리 작업(_record_last_success, _cleanup_old_logs, gc 등)을
    항상 수행해 watchdog 오탐을 막는다.
    """
    logger.info("=" * 60)
    logger.info("예약 자동화 파이프라인 시작")
    logger.info("=" * 60)

    try:
        load_config()
        load_env()
        init_db()
    except Exception:
        logger.exception("초기화 단계 실패 — 중단")
        # 초기화 실패는 진짜 설정 문제이므로 success 기록 X → watchdog이 감지하도록 둠
        return 1

    stat_new = stat_cancel = stat_fail = 0
    stat_update = stat_modify = stat_samhaengsi = stat_cleanup = stat_ical_change = 0

    try:
        reservations = detector.detect_new_reservations()
        logger.info("감지된 예약 이벤트: %d건", len(reservations))

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

        # 재고 자동주문 파이프라인: 매 폴링마다 실행 (캘린더 메모 변경 즉시 반응).
        # stock_detector가 (event_id, memo_hash) 기준으로 이미 처리된 메모를 걸러내므로,
        # 새/수정된 메모가 없으면 0건 반환 후 즉시 종료한다.
        try:
            run_stock_pipeline()
        except Exception:
            logger.exception("재고 자동주문 파이프라인 중 예외")

        # 로직 ①③ — 매일 07시 1회, 3일+ 경과 미확정 건 있을 때만 실행
        try:
            _run_order_confirmation_scan_if_due()
        except Exception:
            logger.exception("주문 확인 스캔 중 예외")

    except Exception:
        # 내부 블록 어디서도 처리 못 한 예외 — 프로세스는 살리고 다음 사이클로 넘김
        logger.exception("[파이프라인] 예기치 못한 전역 예외 (흡수됨, 다음 사이클로 계속)")
        stat_fail += 1
    finally:
        # 정리 작업은 예외 발생 여부와 무관하게 항상 실행.
        # 각 단계를 개별 try로 감싸 어느 하나 실패해도 뒤 단계 계속.
        try:
            _cleanup_old_logs()
        except Exception:
            logger.exception("로그 정리 실패 (무시)")

        try:
            _record_last_success()
        except Exception:
            logger.exception("헬스체크 기록 실패 (무시)")

        try:
            _vacuum_db_if_needed()
        except Exception:
            logger.exception("DB VACUUM 실패 (무시)")

        try:
            import ctypes
            if hasattr(ctypes, "windll"):
                ctypes.windll.kernel32.SetProcessWorkingSetSize(-1, -1, -1)
        except Exception:
            pass

        try:
            gc.collect()
        except Exception:
            pass

        try:
            import psutil
            mem_mb = psutil.Process().memory_info().rss / 1024 / 1024
            logger.info("[성능] 메모리 사용량: %.1f MB", mem_mb)
        except ImportError:
            pass
        except Exception:
            logger.warning("[성능] 메모리 측정 실패", exc_info=True)

    logger.info(
        "처리 요약 — 신규 %d건 / 취소 %d건 / 변경 %d건 / 업데이트 %d건 / 3행시 %d건 / 정리 %d건 / 실패 %d건",
        stat_new, stat_cancel, stat_modify, stat_update, stat_samhaengsi, stat_cleanup, stat_fail,
    )
    return 0 if stat_fail == 0 else 1


# =============================================================
# 재고 자동주문 파이프라인 (stock)
# =============================================================

def _recent_stock_item_names(window_days: int) -> set:
    """window_days 이내 'ordered' 상태로 주문된 품목명 집합을 반환 (중복 방지용)."""
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT item_name FROM stock_orders "
                "WHERE status = 'ordered' "
                "  AND detected_at >= datetime('now', ?)",
                (f"-{int(window_days)} days",),
            )
            return {row["item_name"] for row in cur.fetchall()}
    except Exception:
        logger.exception("[Stock] 최근 주문 이력 조회 실패")
        return set()


def _today_order_count() -> int:
    """오늘(로컬) 'ordered' 상태로 기록된 주문 수를 반환."""
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) AS cnt FROM stock_orders "
                "WHERE status = 'ordered' "
                "  AND date(detected_at) = date('now', 'localtime')"
            )
            row = cur.fetchone()
            return int(row["cnt"]) if row else 0
    except Exception:
        logger.exception("[Stock] 오늘 주문 수 조회 실패")
        return 0


def _record_stock_order(row: dict) -> None:
    """stock_orders 테이블에 1건 INSERT."""
    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO stock_orders
                    (detected_at, calendar_event_id, memo_hash, item_name,
                     matched_url, matched_source, quantity, price,
                     status, skip_reason, fail_reason, ordered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("detected_at"),
                    row.get("calendar_event_id"),
                    row.get("memo_hash"),
                    row.get("item_name"),
                    row.get("matched_url"),
                    row.get("matched_source"),
                    int(row.get("quantity") or 1),
                    row.get("price"),
                    row.get("status"),
                    row.get("skip_reason"),
                    row.get("fail_reason"),
                    row.get("ordered_at"),
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("[Stock] DB INSERT 실패: %s", row.get("item_name"))


def run_stock_pipeline() -> int:
    """재고 자동주문 파이프라인 1회 실행.

    흐름 (PRD 5.2):
      1) 캘린더 재고 메모 감지
      2) Gemini API로 부족 품목 추출
      3) 중복/한도 필터링
      4) 매핑표 + 주문내역 매칭
      5) Playwright 장바구니 담기 (가격 검증 포함)
      6) 카카오 결과 알림 + DB 기록
    반환: 처리한 후보 메모 건수 (없으면 0).
    """
    try:
        config = load_config()
    except Exception:
        logger.exception("[Stock] config 로드 실패 — 중단")
        return 0

    stock_cfg = config.get("stock", {}) or {}
    if not stock_cfg.get("enabled", False):
        logger.info("[Stock] stock.enabled=false — 파이프라인 건너뜀")
        return 0

    logger.info("-" * 60)
    logger.info("재고 자동주문 파이프라인 시작")
    logger.info("-" * 60)

    # 지연 import (playwright/google-genai 미설치 환경에서 기존 파이프라인 영향 방지)
    try:
        from modules import coupang_orderer, product_matcher, stock_detector, stock_parser
    except Exception:
        logger.exception("[Stock] 모듈 import 실패 — 중단")
        return 0

    # 1) 캘린더 메모 감지
    try:
        candidates = stock_detector.detect_stock_memos()
    except Exception:
        logger.exception("[Stock] 메모 감지 실패")
        return 0

    if not candidates:
        logger.info("[Stock] 처리 대상 메모 없음")
        return 0

    # 2) Gemini API 파싱 → 품목 평탄화
    window_days = int(stock_cfg.get("duplicate_window_days",
                                    _STOCK_DUPLICATE_WINDOW_DAYS_DEFAULT))
    max_daily = int(stock_cfg.get("max_daily_orders",
                                  _STOCK_MAX_DAILY_ORDERS_DEFAULT))

    # 매핑표 선로드 (canonical 정규화 + 매칭용)
    mapping = product_matcher.load_mapping()

    # 최근 주문 이력도 canonical 이름 집합으로 변환 (별칭 다른 표기까지 중복 차단)
    recent_items_raw = _recent_stock_item_names(window_days)
    recent_canonical = {
        product_matcher.normalize_to_canonical(n, mapping) for n in recent_items_raw
    }
    today_count = _today_order_count()
    now_iso = datetime.now().isoformat()

    to_order: list[dict] = []    # 장바구니 담을 품목
    skipped_rows: list[dict] = []
    unmapped_rows: list[dict] = []
    deferred_rows: list[dict] = []   # 하루 한도 초과로 이연

    # 모든 메모를 1회 API 호출로 일괄 파싱 (PRD 4.2 성능)
    # 실패 시 내부에서 자동으로 단건 폴백하므로 외부는 동일 계약.
    try:
        parsed_by_event = stock_parser.parse_shortage_items_batch([
            {"event_id": m.get("event_id", ""),
             "memo_text": m.get("memo_text", "")}
            for m in candidates
        ])
    except Exception:
        logger.exception("[Stock] 배치 파싱 중 예외 — 개별 폴백")
        parsed_by_event = {}

    for memo in candidates:
        memo_text = memo.get("memo_text", "")
        event_id = memo.get("event_id", "")
        memo_hash = memo.get("memo_hash", "")

        items = parsed_by_event.get(event_id)
        if items is None:
            # 배치 결과에 해당 event_id가 없으면 단건 호출로 보완
            try:
                items = stock_parser.parse_shortage_items(memo_text)
            except Exception:
                logger.exception("[Stock] 단건 파싱 중 예외 — skip 메모")
                items = []

        for it in items:
            raw_name = (it.get("item_name") or "").strip()
            if not raw_name:
                continue

            # 별칭 → canonical 정규화. DB 저장/중복 체크는 모두 canonical로 통일.
            canonical_name = product_matcher.normalize_to_canonical(raw_name, mapping)

            # 자동주문 제외 품목 (예: 카메라필름 → 알리익스프레스 구매) 조기 차단
            skip_info = (product_matcher.is_skip_item(raw_name, mapping)
                         or product_matcher.is_skip_item(canonical_name, mapping))
            if skip_info:
                row = {
                    "detected_at": now_iso,
                    "calendar_event_id": event_id,
                    "memo_hash": memo_hash,
                    "item_name": skip_info["name"],
                    "status": "skipped",
                    "matched_source": "none",
                    "skip_reason": skip_info["reason"],
                }
                _record_stock_order(row)
                skipped_rows.append({"item_name": skip_info["name"],
                                     "reason": skip_info["reason"]})
                continue

            base_row = {
                "detected_at": now_iso,
                "calendar_event_id": event_id,
                "memo_hash": memo_hash,
                "item_name": canonical_name,
            }

            # 중복 차단 (canonical 기준)
            if canonical_name in recent_canonical:
                row = {**base_row,
                       "status": "skipped",
                       "matched_source": "none",
                       "skip_reason": f"{window_days}일 내 이미 주문됨"}
                _record_stock_order(row)
                skipped_rows.append({"item_name": canonical_name,
                                     "reason": row["skip_reason"]})
                continue

            # 하루 한도 초과 → DB 기록 없이 이연 (다음 사이클에서 재시도)
            if today_count + len(to_order) >= max_daily:
                deferred_rows.append({"item_name": canonical_name})
                continue

            # 매핑표 매칭 (1순위).
            # 미매핑이어도 여기서 unmapped로 확정하지 않고 to_order에 url=""로 넘김.
            # add_items_to_cart가 브라우저 오픈 후 match_from_order_history (2순위)로
            # 주문내역 검색을 시도하고, 그래도 없으면 그때 unmapped로 분류한다.
            # raw_name 전달: variant 선별용 (예: "곰곰 쌀과자 달콤한맛" 메모 →
            # canonical "곰곰 쌀과자" 의 variants 중 "달콤한맛" variant 만 선택)
            hit = product_matcher.match_from_mapping(canonical_name, mapping, raw_name=raw_name)

            # 현재 재고 + 최대재고 정의 시 부족분만큼 주문 (예: 장작 max=6, 현재 2 → 4개).
            # current_stock=None 또는 max_stock=0 이면 기존처럼 기본수량 사용.
            quantity = (hit or {}).get("quantity", 1)
            max_stock = int((hit or {}).get("max_stock", 0) or 0)
            current_stock = it.get("current_stock")
            if max_stock > 0 and current_stock is not None:
                needed = max_stock - int(current_stock)
                if needed <= 0:
                    row = {**base_row,
                           "status": "skipped",
                           "matched_source": "mapping",
                           "skip_reason": f"재고 충분 ({current_stock}/{max_stock})"}
                    _record_stock_order(row)
                    skipped_rows.append({"item_name": canonical_name,
                                         "reason": row["skip_reason"]})
                    continue
                quantity = needed

            # variants 배열이 있으면 여러 URL 모두 장바구니에 담기
            # (예: "곰곰 쌀과자" canonical 하나에 고소한맛/달콤한맛 2 variants)
            variants = (hit or {}).get("variants") or []
            if variants:
                for v in variants:
                    v_qty = int(v.get("quantity", quantity) or quantity)
                    to_order.append({
                        "item_name": canonical_name,
                        "url": v.get("url", ""),
                        "quantity": v_qty,
                        "max_price": (hit or {}).get("max_price", 0),
                        "source": (hit or {}).get("source", "mapping"),
                        "_db_base": base_row,
                        "_variant_name": v.get("name", ""),
                    })
            else:
                to_order.append({
                    "item_name": canonical_name,
                    # hit이 없으면 url=""로 넘김 → add_items_to_cart가 주문내역 검색 시도
                    "url": (hit or {}).get("url", ""),
                    "quantity": quantity,
                    "max_price": (hit or {}).get("max_price", 0),
                    "source": (hit or {}).get("source", "mapping") if hit else "need_search",
                    "_db_base": base_row,
                })

    # 3) Playwright 장바구니 담기
    order_result = {"success": [], "skipped": [], "failed": [], "stopped": False, "stop_reason": ""}
    if to_order:
        try:
            order_result = coupang_orderer.add_items_to_cart([
                {k: v for k, v in item.items() if not k.startswith("_")}
                for item in to_order
            ])
        except Exception:
            logger.exception("[Stock] 쿠팡 장바구니 처리 중 예외")

    # 4) 결과 DB 기록
    # variants 다중 URL 대응: (item_name, url) 복합키. 동일 이름 variant 별 구분.
    def _rkey(s: dict) -> tuple:
        return (s.get("item_name", ""), (s.get("url") or s.get("matched_url") or "").split("?")[0])

    result_by_key: dict[tuple, tuple[str, dict]] = {
        _rkey(s): ("success", s) for s in order_result.get("success", [])
    }
    for s in order_result.get("skipped", []):
        result_by_key[_rkey(s)] = ("skipped", s)
    for s in order_result.get("failed", []):
        result_by_key[_rkey(s)] = ("failed", s)
    for s in order_result.get("unmapped", []):
        result_by_key[_rkey(s)] = ("unmapped", s)

    # 이름만으로 lookup 도 남겨둠 (coupang_orderer 가 url 미반환 시 폴백)
    result_by_name = {s["item_name"]: ("success", s) for s in order_result.get("success", [])}
    for s in order_result.get("skipped", []):
        result_by_name[s["item_name"]] = ("skipped", s)
    for s in order_result.get("failed", []):
        result_by_name[s["item_name"]] = ("failed", s)
    for s in order_result.get("unmapped", []):
        result_by_name[s["item_name"]] = ("unmapped", s)

    for item in to_order:
        name = item["item_name"]
        base_row = item["_db_base"]
        # url 포함 복합키로 먼저 조회, 없으면 이름만으로 폴백
        item_url_base = (item.get("url") or "").split("?")[0]
        outcome = result_by_key.get((name, item_url_base)) or result_by_name.get(name)
        if not outcome:
            # 파이프라인 중단 등으로 처리 안 된 경우 → 실패로 기록
            _record_stock_order({**base_row,
                                 "status": "failed",
                                 "matched_source": item.get("source", "mapping"),
                                 "matched_url": item.get("url"),
                                 "quantity": item.get("quantity", 1),
                                 "fail_reason": "처리 미완료 (중단)"})
            continue

        kind, payload = outcome
        row = {**base_row,
               "matched_source": item.get("source", "mapping"),
               "matched_url": item.get("url"),
               "quantity": item.get("quantity", 1)}

        if kind == "success":
            row["status"] = "ordered"
            row["price"] = int(payload.get("price") or 0)
            row["ordered_at"] = datetime.now().isoformat()
            # 주문내역에서 찾아낸 경우 실제 사용한 URL을 matched_url/source에 반영
            if payload.get("url"):
                row["matched_url"] = payload["url"]
            if payload.get("source"):
                row["matched_source"] = payload["source"]
        elif kind == "skipped":
            row["status"] = "skipped"
            row["skip_reason"] = payload.get("reason", "스킵")
        elif kind == "unmapped":
            row["status"] = "unmapped"
            row["matched_source"] = "none"
            row["skip_reason"] = payload.get("reason", "매핑표 및 주문내역 모두 없음")
        else:
            row["status"] = "failed"
            row["fail_reason"] = payload.get("reason", "실패")

        _record_stock_order(row)

    # 5) 카카오 결과 알림 (처리 0건이면 notifier에서 발송 생략)
    notify_payload = {
        "success": order_result.get("success", []),
        "skipped": skipped_rows + order_result.get("skipped", []),
        "unmapped": unmapped_rows + order_result.get("unmapped", []),
        "failed": order_result.get("failed", []),
    }
    try:
        notifier.send_stock_result(notify_payload)
    except Exception:
        logger.exception("[Stock] 결과 알림 발송 중 예외")

    # 이연/중단 알림
    if deferred_rows:
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            notifier.send_stock_alert(
                f"[재고 주문 이연] 오늘 {max_daily}회 한도 도달, "
                f"{len(deferred_rows)}건은 다음 사이클에서 처리 예정",
                dedup_key=f"deferred:{today_str}",
            )
        except Exception:
            logger.exception("[Stock] 이연 알림 중 예외")

    if order_result.get("stopped"):
        reason = order_result.get("stop_reason", "불명")
        try:
            notifier.send_stock_alert(
                f"[재고 자동화 중단] 사유: {reason}\n"
                "쿠팡 재로그인 또는 캡차/SMS 수동 처리 후 재개해주세요.",
                dedup_key=f"stopped:{reason}",
                cooldown_hours=None,
            )
        except Exception:
            logger.exception("[Stock] 중단 알림 중 예외")

    logger.info(
        "[Stock] 완료 — 성공 %d건 / 스킵 %d건 / 매핑필요 %d건 / 실패 %d건 / 이연 %d건",
        len(notify_payload["success"]),
        len(notify_payload["skipped"]),
        len(notify_payload["unmapped"]),
        len(notify_payload["failed"]),
        len(deferred_rows),
    )

    # 로직 ② — 이번 사이클에서 실제로 뭔가 처리했으면 주문내역 기반 매핑 학습 실행.
    # 사용자가 메모 받고 수동 주문까지 한 상태라면 이 시점에 새 상품 자동 매핑됨.
    # 아직 결제 전이면 묶음 매치 없어서 아무것도 안 함 (0 비용).
    if any([notify_payload["success"], notify_payload["skipped"],
            notify_payload["unmapped"], notify_payload["failed"]]):
        try:
            from modules.coupang_orderer import (
                init_browser, close_browser, is_session_valid, _is_cdp_available,
            )
            from modules.product_matcher import sync_mapping_from_orders
            if _is_cdp_available():
                p, browser, ctx, page = init_browser()
                try:
                    if is_session_valid(page):
                        sync_mapping_from_orders(page, mode="instant", lookback_days=14)
                    else:
                        logger.warning("[Stock] 세션 무효 — 매핑 학습 스킵")
                finally:
                    close_browser(p, browser, ctx, page)
            else:
                logger.info("[Stock] CDP 미가용 — 매핑 학습 스킵")
        except Exception:
            logger.exception("[Stock] 매핑 자동 학습 중 예외 (파이프라인에 영향 없음)")

    return len(candidates)


def _run_order_confirmation_scan_if_due() -> None:
    """매일 07시대 1회, 3일+ 경과 미확정(scan_done_at NULL) stock_orders 가 있으면
    주문내역 스캔 + 로직 ①③ 실행.

    Akamai 리스크 최소화 설계:
    - 발동 시각: 07:00~07:59 (쿠팡 저트래픽 시간대)
    - 대상: status='ordered' AND scan_done_at IS NULL AND detected_at <= now-3일
    - 스캔 후 대상 레코드 모두 scan_done_at 세팅 → 다시는 스캔 안 함
    - 조건 미충족 시 브라우저조차 열지 않음

    data/scan_state.json 의 last_run 날짜로 같은 날 1회만 실행.
    """
    now = datetime.now()
    if now.hour != 7:
        return

    # 사전 체크: 대상 레코드 있는지 확인 (브라우저 열기 전)
    try:
        from modules.product_matcher import _stock_orders_pending_scan
        pending = _stock_orders_pending_scan(min_age_days=3)
    except Exception:
        logger.exception("[Scan] 미확정 레코드 조회 실패")
        return
    if not pending:
        return  # 조용히 종료 (매일 07시 호출되므로 로그 남기지 않음)

    state_path = PROJECT_ROOT / "data" / "scan_state.json"
    today_str = now.strftime("%Y-%m-%d")
    try:
        if state_path.exists():
            import json as _json
            last = _json.loads(state_path.read_text(encoding="utf-8")).get("last_run", "")
            if last == today_str:
                logger.info("[Scan] 오늘 이미 실행됨 — 스킵")
                return
    except Exception:
        logger.warning("[Scan] 상태 파일 로드 실패 (강행)", exc_info=True)

    logger.info("[Scan] 주문 확인 스캔 시작 — 대상 %d건", len(pending))
    try:
        from modules.coupang_orderer import (
            init_browser, close_browser, is_session_valid, _is_cdp_available,
        )
        from modules.product_matcher import sync_mapping_from_orders
        if not _is_cdp_available():
            logger.warning("[Scan] CDP 미가용 — 스캔 스킵")
            return
        p, browser, ctx, page = init_browser()
        try:
            if not is_session_valid(page):
                logger.warning("[Scan] 세션 무효 — 스캔 스킵")
                return
            result = sync_mapping_from_orders(page, mode="scheduled", lookback_days=14)
            logger.info(
                "[Scan] 결과: confirmed=%d unconfirmed=%d added=%d pending=%d",
                len(result.get("confirmed", [])),
                len(result.get("unconfirmed", [])),
                len(result.get("added", [])),
                len(result.get("pending", [])),
            )
        finally:
            close_browser(p, browser, ctx, page)

        # 멱등 플래그 저장 (브라우저 정상 종료 후)
        try:
            import json as _json
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                _json.dumps({"last_run": today_str}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[Scan] 상태 파일 저장 실패", exc_info=True)
    except Exception:
        logger.exception("[Scan] 주문 확인 스캔 중 예외")


def _record_last_success() -> None:
    """파이프라인 정상 완료 시각을 data/last_success.txt에 ISO 포맷으로 기록.

    watchdog.check_pipeline_health()와 setup_check()가 이 파일의 mtime으로
    "마지막 성공 후 경과 시간"을 계산한다. 기록 실패는 파이프라인 흐름에
    영향을 주지 않도록 warning 로그만 남기고 넘어간다.
    """
    try:
        flag_path = PROJECT_ROOT / "data" / "last_success.txt"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(datetime.now().isoformat(), encoding="utf-8")
    except Exception:
        logger.warning("[Watchdog] last_success 기록 실패", exc_info=True)


def _vacuum_db_if_needed() -> None:
    """매월 1일 첫 실행 시 VACUUM으로 DB 파일 크기 최적화.

    월별 플래그 파일(data/.vacuum_YYYY_MM)로 멱등성 보장 — 같은 달 재실행에선 no-op.
    VACUUM은 쓰기 락을 필요로 하므로 WAL 모드에서도 짧은 전면 락 구간이 생긴다.
    PRD 4.2의 5분 제약 내에서 끝나는 작업(보통 ~수 초)이라 안전.
    """
    today = date.today()
    flag = PROJECT_ROOT / "data" / f".vacuum_{today.year}_{today.month:02d}"
    if flag.exists():
        return
    try:
        with get_connection() as conn:
            conn.execute("VACUUM")
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        logger.info("[DB] VACUUM 완료 (%d-%02d)", today.year, today.month)
    except Exception:
        logger.warning("[DB] VACUUM 실패", exc_info=True)


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

def _setup_check_stock() -> list[str]:
    """재고 자동주문 시스템 점검. 반환: 문제점 문자열 리스트 (없으면 빈 리스트).

    점검 항목:
      1) data/product_mapping.json 존재 여부 + JSON 파싱
      2) data/coupang_session.json 존재 여부
      3) Gemini API 키 유효성 (실제 호출로 검증)
      4) stock.calendar_name에 접근 가능 여부
    """
    issues: list[str] = []
    print("\n[5/5] 재고 자동주문 시스템")

    try:
        config = load_config()
    except Exception as e:
        print(f"   [FAIL] config 로드 실패: {e}")
        return [f"config.json 로드 실패: {e}"]

    stock_cfg = config.get("stock", {}) or {}
    if not stock_cfg.get("enabled", False):
        print("   [SKIP] stock.enabled=false (비활성)")
        return []

    # 1) 매핑표
    mapping_path = PROJECT_ROOT / "data" / "product_mapping.json"
    if not mapping_path.exists():
        print(f"   [FAIL] 매핑표 없음: {mapping_path}")
        issues.append("data/product_mapping.json 파일을 생성해주세요.")
    else:
        try:
            import json as _json
            with open(mapping_path, "r", encoding="utf-8") as f:
                _json.load(f)
            print(f"   [OK]   product_mapping.json 유효")
        except Exception as e:
            print(f"   [FAIL] 매핑표 파싱 실패: {e}")
            issues.append("data/product_mapping.json을 JSON 형식으로 수정해주세요.")

    # 2) 쿠팡 세션
    session_path = PROJECT_ROOT / "data" / "coupang_session.json"
    if not session_path.exists():
        print(f"   [FAIL] 쿠팡 세션 없음: {session_path}")
        issues.append(
            "python scripts/coupang_import_cookies.py (권장) 또는 "
            "python scripts/coupang_login.py 로 최초 세션을 준비해주세요."
        )
    else:
        print(f"   [OK]   coupang_session.json 존재")

    # 2-1) CDP Chrome 가용성 (Akamai 우회 필수 조건)
    try:
        from modules.coupang_orderer import _is_cdp_available
        if _is_cdp_available():
            print("   [OK]   CDP Chrome(port 9222) 응답 — Akamai 우회 경로 활성")
        else:
            print("   [WARN] CDP Chrome(port 9222) 미응답 — 자동주문 시 알림 전환됨")
            issues.append(
                "python scripts/coupang_chrome_cdp_start.py 로 CDP Chrome을 실행하세요. "
                "미실행 상태면 재고 자동주문은 알림만 보내고 실제 카트 담기를 스킵합니다."
            )
    except Exception as e:
        print(f"   [WARN] CDP 체크 실패: {e}")

    # 3) Gemini API 키 유효성 (실제 호출로 검증)
    try:
        from modules.stock_parser import check_api_key_valid
        is_valid, msg = check_api_key_valid()
        status = "[OK]  " if is_valid else "[FAIL]"
        print(f"   {status} Gemini API: {msg}")
        if not is_valid:
            issues.append(f"Gemini API 점검 실패: {msg}")
    except Exception as e:
        print(f"   [FAIL] Gemini API 점검 중 예외: {e}")
        issues.append("Gemini API 점검 중 예외 발생.")

    # 4) 캘린더 접근
    cal_name = stock_cfg.get("calendar_name", "")
    if not cal_name:
        print("   [FAIL] stock.calendar_name 미설정")
        issues.append("config.json의 stock.calendar_name을 채워주세요.")
    else:
        try:
            memos = calendar.read_stock_memos(cal_name, 1)
            print(f"   [OK]   캘린더 '{cal_name}' 접근 가능 (최근 1일: {len(memos)}건)")
        except Exception as e:
            print(f"   [FAIL] 캘린더 접근 실패: {e}")
            issues.append(f"구글 캘린더 '{cal_name}' 접근 실패.")

    return issues


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

    # 5) 재고 자동주문 시스템
    try:
        stock_issues = _setup_check_stock()
        issues.extend(stock_issues)
    except Exception as e:
        print(f"   [FAIL] 재고 시스템 점검 중 예외: {e}")
        issues.append("재고 시스템 점검 중 예외 발생.")

    # 6) 파이프라인 헬스체크
    print("\n[파이프라인 헬스체크]")
    try:
        from watchdog import check_pipeline_health
        is_healthy, msg = check_pipeline_health()
        status = "[OK]  " if is_healthy else "[WARN]"
        print(f"   {status} {msg}")
        # 헬스체크 실패는 issues에 추가하지 않음 — 최초 실행 / 단순 지연은
        # 설정 오류가 아니므로 점검 전체를 FAIL로 만들지는 않는다.
    except Exception as e:
        print(f"   [FAIL] 헬스체크 중 예외: {e}")

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
    """OS별 자동 실행 등록 방법을 실제 절대경로로 채워서 출력.

    Windows는 `scripts/setup_windows.ps1` 한 방 실행으로 ReservationBot +
    CoupangCDPChrome 두 작업이 자동 등록된다 (pythonw.exe 백그라운드, 5분 반복).
    """
    config = load_config()
    interval = int(config.get("polling_interval_minutes", 60))

    py_path = sys.executable
    main_path = str(PROJECT_ROOT / "main.py")
    cron_log = str(LOG_DIR / "cron.log")
    cron_expr = _cron_expr(interval)
    setup_ps1 = str(PROJECT_ROOT / "scripts" / "setup_windows.ps1")
    uninstall_ps1 = str(PROJECT_ROOT / "scripts" / "uninstall_windows.ps1")

    print("=" * 60)
    print(f" 자동 실행 등록 안내 (폴링 주기: {interval}분)")
    print("=" * 60)

    print("\n[Mac / Linux] crontab -e 실행 후 아래 한 줄 추가:\n")
    print(f"  {cron_expr} {py_path} {main_path} >> {cron_log} 2>&1")

    print("\n[Windows] 관리자 PowerShell에서 아래 한 줄 실행 (원클릭):\n")
    print(f'  powershell -ExecutionPolicy Bypass -File "{setup_ps1}"')
    print("\n  → ReservationBot(5분 반복) + CoupangCDPChrome(로그온 시) 자동 등록")
    print("  → pythonw.exe로 콘솔 창 없이 백그라운드 실행")
    print("\n  제거: ")
    print(f'  powershell -ExecutionPolicy Bypass -File "{uninstall_ps1}"')
    print()
    return 0


# =============================================================
# 실행 중복 방지 Lock 파일
# =============================================================

_LOCK_PATH = PROJECT_ROOT / "data" / ".pipeline.lock"
_LOCK_STALE_SEC = 3600  # 60분 이상 된 락은 이전 실행이 비정상 종료된 것으로 간주


def _acquire_lock():
    """동일 프로세스 중복 실행 방지용 OS 레벨 파일 락.

    Windows(msvcrt) / Unix(fcntl) 둘 다 지원. 락 획득 실패 시 "이미 실행 중"으로
    판정하고 깔끔히 sys.exit(0). 단 60분 이상 된 락 파일은 이전 실행이
    비정상 종료된 것으로 보고 제거 후 재시도한다 (stale lock 방지).

    반환: 락 파일 핸들 (프로세스 종료 전까지 보유 필요). main()이 반환될 때
          파이썬이 자동으로 파일을 닫으며 락도 해제된다.
    """
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _try_lock():
        # Windows
        try:
            import msvcrt
            f = open(_LOCK_PATH, "w")
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return f
            except OSError:
                f.close()
                return None
        except ImportError:
            pass

        # Unix
        try:
            import fcntl
            f = open(_LOCK_PATH, "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                f.close()
                return None
        except ImportError:
            return None

    lock_file = _try_lock()
    if lock_file is not None:
        return lock_file

    # 락 획득 실패 → stale 여부 확인
    try:
        age = time.time() - _LOCK_PATH.stat().st_mtime
        if age > _LOCK_STALE_SEC:
            _LOCK_PATH.unlink()
            logger.warning("[Lock] stale lock 제거 후 재시도 (age=%.0fs)", age)
            lock_file = _try_lock()
            if lock_file is not None:
                return lock_file
    except Exception:
        logger.warning("[Lock] stale 체크 실패", exc_info=True)

    logger.info("[Lock] 이미 실행 중 — 종료")
    sys.exit(0)


# =============================================================
# 엔트리포인트
# =============================================================

def main() -> int:
    lock = _acquire_lock()  # noqa: F841  # 프로세스 수명 동안 유지 필요
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

"""
MacroDroid 웹훅 수신 서버.

네이버 앱 알림 + 에어비앤비 문자를 웹훅으로 수신하고,
캘린더 등록, 카카오 알림, blocked.ics, DB 저장 파이프라인을 실행한다.

사용법:
  python webhook_server.py
"""

import gc
import hashlib
import logging
import re
import sys
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from typing import Optional
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from flask import Flask, jsonify, request

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

formatter = logging.Formatter(
    fmt="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers.clear()

file_handler = RotatingFileHandler(
    LOG_DIR / f"{date.today().isoformat()}.log",
    maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
file_handler.setFormatter(formatter)
root_logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

from modules import blocker, calendar, notifier
from modules.config_loader import load_config
from modules.db import get_connection, init_db

init_db()

app = Flask(__name__)


# =============================================================
# 파싱 — 네이버
# =============================================================

def _parse_naver_body(body: str) -> dict:
    """네이버 앱 알림 본문 파싱."""
    info = {}

    name_match = re.search(r"^(.+?)님[,，]", body)
    if name_match:
        info["guest_name"] = name_match.group(1).strip()

    dates = re.findall(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", body)
    if len(dates) >= 2:
        y1, m1, d1 = dates[0]
        y2, m2, d2 = dates[1]
        info["checkin"] = date(int(y1), int(m1), int(d1))
        info["checkout"] = date(int(y2), int(m2), int(d2))
    elif len(dates) == 1:
        y, m, d = dates[0]
        info["checkin"] = date(int(y), int(m), int(d))

    return info


# =============================================================
# 파싱 — 에어비앤비
# =============================================================

def _resolve_date(month: int, day: int) -> date:
    """월/일로 date 생성. 6개월 이상 과거면 내년으로."""
    year = date.today().year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return date(year, month, min(day, 28))
    if (date.today() - candidate).days > 180:
        candidate = date(year + 1, month, day)
    return candidate


def _parse_airbnb_body(body: str) -> dict:
    """에어비앤비 문자 본문 파싱.

    형식1: "에어비앤비: 근영 님이 4월 30일~5월 1일(1박) 숙박을 예약했습니다."
    형식2: "에어비앤비: 축하드려요! 유정 님이 4월 10일~11일에 1박 숙박을 예약했습니다."
    취소:  "에어비앤비: 근영 님의 예약이 취소되었습니다."
    """
    info = {}

    name_match = re.search(r"(?:에어비앤비:\s*(?:축하드려요!\s*)?)?(\S+?)\s*님[이의]", body)
    if name_match:
        info["guest_name"] = name_match.group(1).strip()

    info["is_cancel"] = "취소" in body

    date_matches = re.findall(r"(\d{1,2})월\s*(\d{1,2})일", body)
    if len(date_matches) >= 2:
        m1, d1 = int(date_matches[0][0]), int(date_matches[0][1])
        m2, d2 = int(date_matches[1][0]), int(date_matches[1][1])
        info["checkin"] = _resolve_date(m1, d1)
        info["checkout"] = _resolve_date(m2, d2)
    elif len(date_matches) == 1:
        m1, d1 = int(date_matches[0][0]), int(date_matches[0][1])
        info["checkin"] = _resolve_date(m1, d1)
        # "4월 30일~5월 1일" 에서 두 번째가 "일" 단위만 있는 경우: "10일~11일"
        day_only = re.search(r"~\s*(\d{1,2})일", body)
        if day_only:
            d2 = int(day_only.group(1))
            m2 = m1 if d2 > d1 else m1 + 1
            info["checkout"] = _resolve_date(m2, d2)

    return info


# =============================================================
# 공통 헬퍼
# =============================================================

def _generate_booking_id(platform: str, guest_name: str, checkin: date) -> str:
    raw = f"{platform}-{guest_name}-{checkin.isoformat()}"
    return f"{platform}-{hashlib.md5(raw.encode()).hexdigest()[:16]}"


# =============================================================
# 파이프라인 — 신규
# =============================================================

def _try_update_guest_name(reservation: dict) -> Optional[dict]:
    """동일 날짜의 기존 예약 중 guest_name이 '?'인 건을 업데이트."""
    guest_name = reservation.get("guest_name", "")
    checkin = reservation["checkin"].isoformat()
    platform = reservation["platform"]

    if not guest_name or guest_name == "?":
        return None

    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, google_event_id_a, google_event_id_b "
            "FROM reservations "
            "WHERE platform = ? AND checkin = ? AND status = 'confirmed' "
            "  AND (guest_name IS NULL OR guest_name IN ('?', '', '확인필요'))",
            (platform, checkin),
        )
        row = cur.fetchone()

    if not row:
        return None

    actual_id = row["booking_id"]
    config = load_config()
    prefix = config.get("platform_prefix", {}).get(platform, "")
    owner_cal = config.get("naver_owner_calendar", "")
    staff_cal = config.get("naver_staff_calendar", "")

    with get_connection() as conn:
        cur2 = conn.execute(
            "SELECT guests FROM reservations WHERE booking_id = ?", (actual_id,)
        )
        guests = cur2.fetchone()["guests"] or config.get("base_guests", 2)

    summary_a = f"{prefix}. {guest_name}. {guests}인"
    summary_b = f"{config.get('staff_name', '')} / {guests}인"

    calendar.update_event_summary(row["google_event_id_a"], owner_cal, summary_a)
    calendar.update_event_summary(row["google_event_id_b"], staff_cal, summary_b)

    with get_connection() as conn:
        conn.execute(
            "UPDATE reservations SET guest_name = ? WHERE booking_id = ?",
            (guest_name, actual_id),
        )
        conn.commit()

    logger.info("[Webhook] 이름 업데이트: %s → %s", actual_id, guest_name)
    return {"status": "ok", "action": "name_updated", "booking_id": actual_id, "guest_name": guest_name}


def _handle_new_reservation(reservation: dict) -> dict:
    booking_id = reservation["booking_id"]
    platform = reservation["platform"]
    checkin_str = reservation["checkin"].isoformat()

    # 동일 날짜에 이름이 없는 기존 예약이 있으면 이름만 업데이트
    name_update = _try_update_guest_name(reservation)
    if name_update:
        return name_update

    # 동일 booking_id 중복 체크
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT id FROM reservations WHERE booking_id = ?", (booking_id,)
        )
        if cur.fetchone():
            return {"status": "skip", "reason": "already_processed", "booking_id": booking_id}

    # 에어비앤비: 같은 체크인 날짜에 이미 Gmail로 처리된 예약이 있으면 skip
    if platform == "airbnb":
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT id FROM reservations "
                "WHERE platform = 'airbnb' AND checkin = ? AND status = 'confirmed'",
                (checkin_str,),
            )
            if cur.fetchone():
                return {"status": "skip", "reason": "already_processed_by_gmail", "checkin": checkin_str}

        # Gmail로 아직 처리 안 됨 → 임시 저장 (캘린더 미생성)
        with get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reservations
                    (platform, booking_id, guest_name, guests,
                     checkin, checkout, status)
                VALUES (?, ?, ?, ?, ?, ?, 'confirmed')
                """,
                (
                    platform,
                    booking_id,
                    "확인필요",
                    reservation.get("guests"),
                    checkin_str,
                    reservation["checkout"].isoformat(),
                ),
            )
            conn.commit()

        logger.info("[Webhook] 에어비앤비 임시 저장: %s (Gmail 대기)", booking_id)
        return {"status": "ok", "action": "pending_gmail", "booking_id": booking_id}

    # 네이버: 즉시 전체 처리
    cal_ids = calendar.create_events(reservation)

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

    blocker.block_airbnb(reservation)
    notifier.send_notification(reservation, "created")

    logger.info("[Webhook] 신규 예약 처리 완료: %s/%s", platform, booking_id)
    return {"status": "ok", "action": "created", "booking_id": booking_id}


# =============================================================
# 파이프라인 — 취소
# =============================================================

def _handle_cancel_reservation(reservation: dict) -> dict:
    guest_name = reservation.get("guest_name", "")
    checkin = reservation["checkin"]
    platform = reservation["platform"]
    booking_id = reservation.get("booking_id")

    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, google_event_id_a, google_event_id_b "
            "FROM reservations "
            "WHERE status != 'cancelled' AND platform = ? "
            "  AND (booking_id = ? OR (guest_name = ? AND checkin = ?))",
            (platform, booking_id, guest_name, checkin.isoformat()),
        )
        row = cur.fetchone()

    if not row:
        return {"status": "skip", "reason": "not_found_or_already_cancelled"}

    actual_booking_id = row["booking_id"]

    calendar.delete_events(
        google_event_id_a=row["google_event_id_a"],
        google_event_id_b=row["google_event_id_b"],
    )

    if platform == "naver":
        blocker.unblock_airbnb(actual_booking_id)

    with get_connection() as conn:
        conn.execute(
            "UPDATE reservations SET status = 'cancelled' WHERE booking_id = ?",
            (actual_booking_id,),
        )
        conn.commit()

    notifier.send_notification(reservation, "deleted")

    logger.info("[Webhook] 취소 예약 처리 완료: %s/%s", platform, actual_booking_id)
    return {"status": "ok", "action": "deleted", "booking_id": actual_booking_id}


# =============================================================
# 오류 알림
# =============================================================

_error_logger = logging.getLogger("webhook_error")
_error_handler = logging.FileHandler(LOG_DIR / "webhook_error.log", encoding="utf-8")
_error_handler.setFormatter(formatter)
_error_logger.addHandler(_error_handler)


def _notify_error(summary: str) -> None:
    """오류를 error 로그에 기록."""
    _error_logger.exception(summary)


# =============================================================
# Flask 엔드포인트
# =============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(silent=True) or {}
        logger.info("[Webhook] 수신: %s", data)

        body = data.get("body", "")
        if not body:
            return jsonify({"status": "error", "reason": "empty_body"}), 200

        title = data.get("title", "")
        platform = data.get("platform", "").lower()

        if not platform or platform not in ("naver", "airbnb"):
            if "에어비앤비" in body or "airbnb" in body.lower():
                platform = "airbnb"
            else:
                platform = "naver"

        if platform == "airbnb":
            parsed = _parse_airbnb_body(body)
            is_cancel = parsed.get("is_cancel", False) or "취소" in title
        else:
            parsed = _parse_naver_body(body)
            is_cancel = "예약취소" in title or "취소" in title

        if not parsed.get("guest_name") or not parsed.get("checkin"):
            logger.warning("[Webhook] 파싱 실패: platform=%s, parsed=%s", platform, parsed)
            return jsonify({"status": "error", "reason": "parse_failed"}), 200

        config = load_config()
        base_guests = config.get("base_guests", 2)
        booking_id = _generate_booking_id(platform, parsed["guest_name"], parsed["checkin"])

        reservation = {
            "platform": platform,
            "booking_id": booking_id,
            "guest_name": parsed["guest_name"],
            "guests": base_guests,
            "checkin": parsed["checkin"],
            "checkout": parsed.get("checkout", parsed["checkin"]),
        }

        if is_cancel:
            result = _handle_cancel_reservation(reservation)
        else:
            result = _handle_new_reservation(reservation)
        return jsonify(result), 200

    except Exception as e:
        _notify_error(str(e))
        return jsonify({"status": "error", "reason": str(e)}), 200
    finally:
        gc.collect()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.errorhandler(Exception)
def handle_exception(e):
    _notify_error(f"전역 예외: {e}")
    return jsonify({"status": "error", "reason": str(e)}), 200


# =============================================================
# 메인 실행 (자동 재시작)
# =============================================================

if __name__ == "__main__":
    import time

    while True:
        try:
            try:
                notifier._send_kakao_message("[웹훅 서버 시작] 정상 실행 중")
            except Exception:
                pass
            logger.info("[Webhook] 서버 시작: http://0.0.0.0:5000")
            app.run(host="0.0.0.0", port=5000, debug=False)
        except KeyboardInterrupt:
            logger.info("[Webhook] 서버 종료 (Ctrl+C)")
            break
        except Exception as e:
            logger.exception("[Webhook] 서버 비정상 종료: %s", e)
            try:
                notifier._send_kakao_message(f"[웹훅 서버 재시작] 예기치 않은 종료 후 재시작됨: {e}")
            except Exception:
                pass
            logger.info("[Webhook] 5초 후 재시작...")
            time.sleep(5)

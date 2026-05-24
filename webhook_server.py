"""
MacroDroid 웹훅 수신 서버.

네이버 앱 알림 + 에어비앤비 문자를 웹훅으로 수신하고,
캘린더 등록, Discord 알림, blocked.ics, DB 저장 파이프라인을 실행한다.

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

def _handle_new_reservation(reservation: dict) -> dict:
    """신규 예약: pending_email 로 claim 후 Gmail 정보와 합쳐 확정(merge-before-create).

    트리거(웹훅)만으로는 캘린더/알림을 만들지 않는다. claim 직후 즉시 Gmail 확인을
    시도하고(보통 메일이 이미 도착해 즉시 확정), 아직이면 pending 상태로 두어
    webhook_server 워처(1분)와 main.py 파이프라인(5분)이 재시도한다.

    네이버=웹훅(이름)+Gmail(인원), 에어비앤비=웹훅(날짜)+Gmail(이름·인원).
    """
    from modules import reservation_flow

    booking_id = reservation["booking_id"]
    platform = reservation["platform"]

    if not reservation_flow.claim_pending(reservation):
        return {"status": "skip", "reason": "duplicate_or_exists", "booking_id": booking_id}

    status = reservation_flow.finalize_if_ready(booking_id)
    logger.info("[Webhook] 신규 claim: %s/%s → %s", platform, booking_id, status)
    return {"status": "ok", "action": status, "booking_id": booking_id}


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

        # 에어비앤비는 iCal+Gmail 파이프라인(main.py)이 트리거·날짜·이름·인원·취소까지
        # 전부 처리한다(DB 14건 중 13건이 iCal산, 웹훅산 1건은 중복 유령 예약이었음).
        # 웹훅 SMS는 잉여이며 '취소-후 늦은 SMS'로 유령 예약을 만들어 중복 알림을
        # 유발하므로 무시한다. 네이버는 iCal이 없어 웹훅이 주 경로이므로 그대로 처리.
        if platform == "airbnb":
            logger.info("[Webhook] 에어비앤비 웹훅 무시 (iCal+Gmail 파이프라인이 처리): title=%s", title)
            return jsonify({"status": "ignored", "reason": "airbnb_handled_by_ical"}), 200

        # 여기 도달하는 건 네이버뿐 (에어비앤비는 위에서 early-return).
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


@app.errorhandler(404)
def handle_404(e):
    logger.info("[Webhook] 404: %s %s", request.method, request.path)
    return jsonify({"status": "error", "reason": "not_found", "path": request.path}), 404


@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import NotFound
    if isinstance(e, NotFound):
        return handle_404(e)
    _error_logger.exception("전역 예외: %s", e)
    return jsonify({"status": "error", "reason": str(e)}), 200


# =============================================================
# pending_email 빠른 재시도 워처 (0~5분, 1분 간격)
# =============================================================

def _pending_watcher_loop() -> None:
    """60초마다 생성 5분 이내 pending_email 예약을 finalize 재시도.

    main.py 파이프라인은 5분 주기라 sub-5분 granularity가 불가능하므로, 항상 떠 있는
    이 서버가 0~5분 구간의 1분 단위 재시도를 담당한다(웹훅·iCal 트리거 공통).
    5~60분 구간과 60분 만료 강제확정은 파이프라인이 처리한다.
    """
    import time
    from modules import reservation_flow

    while True:
        try:
            time.sleep(60)
            n = reservation_flow.retry_pending(
                max_age_minutes=reservation_flow.FAST_WINDOW_MINUTES
            )
            if n:
                logger.info("[Watcher] pending 확정 %d건", n)
        except Exception:
            logger.exception("[Watcher] pending 워처 예외 (계속)")


# =============================================================
# 메인 실행 (자동 재시작)
# =============================================================

if __name__ == "__main__":
    import time
    import threading

    threading.Thread(target=_pending_watcher_loop, daemon=True).start()
    logger.info("[Webhook] pending 워처 시작 (60초 주기, 0~5분 창)")

    while True:
        try:
            logger.info("[Webhook] 서버 시작: http://0.0.0.0:5000")
            app.run(host="0.0.0.0", port=5000, debug=False)
        except KeyboardInterrupt:
            logger.info("[Webhook] 서버 종료 (Ctrl+C)")
            break
        except Exception as e:
            logger.exception("[Webhook] 서버 비정상 종료: %s", e)
            logger.info("[Webhook] 5초 후 재시작...")
            time.sleep(5)

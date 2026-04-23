"""
플랫폼 간 더블부킹 방지를 위한 차단/해제 모듈.

- 네이버 예약이 들어오면 → 에어비앤비 iCal에 차단 이벤트 기록
  (에어비앤비 측은 우리가 제공하는 iCal URL을 읽어 자동으로 해당 날짜를 막음)
- 에어비앤비 예약이 들어오면 → 네이버 플레이스 관리자에 차단 처리
  (API 가능하면 API, 아니면 Playwright 브라우저 자동화)

각 함수는 성공/실패(또는 'skip-성공')를 bool로 반환한다.
"""

import base64
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from icalendar import Calendar, Event

from modules.config_loader import load_config
from modules.db import get_connection
from modules.env_loader import load_env


logger = logging.getLogger(__name__)


# 프로젝트 루트 (config 내 상대경로 해석 기준)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# =============================================================
# 공통 헬퍼
# =============================================================

def _blocked_ical_path() -> Path:
    """config.json에서 blocked.ics 경로를 읽어 절대 경로로 반환."""
    config = load_config()
    rel = config.get("blocked_ical_path", "ical/blocked.ics")
    return _PROJECT_ROOT / rel


def _airbnb_block_uid(booking_id: str) -> str:
    """에어비앤비 iCal 차단 이벤트의 UID. 네이버 예약 ID를 접두어로 구분."""
    return f"naver-{booking_id}@reservation-bot"


def _load_or_create_calendar(path: Path) -> Calendar:
    """blocked.ics를 읽어 Calendar 객체 반환. 파일 없으면 빈 캘린더 새로 생성."""
    if path.exists():
        with open(path, "rb") as f:
            return Calendar.from_ical(f.read())
    cal = Calendar()
    cal.add("prodid", "-//reservation-bot//blocked dates//KR")
    cal.add("version", "2.0")
    return cal


def _write_calendar(path: Path, cal: Calendar) -> None:
    """Calendar 객체를 ics 파일로 덮어쓴 뒤 GitHub에 push."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(cal.to_ical())
    _push_to_github(path)


# =============================================================
# GitHub API push
# =============================================================

_GITHUB_REPO = "burnhaam/socams-ical"
_GITHUB_FILE_PATH = "blocked.ics"
_GITHUB_API_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{_GITHUB_FILE_PATH}"


def _github_headers() -> dict:
    env = load_env()
    token = env.get("GITHUB_TOKEN", "")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"} if token else {}


def _push_to_github(local_path: Path) -> bool:
    """blocked.ics를 GitHub API로 push. 실패 시 3회 재시도."""
    headers = _github_headers()
    if not headers:
        logger.info("[GitHub] GITHUB_TOKEN 미설정 — push skip")
        return False

    with open(local_path, "rb") as f:
        local_bytes = f.read()
    content_b64 = base64.b64encode(local_bytes).decode()

    for attempt in range(3):
        try:
            sha = None
            resp = requests.get(_GITHUB_API_URL, headers=headers, timeout=30)
            if resp.status_code == 200:
                sha = resp.json().get("sha")

            payload = {
                "message": f"blocked.ics 업데이트 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                "content": content_b64,
                "branch": "main",
            }
            if sha:
                payload["sha"] = sha

            resp = requests.put(_GITHUB_API_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()

            if _verify_github_sync(local_bytes, headers):
                logger.info("[GitHub] blocked.ics push + 검증 OK")
                return True

            logger.warning("[GitHub] push 성공했으나 검증 실패, 재시도 %d/3", attempt + 1)
        except requests.RequestException as e:
            logger.error("[GitHub] push 실패 (시도 %d/3): %s", attempt + 1, e)

        if attempt < 2:
            import time
            time.sleep(1)

    try:
        from datetime import date
        from modules.notifier import _send_kakao_message
        # 같은 날 같은 사유 알림은 영구 1회
        _send_kakao_message(
            "[GitHub push 실패] blocked.ics 수동 확인 필요",
            dedup_key=f"github_push_failed:{date.today().isoformat()}",
            cooldown_hours=None,
        )
    except Exception:
        pass
    return False


def _verify_github_sync(local_bytes: bytes, headers: dict) -> bool:
    """GitHub API로 실제 파일 내용이 로컬과 동일한지 검증."""
    try:
        resp = requests.get(_GITHUB_API_URL, headers=headers, timeout=30)
        if resp.status_code != 200:
            return False
        remote_bytes = base64.b64decode(resp.json().get("content", ""))
        return remote_bytes.strip() == local_bytes.strip()
    except Exception:
        return False


def sync_github_if_needed() -> bool:
    """로컬 blocked.ics와 GitHub 내용이 다르면 push. main.py 폴링에서 호출."""
    path = _blocked_ical_path()
    if not path.exists():
        return False

    headers = _github_headers()
    if not headers:
        return False

    with open(path, "rb") as f:
        local_bytes = f.read()

    if _verify_github_sync(local_bytes, headers):
        return False

    logger.warning("[GitHub] 로컬과 GitHub 불일치 감지 — 동기화 시도")
    return _push_to_github(path)


# =============================================================
# 에어비앤비 차단/해제 (iCal 파일 기반)
# =============================================================

def block_airbnb(reservation: dict) -> bool:
    """네이버 예약 정보를 받아 blocked.ics에 차단 VEVENT를 추가."""
    booking_id = reservation.get("booking_id")
    checkin = reservation.get("checkin")
    checkout = reservation.get("checkout")

    if not (booking_id and isinstance(checkin, date) and isinstance(checkout, date)):
        logger.error("[Blocker] block_airbnb: 유효하지 않은 예약 정보 %s", reservation)
        return False

    path = _blocked_ical_path()
    uid = _airbnb_block_uid(booking_id)

    try:
        cal = _load_or_create_calendar(path)

        # 이미 같은 UID로 차단되어 있으면 중복 추가하지 않음
        for comp in cal.walk("VEVENT"):
            if str(comp.get("UID", "")) == uid:
                logger.info("[Blocker] 이미 차단된 UID (skip): %s", uid)
                return True

        event = Event()
        event.add("uid", uid)
        event.add("summary", "BLOCKED")
        event.add("dtstart", checkin)   # 종일(date)로 자동 인식
        event.add("dtend", checkout)
        event.add("dtstamp", datetime.now(timezone.utc))
        cal.add_component(event)

        _write_calendar(path, cal)
        logger.info(
            "[Blocker] 에어비앤비 차단 추가 OK (uid=%s, %s ~ %s)",
            uid, checkin, checkout,
        )
        return True
    except Exception as e:
        logger.error("[Blocker] block_airbnb 실패: %s", e)
        return False


def unblock_airbnb(booking_id: str) -> bool:
    """blocked.ics에서 해당 네이버 예약의 VEVENT를 찾아 제거."""
    if not booking_id:
        logger.error("[Blocker] unblock_airbnb: booking_id 누락")
        return False

    path = _blocked_ical_path()
    uid = _airbnb_block_uid(booking_id)

    # 차단 파일 자체가 없으면 해제할 것도 없으므로 성공으로 간주
    if not path.exists():
        logger.info("[Blocker] blocked.ics 없음 — 해제 skip")
        return True

    try:
        cal = _load_or_create_calendar(path)

        before = len(cal.subcomponents)
        cal.subcomponents = [
            c for c in cal.subcomponents
            if not (c.name == "VEVENT" and str(c.get("UID", "")) == uid)
        ]
        after = len(cal.subcomponents)

        if before == after:
            logger.info("[Blocker] 해제 대상 VEVENT 없음: %s", uid)
            return True

        _write_calendar(path, cal)
        logger.info("[Blocker] 에어비앤비 차단 해제 OK (uid=%s)", uid)
        return True
    except Exception as e:
        logger.error("[Blocker] unblock_airbnb 실패: %s", e)
        return False


# =============================================================
# 네이버 차단/해제
# =============================================================

# 네이버 예약 파트너 API 엔드포인트 (실제 사용 시 인증/스펙 확인 필요)
_NAVER_PARTNER_BLOCK_URL = "https://partner.booking.naver.com/v1/places/{place_id}/block"


def _block_naver_api(reservation: dict, block: bool) -> bool:
    """네이버 파트너 API로 날짜 차단/해제. block=False면 해제."""
    env = load_env()
    place_id = env.get("NAVER_PLACE_ID", "")

    if not place_id:
        logger.error("[Blocker] NAVER_PLACE_ID 미설정")
        return False

    url = _NAVER_PARTNER_BLOCK_URL.format(place_id=place_id)
    access_token = ""
    method = "POST" if block else "DELETE"
    payload = {
        "startDate": reservation["checkin"].isoformat(),
        "endDate": reservation["checkout"].isoformat(),
        "reason": "외부 플랫폼 예약으로 차단" if block else "외부 플랫폼 취소로 해제",
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = requests.request(method, url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info(
            "[Blocker] 네이버 %s OK (%s ~ %s)",
            "차단" if block else "해제",
            reservation["checkin"], reservation["checkout"],
        )
        return True
    except requests.RequestException as e:
        logger.error(
            "[Blocker] 네이버 %s API 실패: %s",
            "차단" if block else "해제", e,
        )
        return False


def _block_naver_playwright(reservation: dict, block: bool) -> bool:
    """Playwright로 네이버 플레이스 관리자 페이지를 조작해 날짜 차단/해제.

    실제 UI 셀렉터는 네이버 측 화면 변경에 따라 바뀔 수 있으므로
    운영자가 한 번 수동 확인 후 업데이트해야 한다.
    """
    try:
        from playwright.sync_api import sync_playwright  # 지연 import (미설치 환경 허용)
    except ImportError:
        logger.error("[Blocker] playwright 미설치 — `pip install playwright` 후 `playwright install` 필요")
        return False

    checkin: date = reservation["checkin"]
    checkout: date = reservation["checkout"]

    try:
        with sync_playwright() as p:
            # 최초 1회 수동 로그인 후 storage_state로 세션을 재사용하는 것을 권장
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(storage_state=str(_PROJECT_ROOT / "naver_state.json"))
            page = context.new_page()

            # 1) 네이버 스마트플레이스 예약 관리 페이지 이동
            #    TODO: 실제 URL 확인 — 예) https://partner.booking.naver.com/bizes/{id}/booking
            page.goto("https://partner.booking.naver.com/")

            # 2) 예약 관리 > 휴무일/차단 설정 메뉴 진입
            #    TODO: selector 확인 — 예) page.click("text=휴무일 설정")
            # page.click("TODO_SELECTOR_block_menu")

            # 3) 캘린더에서 checkin ~ checkout 범위 선택
            #    TODO: selector 확인 — 날짜 피커의 data-date 속성 등 이용
            # page.click(f"[data-date='{checkin.isoformat()}']")
            # page.click(f"[data-date='{(checkout - timedelta(days=1)).isoformat()}']")

            # 4) 차단/해제 버튼 클릭
            #    TODO: selector 확인 — 예) page.click("button:has-text('차단')")
            # page.click("TODO_SELECTOR_confirm_block" if block else "TODO_SELECTOR_confirm_unblock")

            # 5) 저장 후 상태 저장
            context.storage_state(path=str(_PROJECT_ROOT / "naver_state.json"))
            browser.close()

        logger.warning(
            "[Blocker] Playwright %s 플로우는 셀렉터 수동 구현이 필요 (%s ~ %s)",
            "차단" if block else "해제", checkin, checkout,
        )
        # 실제 셀렉터를 넣기 전까지는 미완료로 취급
        return False
    except Exception as e:
        logger.error("[Blocker] Playwright %s 실패: %s", "차단" if block else "해제", e)
        return False


def block_naver(reservation: dict) -> bool:
    """네이버 측 날짜 차단. config의 naver_block_method에 따라 API/Playwright 분기."""
    checkin = reservation.get("checkin")
    checkout = reservation.get("checkout")
    if not (isinstance(checkin, date) and isinstance(checkout, date)):
        logger.error("[Blocker] block_naver: 유효하지 않은 날짜")
        return False

    config = load_config()
    method = config.get("naver_block_method", "api").lower()

    if method == "api":
        return _block_naver_api(reservation, block=True)
    if method == "playwright":
        return _block_naver_playwright(reservation, block=True)

    logger.error("[Blocker] 알 수 없는 naver_block_method: %s", method)
    return False


def unblock_naver(reservation: dict) -> bool:
    """네이버 측 날짜 차단 해제.

    같은 날짜 범위에 다른 활성 예약이 DB에 남아있으면 해제하지 않고 skip.
    (다른 예약이 여전히 점유 중이므로 차단이 유지되어야 함)
    """
    booking_id = reservation.get("booking_id")
    checkin = reservation.get("checkin")
    checkout = reservation.get("checkout")
    if not (booking_id and isinstance(checkin, date) and isinstance(checkout, date)):
        logger.error("[Blocker] unblock_naver: 유효하지 않은 예약 정보")
        return False

    # 겹치는 다른 활성 예약 검사 (본인 제외)
    #   기간 겹침 조건: existing.checkin < 요청.checkout AND existing.checkout > 요청.checkin
    try:
        with get_connection() as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*) FROM reservations
                 WHERE booking_id != ?
                   AND status != 'cancelled'
                   AND checkin  < ?
                   AND checkout > ?
                """,
                (booking_id, checkout.isoformat(), checkin.isoformat()),
            )
            overlap = cur.fetchone()[0]
    except Exception as e:
        logger.error("[Blocker] unblock_naver: 겹침 조회 실패: %s", e)
        return False

    if overlap > 0:
        logger.info(
            "[Blocker] 겹치는 다른 활성 예약 %d건 존재 — 네이버 해제 skip (%s ~ %s)",
            overlap, checkin, checkout,
        )
        return True

    config = load_config()
    method = config.get("naver_block_method", "api").lower()

    if method == "api":
        return _block_naver_api(reservation, block=False)
    if method == "playwright":
        return _block_naver_playwright(reservation, block=False)

    logger.error("[Blocker] 알 수 없는 naver_block_method: %s", method)
    return False

"""
Discord 알림 모듈.

예약 생성/취소, 인원 변경, 3행시, 재고 자동주문 결과/긴급 알림을
Discord 웹훅으로 전송한다. 호출 실패는 예외로 던지지 않고 로그만 남긴다.

dedup_key 기반의 영구/쿨다운 디듀프(notify_state.json)를 제공한다.
"""

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from modules.env_loader import load_env

try:
    from google import genai
except ImportError:
    genai = None


logger = logging.getLogger(__name__)


# 알림 중복/쿨다운 상태 저장 파일. 키 → 마지막 발송 ISO 시각.
_NOTIFY_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "notify_state.json"


# =============================================================
# Discord 웹훅
# =============================================================

def _send_discord_webhook(text: str) -> bool:
    """Discord 채널에 웹훅으로 메시지 발송. 실패해도 예외 미전파.

    DISCORD_WEBHOOK_URL 미설정이면 조용히 스킵.
    """
    env = load_env()
    webhook_url = env.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False
    # Discord 메시지 제한: 2000자. 초과 시 잘라냄.
    payload = {"content": text[:1950] + ("…" if len(text) > 1950 else "")}
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True
        logger.warning("[Discord] 웹훅 응답 %s: %s", r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        logger.warning("[Discord] 웹훅 전송 실패: %s", e)
        return False
    except Exception:
        logger.exception("[Discord] 웹훅 예기치 못한 오류")
        return False


# 플랫폼 코드 → 알림 메시지 표기용 한글 이름
_PLATFORM_DISPLAY = {
    "airbnb": "에어비앤비",
    "naver": "네이버",
}


# =============================================================
# 알림 디듀프/쿨다운
# =============================================================

def _load_notify_state() -> dict:
    try:
        if _NOTIFY_STATE_PATH.exists():
            return json.loads(_NOTIFY_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("[Notify] 상태 파일 로드 실패: %s", _NOTIFY_STATE_PATH)
    return {}


def _save_notify_state(state: dict) -> None:
    try:
        _NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _NOTIFY_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("[Notify] 상태 파일 저장 실패: %s", _NOTIFY_STATE_PATH)


def _should_send(dedup_key: str, cooldown_hours: Optional[float]) -> bool:
    """dedup_key 기반으로 이번 발송을 허용할지 판단.

    cooldown_hours=None → 키당 영구 1회 (이미 보낸 적 있으면 False).
    cooldown_hours=N    → N시간 이내 같은 키는 False.
    """
    if not dedup_key:
        return True
    state = _load_notify_state()
    last = state.get(dedup_key)
    if not last:
        return True
    if cooldown_hours is None:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now() - last_dt >= timedelta(hours=cooldown_hours)


def _mark_sent(dedup_key: str) -> None:
    if not dedup_key:
        return
    state = _load_notify_state()
    state[dedup_key] = datetime.now().isoformat()
    _save_notify_state(state)


def clear_dedup_keys(keys: list[str]) -> int:
    """주어진 dedup 키들을 notify_state에서 제거. 재예약(취소→재확정) 시
    같은 booking_id의 영구 키가 남아 알림이 스킵되는 것을 막기 위해 호출한다.
    반환: 실제 삭제된 키 수.
    """
    state = _load_notify_state()
    removed = 0
    for k in keys:
        if k in state:
            del state[k]
            removed += 1
    if removed:
        _save_notify_state(state)
    return removed


# =============================================================
# 메시지 생성
# =============================================================

# action → 사용할 템플릿 분류. created/blocked는 '신규', deleted/unblocked는 '취소'.
_NEW_ACTIONS = {"created", "blocked"}
_CANCEL_ACTIONS = {"deleted", "unblocked"}


def _format_date(value) -> str:
    """date 또는 문자열을 'YYYY-MM-DD' 포맷으로 통일."""
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value
    return "?"


def _build_message(reservation: dict, action: str) -> Optional[str]:
    """action에 따라 신규/취소 메시지 본문을 생성."""
    platform_code = reservation.get("platform", "")
    platform_label = _PLATFORM_DISPLAY.get(platform_code, platform_code or "?")
    guest_name = reservation.get("guest_name") or "예약자"
    guests = reservation.get("guests")
    guests_str = f"{guests}" if guests is not None else "?"
    checkin = _format_date(reservation.get("checkin"))
    checkout = _format_date(reservation.get("checkout"))

    # 네이버 신규의 '🚫 에어비앤비 차단 완료'는 실제 반영 시점에 별도 발송
    # (send_airbnb_block_confirmed). 에어비앤비 차단은 우리 iCal을 에어비앤비가
    # 시간당 1회 가져가 반영하므로 예약 생성 즉시 '완료'라고 말할 수 없다.
    _BLOCK_LINE = {
        "airbnb": "\n⚠️ 네이버 플레이스 수동 차단 필요",
    }
    # 네이버 취소의 '🔓 에어비앤비 차단 해제 완료'도 실제 반영(에어비앤비 export iCal에서
    # 'Not available'가 사라짐) 시점에 confirm_airbnb_unblocks → send_airbnb_unblock_confirmed
    # 로 별도 발송한다. 생성 시 '차단 완료'를 즉시 말 못 하는 것과 같은 이유(에어비앤비는
    # 우리 iCal을 시간당 1회 가져가므로 취소 즉시 '해제 완료'라고 말할 수 없다).
    _UNBLOCK_LINE = {
        "airbnb": "\n🔓 네이버 플레이스 수동 해제 필요",
    }

    if action in _NEW_ACTIONS:
        return (
            "[예약 알림]\n"
            f"플랫폼: {platform_label}\n"
            f"예약자: {guest_name} / {guests_str}인\n"
            f"📅 {checkin}~{checkout}\n"
            "✅ 캘린더 등록 완료"
            + _BLOCK_LINE.get(platform_code, "")
        )

    if action in _CANCEL_ACTIONS:
        return (
            "[취소 알림]\n"
            f"플랫폼: {platform_label}\n"
            f"예약자: {guest_name} / {guests_str}인\n"
            f"📅 {checkin}~{checkout}\n"
            "🗑 캘린더 삭제 완료"
            + _UNBLOCK_LINE.get(platform_code, "")
        )

    logger.error("[Notify] 알 수 없는 action: %s", action)
    return None


# =============================================================
# 알림 전송
# =============================================================

def _send_message(text: str,
                  dedup_key: Optional[str] = None,
                  cooldown_hours: Optional[float] = None) -> bool:
    """Discord 웹훅으로 메시지 발송.

    dedup_key 지정 시 _should_send/_mark_sent로 중복 발송 차단.
    cooldown_hours=None 이면 영구 1회 (이미 보낸 키는 다시 안 보냄).
    """
    if dedup_key and not _should_send(dedup_key, cooldown_hours):
        logger.info("[Notify] 중복 차단 (dedup_key=%s)", dedup_key)
        return False

    ok = _send_discord_webhook(text)

    if dedup_key and ok:
        _mark_sent(dedup_key)
    return ok


def send_notification(reservation: dict, action: str) -> None:
    """예약/취소 알림을 Discord로 전송.

    동일 (platform, booking_id, action) 조합은 영구 1회만 발송.
    """
    message = _build_message(reservation, action)
    if not message:
        return

    booking_id = reservation.get("booking_id", "")
    platform = reservation.get("platform", "")
    dedup_key = f"reservation:{platform}:{booking_id}:{action}" if booking_id else ""

    if dedup_key and not _should_send(dedup_key, None):
        logger.info("[Notify] 중복 차단 — 이미 발송 (dedup_key=%s)", dedup_key)
        return

    ok = _send_discord_webhook(message)
    if ok:
        logger.info("[Notify] 알림 전송 OK (action=%s)", action)
    else:
        logger.error("[Notify] 알림 전송 실패 (action=%s)", action)

    if dedup_key:
        # 성공 여부와 무관하게 한 번 시도한 키는 기록 (Discord 미설정 환경 고려).
        _mark_sent(dedup_key)


def send_airbnb_block_confirmed(reservation: dict) -> None:
    """에어비앤비가 우리 차단(blocked.ics)을 실제로 가져가 반영했을 때 발송.

    confirm_airbnb_blocks()가 에어비앤비 export iCal에 'Not available'로 뜬 것을
    확인한 뒤 호출한다. booking_id당 영구 1회(전송 성공 시에만 기록).
    """
    booking_id = reservation.get("booking_id", "")
    name = reservation.get("guest_name") or "예약자"
    checkin = _format_date(reservation.get("checkin"))
    checkout = _format_date(reservation.get("checkout"))
    message = (
        "🚫 에어비앤비 해당 날짜 차단 완료\n"
        f"예약자: {name}\n"
        f"📅 {checkin}~{checkout}"
    )
    dedup_key = f"airbnb_block_confirmed:{booking_id}" if booking_id else None
    if _send_message(message, dedup_key=dedup_key):
        logger.info("[Notify] 에어비앤비 차단 완료 알림 OK: %s", booking_id)
    else:
        logger.warning("[Notify] 에어비앤비 차단 완료 알림 미발송: %s", booking_id)


def send_airbnb_unblock_confirmed(reservation: dict) -> None:
    """에어비앤비가 우리 차단 해제(blocked.ics에서 제거)를 실제로 반영했을 때 발송.

    confirm_airbnb_unblocks()가 에어비앤비 export iCal에서 해당 구간의 'Not available'가
    사라진 것을 확인한 뒤 호출한다. booking_id당 영구 1회(전송 성공 시에만 기록).
    """
    booking_id = reservation.get("booking_id", "")
    name = reservation.get("guest_name") or "예약자"
    checkin = _format_date(reservation.get("checkin"))
    checkout = _format_date(reservation.get("checkout"))
    message = (
        "🔓 에어비앤비 해당 날짜 차단 해제 완료\n"
        f"예약자: {name}\n"
        f"📅 {checkin}~{checkout}"
    )
    dedup_key = f"airbnb_unblock_confirmed:{booking_id}" if booking_id else None
    if _send_message(message, dedup_key=dedup_key):
        logger.info("[Notify] 에어비앤비 차단 해제 완료 알림 OK: %s", booking_id)
    else:
        logger.warning("[Notify] 에어비앤비 차단 해제 완료 알림 미발송: %s", booking_id)


def send_guests_update(guest_name: str, old_guests: int, new_guests: int) -> None:
    """인원수 변경 알림을 Discord로 전송."""
    message = f"[인원 업데이트] {guest_name}님 예약 {old_guests}인 → {new_guests}인으로 변경"
    if _send_message(message):
        logger.info("[Notify] 인원 업데이트 알림 OK: %s %d→%d", guest_name, old_guests, new_guests)
    else:
        logger.error("[Notify] 인원 업데이트 알림 실패: %s", guest_name)


# =============================================================
# 3행시 생성 및 전송
# =============================================================

def _is_valid_samhaengsi(text: str, name: str) -> bool:
    """3행시 6개가 포함되어 있는지 검증. 첫 글자로 시작하는 행이 최소 6회."""
    first_char = name[0]
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    starts = sum(1 for l in lines if l.startswith(first_char))
    return starts >= 6


def _call_gemini(prompt: str) -> Optional[str]:
    """Gemini API 단일 호출. 429 시 백업 키로 자동 전환."""
    try:
        from modules.gemini_client import generate_content_with_fallback
        response = generate_content_with_fallback(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "system_instruction": "당신은 재치있고 감동적인 3행시 작가입니다.",
                "max_output_tokens": 1500,
                "temperature": 0.9,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        return response.text.strip() if response.text else None
    except Exception as e:
        logger.error("[3행시] Gemini API 호출 실패: %s", e)
        return None


def _generate_samhaengsi(name: str) -> Optional[str]:
    """Gemini API로 이름 3행시 생성. 불완전하면 1회 재시도."""
    if genai is None:
        logger.error("[3행시] google-genai 패키지 미설치")
        return None

    env = load_env()
    api_key = env.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("[3행시] GEMINI_API_KEY 미설정")
        return None

    chars = " ".join(name)
    prompt = (
        f'"{name}" ({chars}) 이름으로 3행시를 6개 지어줘.\n\n'
        "조건:\n"
        f"- 각 3행시는 반드시 {len(name)}행으로 구성\n"
        f"- 첫 번째 행은 '{name[0]}'로 시작, "
        + (f"두 번째 행은 '{name[1]}'로 시작, " if len(name) > 1 else "")
        + (f"세 번째 행은 '{name[2]}'로 시작\n" if len(name) > 2 else "\n")
        + "- 칭찬하는 유쾌하고 감동적인 내용\n"
        "- 각 3행시마다 이모지 1~2개 포함\n"
        "- 마지막 행에 이름을 한 번 더 불러주기\n"
        "- 6개 3행시 사이에 빈 줄로 구분\n"
        "- 3행시만 출력하고 번호, 제목, 설명은 절대 붙이지 마\n\n"
        "예시 (김소라):\n"
        "김 - 김치처럼 매콤한 매력,\n"
        "소 - 소녀의 순수하고,\n"
        "라 - 라디오 같은 따뜻한 목소리! 김소라님! 🎶\n\n"
        f'이제 "{name}" 3행시 6개를 지어줘:'
    )

    result = _call_gemini(prompt)
    if result and _is_valid_samhaengsi(result, name):
        return result

    logger.warning("[3행시] 불완전한 응답, 재시도: %s", result[:50] if result else "None")
    retry = _call_gemini(prompt)
    if retry and _is_valid_samhaengsi(retry, name):
        return retry

    logger.error("[3행시] 재시도 후에도 불완전: %s", retry[:50] if retry else "None")
    return retry or result


def _is_english_name(name: str) -> bool:
    """영문 이름 여부 확인."""
    import re
    return bool(re.search(r"[a-zA-Z]", name))


def _convert_english_to_korean(name: str) -> Optional[str]:
    """Gemini API로 영문 이름을 한국어로 변환."""
    if genai is None:
        return None

    env = load_env()
    api_key = env.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    prompt = (
        "다음 영문 이름을 한국어로 변환해줘.\n"
        "성은 뒤로, 이름은 앞으로 (한국식 순서).\n"
        "이름만 출력하고 설명 붙이지 마.\n"
        f"예: junyeon hwang → 황준연\n\n"
        f"{name}"
    )

    try:
        from modules.gemini_client import generate_content_with_fallback
        response = generate_content_with_fallback(
            model="gemini-2.5-flash",
            config={"max_output_tokens": 50, "temperature": 0.1, "thinking_config": {"thinking_budget": 0}},
            contents=prompt,
        )
        result = response.text.strip()
        import re
        if re.match(r'^[가-힣]{2,5}$', result):
            return result
        match = re.search(r'([가-힣]{2,5})', result)
        return match.group(1) if match else None
    except Exception as e:
        logger.error("[3행시] 영문→한국어 변환 실패: %s", e)
        return None


def _normalize_name_for_samhaengsi(name: str) -> str:
    """3행시용 이름 정규화. 띄어쓴 한글 이름을 한 덩어리로 합친다.

    - '이름 성' (외자 성, 예: '진성 김') → '김진성'
    - '성 이름' (외자 성, 예: '최 유나') → '최유나'
    이름이 외자인 경우는 드물어서, 둘 중 한쪽만 1글자면 그쪽을 성으로 간주한다.
    """
    import re
    parts = name.split()
    if len(parts) == 2 and all(re.match(r'^[가-힣]+$', p) for p in parts):
        first, second = parts
        if len(first) == 1 and len(second) >= 2:
            return f"{first}{second}"
        if len(second) == 1 and len(first) >= 2:
            return f"{second}{first}"
    return name


def _split_samhaengsi_into_blocks(text: str, name_len: int) -> list:
    """Gemini 응답 텍스트를 개별 3행시 블록으로 분리.

    1차: 빈 줄을 구분자로 사용.
    2차: 빈 줄 구분이 누락된 경우 name_len 줄씩 묶어 폴백.
    """
    blocks: list = []
    current: list = []
    for raw_line in text.strip().splitlines():
        line = raw_line.rstrip()
        if line.strip():
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))

    if len(blocks) < 6 and name_len >= 1:
        lines = [l.rstrip() for l in text.strip().splitlines() if l.strip()]
        if len(lines) >= name_len * 2 and len(lines) % name_len == 0:
            blocks = [
                "\n".join(lines[i:i + name_len])
                for i in range(0, len(lines), name_len)
            ]
    return blocks


def send_samhaengsi(name: str) -> None:
    """이름으로 3행시 6개를 생성해 Discord 일반 텍스트 메시지 6개로 분리 발송.

    임베드는 iOS Discord 앱에서 복사 동작이 막혀 있어서, 일반 메시지(content)로
    보낸다. 모바일 꾹 누르기 → 메시지 복사가 양 플랫폼 모두에서 동작한다.
    """
    original_name = name
    korean_name = name

    if _is_english_name(name):
        converted = _convert_english_to_korean(name)
        if converted:
            korean_name = converted
            logger.info("[3행시] 영문→한국어 변환: %s → %s", name, korean_name)
        else:
            logger.warning("[3행시] 영문→한국어 변환 실패, 원본 사용: %s", name)
    else:
        korean_name = _normalize_name_for_samhaengsi(name)

    poem = _generate_samhaengsi(korean_name)
    if not poem:
        logger.error("[3행시] 생성 실패: %s", korean_name)
        return

    if _is_english_name(original_name) and korean_name != original_name:
        header = f"오늘 3행시 송부드립니다.🎉\n{original_name} ({korean_name})님"
    else:
        header = "오늘 3행시 송부드립니다.🎉"

    blocks = _split_samhaengsi_into_blocks(poem, name_len=len(korean_name))
    sent_count = 0
    for block in blocks[:6]:
        body = f"{header}\n\n{block}"
        if _send_discord_webhook(body):
            sent_count += 1
    if sent_count:
        logger.info("[3행시] Discord 메시지 %d개 전송 OK: %s", sent_count, korean_name)
    else:
        logger.error("[3행시] Discord 메시지 전송 실패 또는 블록 0개: %s (blocks=%d)",
                     korean_name, len(blocks))


# =============================================================
# 재고 자동주문 알림
# =============================================================

def _format_price(price) -> str:
    """원 단위 가격을 '12,800원' 형식으로 포맷팅."""
    if price is None:
        return "가격 미확인"
    try:
        return f"{int(price):,}원"
    except (TypeError, ValueError):
        return str(price)


def _build_stock_message(result: dict) -> Optional[str]:
    """재고 자동주문 처리 결과를 Discord 메시지 문자열로 변환.

    입력: {"success": [...], "skipped": [...], "unmapped": [...], "failed": [...]}
    처리 항목이 하나도 없으면 None 반환 → 알림 생략.
    """
    success = result.get("success") or []
    skipped = result.get("skipped") or []
    unmapped = result.get("unmapped") or []
    failed = result.get("failed") or []

    if not (success or skipped or unmapped or failed):
        return None

    lines: list[str] = ["[재고 자동주문 완료]"]

    if success:
        lines.append("")
        lines.append(f"✅ 장바구니에 담음 ({len(success)}건):")
        for i, item in enumerate(success, 1):
            name = item.get("item_name", "?")
            qty = item.get("quantity", 1)
            unit_price = item.get("price")
            # price 필드는 단가. qty 곱해서 소계 표시 (qty>1일 때만).
            try:
                qty_int = int(qty) if qty else 1
                unit_int = int(unit_price) if unit_price is not None else 0
                subtotal = unit_int * qty_int if unit_int > 0 else 0
            except (TypeError, ValueError):
                qty_int, subtotal = 1, 0
            if qty_int > 1 and subtotal > 0:
                lines.append(f"{i}. {name} ({qty_int}개) - {_format_price(subtotal)} "
                             f"({_format_price(unit_int)} × {qty_int})")
            else:
                lines.append(f"{i}. {name} ({qty_int}개) - {_format_price(unit_price)}")

    if skipped:
        lines.append("")
        lines.append(f"⏭️ 자동 스킵 ({len(skipped)}건):")
        for item in skipped:
            name = item.get("item_name", "?")
            reason = item.get("reason", "사유 불명")
            lines.append(f"- {name}: {reason}")

    if unmapped:
        lines.append("")
        lines.append(f"⚠️ 매핑 필요 ({len(unmapped)}건):")
        for item in unmapped:
            name = item.get("item_name", "?")
            lines.append(f"- {name}: 매핑표에 없음")

    if failed:
        lines.append("")
        lines.append(f"❌ 처리 실패 ({len(failed)}건):")
        for item in failed:
            name = item.get("item_name", "?")
            reason = item.get("reason", "사유 불명")
            lines.append(f"- {name}: {reason}")

    # 총액 (성공 건 합계). price는 단가이므로 quantity 곱해서 합산.
    try:
        total = sum(int(s.get("price") or 0) * int(s.get("quantity") or 1)
                    for s in success)
        if total > 0:
            lines.append("")
            lines.append(f"총 {total:,}원")
            # GitHub Pages 리디렉트 페이지를 경유 — Discord는 HTTPS만 클릭 허용하므로
            # intent://나 coupang:// 같은 비표준 스킴을 직접 못 쓴다.
            # 정적 HTML에서 UA 감지 후 Android는 intent://, iOS는 coupang:// 스킴으로
            # 자동 점프시켜 쿠팡 앱을 연다. 앱 없으면 모바일 웹 카트로 폴백.
            # 페이지 소스: docs/coupang_cart_redirect.html (사용자 GitHub Pages에 배포)
            redirect_url = "https://burnhaam.github.io/toss-redirect/coupang.html"
            lines.append(f"[👉 쿠팡 장바구니에서 결제하기]({redirect_url})")
    except (TypeError, ValueError):
        pass

    return "\n".join(lines)


def send_stock_result(result: dict) -> None:
    """재고 자동주문 처리 결과를 Discord로 전송.

    처리 항목이 0건이면 발송하지 않는다. 동일 내용 메시지는 notify_state 기반으로
    영구 디듀프되어 재발송되지 않는다.
    """
    message = _build_stock_message(result)
    if not message:
        logger.info("[Stock] 처리 항목 0건 — 알림 생략")
        return

    msg_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    dedup_key = f"stock_result:{msg_hash}"

    if not _should_send(dedup_key, None):
        logger.info("[Stock] 결과 알림 스킵 (동일 내용 이미 발송됨)")
        return

    if _send_message(message):
        logger.info("[Stock] 결과 알림 전송 OK")
        _mark_sent(dedup_key)
    else:
        logger.error("[Stock] 결과 알림 전송 실패")


def send_stock_alert(message: str,
                     dedup_key: Optional[str] = None,
                     cooldown_hours: Optional[float] = None) -> None:
    """SMS/세션 만료/크래시 등 즉시 알림용.

    dedup_key가 주어지면 notify_state 기반 중복/쿨다운 체크 후 발송한다.
    cooldown_hours=None → 키당 영구 1회, cooldown_hours=N → N시간 이내 같은 키 스킵.
    """
    if not message:
        return

    if dedup_key and not _should_send(dedup_key, cooldown_hours):
        logger.info("[Stock] 알림 스킵 (key=%s, cooldown=%sh)",
                    dedup_key, cooldown_hours)
        return

    if _send_message(message):
        logger.info("[Stock] 긴급 알림 전송 OK")
        if dedup_key:
            _mark_sent(dedup_key)
    else:
        logger.error("[Stock] 긴급 알림 전송 실패")

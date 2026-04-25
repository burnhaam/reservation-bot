"""
카카오톡 '나에게 보내기' 알림 모듈.

카카오 REST API의 Memo API(기본 텍스트 템플릿)로 예약 생성/취소
결과를 본인 카카오톡으로 전송한다. 액세스 토큰이 만료되면
리프레시 토큰으로 자동 재발급하며, 성공 시 새 토큰을 .env에 저장한다.

호출 실패는 예외로 던지지 않고 로그만 남긴다.
"""

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from modules.env_loader import load_env, update_env_value

try:
    from google import genai
except ImportError:
    genai = None


logger = logging.getLogger(__name__)


# 카카오 OAuth / Memo API 엔드포인트
_KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
_KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

# 알림 중복/쿨다운 상태 저장 파일. 키 → 마지막 발송 ISO 시각.
_NOTIFY_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "notify_state.json"


# =============================================================
# Discord 웹훅 (이중화 알림)
# =============================================================

def _send_discord_webhook(text: str) -> bool:
    """Discord 채널에 웹훅으로 메시지 발송. 실패해도 예외 미전파.

    DISCORD_WEBHOOK_URL 미설정이면 조용히 스킵.
    카카오와 병행 호출되므로 한쪽 실패가 다른 쪽에 영향 주면 안 됨.
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


# =============================================================
# 토큰 관리
# =============================================================

def _refresh_kakao_access_token() -> Optional[str]:
    """카카오 리프레시 토큰으로 액세스 토큰 재발급.

    성공 시 새 access_token(및 갱신 시 새 refresh_token)을 .env에 저장하고 반환.
    실패 시 None.
    """
    env = load_env()
    rest_api_key = env.get("KAKAO_REST_API_KEY", "")
    client_secret = env.get("KAKAO_CLIENT_SECRET", "")
    refresh_token = env.get("KAKAO_REFRESH_TOKEN", "")

    if not (rest_api_key and refresh_token):
        logger.error("[Kakao] REST API KEY 또는 REFRESH_TOKEN 누락")
        return None

    data = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
        "client_secret": client_secret,
    }
    try:
        resp = requests.post(_KAKAO_TOKEN_URL, data=data, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as e:
        logger.error("[Kakao] 토큰 갱신 요청 실패: %s", e)
        return None
    except ValueError as e:
        logger.error("[Kakao] 토큰 응답 파싱 실패: %s", e)
        return None

    access_token = result.get("access_token")
    if not access_token:
        logger.error("[Kakao] 토큰 응답에 access_token 없음: %s", result)
        return None

    # 새 토큰 저장
    update_env_value("KAKAO_ACCESS_TOKEN", access_token)

    # 카카오는 갱신 주기가 도래해야 refresh_token을 내려주므로 있을 때만 저장
    new_refresh = result.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        update_env_value("KAKAO_REFRESH_TOKEN", new_refresh)

    return access_token


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

    _BLOCK_LINE = {
        "naver": "\n🚫 에어비앤비 해당 날짜 차단 완료",
        "airbnb": "\n⚠️ 네이버 플레이스 수동 차단 필요",
    }
    _UNBLOCK_LINE = {
        "naver": "\n🔓 에어비앤비 해당 날짜 차단 해제 완료",
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

    logger.error("[Kakao] 알 수 없는 action: %s", action)
    return None


# =============================================================
# 알림 전송
# =============================================================

def _post_memo(access_token: str, text: str) -> bool:
    """카카오 memo API 호출. 성공 여부 반환."""
    headers = {"Authorization": f"Bearer {access_token}"}
    template_object = {
        "object_type": "text",
        "text": text,
        # 링크는 필수 파라미터이므로 본 봇을 가리키는 placeholder 값을 넣음
        "link": {"web_url": "https://example.com", "mobile_web_url": "https://example.com"},
        "button_title": "확인",
    }
    data = {"template_object": json.dumps(template_object, ensure_ascii=False)}

    try:
        resp = requests.post(_KAKAO_MEMO_URL, headers=headers, data=data, timeout=30)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("[Kakao] 메시지 전송 실패: %s", e)
        return False


def send_notification(reservation: dict, action: str) -> None:
    """예약/취소 알림을 카카오톡 + 디스코드에 동시 전송.

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

    # Discord 병행 발송 (실패 무관)
    _send_discord_webhook(message)

    env = load_env()
    access_token = env.get("KAKAO_ACCESS_TOKEN", "")

    # 1) 기존 액세스 토큰으로 전송 시도
    if access_token and _post_memo(access_token, message):
        logger.info("[Kakao] 알림 전송 OK (action=%s)", action)
        if dedup_key:
            _mark_sent(dedup_key)
        return

    # 2) 실패 또는 토큰 없음 → 리프레시 후 재시도
    access_token = _refresh_kakao_access_token()
    if not access_token:
        if dedup_key:
            _mark_sent(dedup_key)  # Discord는 보냈을 수 있으니 중복 방지
        return

    if _post_memo(access_token, message):
        logger.info("[Kakao] 알림 전송 OK (토큰 갱신 후, action=%s)", action)
    else:
        logger.error("[Kakao] 토큰 갱신 후에도 전송 실패 (action=%s)", action)
    if dedup_key:
        _mark_sent(dedup_key)


def send_guests_update(guest_name: str, old_guests: int, new_guests: int) -> None:
    """인원수 변경 알림을 카카오톡으로 전송."""
    message = f"[인원 업데이트] {guest_name}님 예약 {old_guests}인 → {new_guests}인으로 변경"
    if _send_kakao_message(message):
        logger.info("[Kakao] 인원 업데이트 알림 OK: %s %d→%d", guest_name, old_guests, new_guests)
    else:
        logger.error("[Kakao] 인원 업데이트 알림 실패: %s", guest_name)


# =============================================================
# 3행시 생성 및 전송
# =============================================================

def _is_valid_samhaengsi(text: str, name: str) -> bool:
    """3행시 5개가 포함되어 있는지 검증. 첫 글자로 시작하는 행이 최소 5회."""
    first_char = name[0]
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    starts = sum(1 for l in lines if l.startswith(first_char))
    return starts >= 5


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
        f'"{name}" ({chars}) 이름으로 펜션에 오는 손님을 위한 3행시를 5개 지어줘.\n\n'
        "조건:\n"
        f"- 각 3행시는 반드시 {len(name)}행으로 구성\n"
        f"- 첫 번째 행은 '{name[0]}'로 시작, "
        + (f"두 번째 행은 '{name[1]}'로 시작, " if len(name) > 1 else "")
        + (f"세 번째 행은 '{name[2]}'로 시작\n" if len(name) > 2 else "\n")
        + "- 손님을 칭찬하는 유쾌하고 감동적인 내용\n"
        "- 각 3행시마다 이모지 1~2개 포함\n"
        "- 마지막 행에 이름을 한 번 더 불러주기\n"
        "- 5개 3행시 사이에 빈 줄로 구분\n"
        "- 3행시만 출력하고 번호, 제목, 설명은 절대 붙이지 마\n\n"
        "예시 (김소라):\n"
        "김 - 김치처럼 매콤한 매력,\n"
        "소 - 소녀의 순수하고,\n"
        "라 - 라디오 같은 따뜻한 목소리! 김소라님! 🎶\n\n"
        f'이제 "{name}" 3행시 5개를 지어줘:'
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


def _kakao_enabled() -> bool:
    """config.json의 notifications.kakao_enabled 플래그 조회.

    기본값 True (후방 호환). 설정파일 읽기 실패/키 없음도 True 반환.
    """
    try:
        from modules.config_loader import load_config
        cfg = load_config() or {}
        notif = cfg.get("notifications") or {}
        val = notif.get("kakao_enabled", True)
        return bool(val)
    except Exception:
        return True


def _send_kakao_message(text: str,
                        dedup_key: Optional[str] = None,
                        cooldown_hours: Optional[float] = None) -> bool:
    """카카오톡 + Discord 병행 발송. 토큰 만료 시 자동 갱신.

    config.notifications.kakao_enabled=false 면 카카오는 건너뜀 (Discord는 계속).
    토큰 갱신/OAuth 로직은 유지되어 언제든 재활성화 가능.

    dedup_key 지정 시 _should_send/_mark_sent로 중복 발송 차단.
    cooldown_hours=None 이면 영구 1회 (이미 보낸 키는 다시 안 보냄).
    """
    if dedup_key and not _should_send(dedup_key, cooldown_hours):
        logger.info("[Notify] 중복 차단 (dedup_key=%s)", dedup_key)
        return False

    # Discord는 항상 시도 (기본 채널)
    discord_ok = _send_discord_webhook(text)

    kakao_ok = False
    if _kakao_enabled():
        env = load_env()
        access_token = env.get("KAKAO_ACCESS_TOKEN", "")
        if access_token and _post_memo(access_token, text):
            kakao_ok = True
        else:
            access_token = _refresh_kakao_access_token()
            if access_token and _post_memo(access_token, text):
                kakao_ok = True
    else:
        logger.debug("[Notify] 카카오 비활성화 (config) — Discord 전용")

    if dedup_key:
        # 둘 중 하나라도 성공했으면 dedup 기록 (둘 다 실패면 다음 폴링에서 재시도)
        if discord_ok or kakao_ok:
            _mark_sent(dedup_key)
    return kakao_ok or discord_ok


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
    """3행시용 이름 정규화. '이름 성' → '성이름' (띄어쓰기 없이)."""
    import re
    parts = name.split()
    if len(parts) == 2 and all(re.match(r'^[가-힣]+$', p) for p in parts):
        given, family = parts
        if len(family) == 1 and len(given) >= 1:
            return f"{family}{given}"
    return name


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
    """재고 자동주문 처리 결과를 카카오톡용 메시지 문자열로 변환.

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
            price = _format_price(item.get("price"))
            lines.append(f"{i}. {name} ({qty}개) - {price}")

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

    # 총액 (성공 건 합계)
    try:
        total = sum(int(s.get("price") or 0) for s in success)
        if total > 0:
            lines.append("")
            lines.append(f"총 {total:,}원")
            # Discord 마크다운 하이퍼링크 (카카오에선 plain text 로 보임 — 현재 비활성)
            lines.append("[👉 쿠팡 장바구니에서 결제하기](https://cart.coupang.com/cartView.pang)")
    except (TypeError, ValueError):
        pass

    return "\n".join(lines)


def send_stock_result(result: dict) -> None:
    """재고 자동주문 처리 결과를 카카오톡으로 전송.

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

    if _send_kakao_message(message):
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

    if _send_kakao_message(message):
        logger.info("[Stock] 긴급 알림 전송 OK")
        if dedup_key:
            _mark_sent(dedup_key)
    else:
        logger.error("[Stock] 긴급 알림 전송 실패")


def send_samhaengsi(name: str) -> None:
    """이름으로 3행시를 생성하고 카카오톡으로 전송."""
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
        message = f"오늘 3행시 송부드립니다.🎉\n{original_name} ({korean_name})님\n\n{poem}"
    else:
        message = f"오늘 3행시 송부드립니다.🎉\n\n{poem}"

    if _send_kakao_message(message):
        logger.info("[3행시] 전송 OK: %s", korean_name)
    else:
        logger.error("[3행시] 카카오톡 전송 실패: %s", korean_name)

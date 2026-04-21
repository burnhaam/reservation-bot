"""
카카오톡 '나에게 보내기' 알림 모듈.

카카오 REST API의 Memo API(기본 텍스트 템플릿)로 예약 생성/취소
결과를 본인 카카오톡으로 전송한다. 액세스 토큰이 만료되면
리프레시 토큰으로 자동 재발급하며, 성공 시 새 토큰을 .env에 저장한다.

호출 실패는 예외로 던지지 않고 로그만 남긴다.
"""

import json
import logging
from datetime import date
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


# 플랫폼 코드 → 알림 메시지 표기용 한글 이름
_PLATFORM_DISPLAY = {
    "airbnb": "에어비앤비",
    "naver": "네이버",
}


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
    """예약/취소 알림을 카카오톡 나에게 보내기로 전송."""
    message = _build_message(reservation, action)
    if not message:
        return

    env = load_env()
    access_token = env.get("KAKAO_ACCESS_TOKEN", "")

    # 1) 기존 액세스 토큰으로 전송 시도
    if access_token and _post_memo(access_token, message):
        logger.info("[Kakao] 알림 전송 OK (action=%s)", action)
        return

    # 2) 실패 또는 토큰 없음 → 리프레시 후 재시도
    access_token = _refresh_kakao_access_token()
    if not access_token:
        return

    if _post_memo(access_token, message):
        logger.info("[Kakao] 알림 전송 OK (토큰 갱신 후, action=%s)", action)
    else:
        logger.error("[Kakao] 토큰 갱신 후에도 전송 실패 (action=%s)", action)


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


def _call_gemini(client, prompt: str) -> Optional[str]:
    """Gemini API 단일 호출."""
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            config={
                "system_instruction": "당신은 재치있고 감동적인 3행시 작가입니다.",
                "max_output_tokens": 1500,
                "temperature": 0.9,
                "thinking_config": {"thinking_budget": 0},
            },
            contents=prompt,
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

    client = genai.Client(api_key=api_key)

    result = _call_gemini(client, prompt)
    if result and _is_valid_samhaengsi(result, name):
        return result

    logger.warning("[3행시] 불완전한 응답, 재시도: %s", result[:50] if result else "None")
    retry = _call_gemini(client, prompt)
    if retry and _is_valid_samhaengsi(retry, name):
        return retry

    logger.error("[3행시] 재시도 후에도 불완전: %s", retry[:50] if retry else "None")
    return retry or result


def _send_kakao_message(text: str) -> bool:
    """카카오톡 나에게 보내기로 텍스트 전송. 토큰 만료 시 자동 갱신."""
    env = load_env()
    access_token = env.get("KAKAO_ACCESS_TOKEN", "")

    if access_token and _post_memo(access_token, text):
        return True

    access_token = _refresh_kakao_access_token()
    if access_token and _post_memo(access_token, text):
        return True

    return False


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
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
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

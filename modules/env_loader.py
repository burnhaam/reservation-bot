"""
.env 환경변수 로드 모듈.

API 키, 리프레시 토큰, iCal URL 등 민감 정보를 코드와 분리하여
운영 환경에서 안전하게 주입하기 위한 모듈이다.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


# 프로젝트 루트 기준 .env 경로
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


# 이 모듈이 참조하는 환경변수 키 목록
ENV_KEYS = [
    "AIRBNB_ICAL_URL",
    "KAKAO_REST_API_KEY",
    "KAKAO_CLIENT_SECRET",
    "KAKAO_ACCESS_TOKEN",
    "KAKAO_REFRESH_TOKEN",
    "NAVER_PLACE_ID",
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    # Discord 알림/봇
    "DISCORD_WEBHOOK_URL",
    "DISCORD_BOT_TOKEN",
    "DISCORD_CHANNEL_ID",
]


def load_env(path: Path = ENV_PATH) -> dict:
    """.env 파일을 로드해 필요한 키만 딕셔너리로 반환한다."""
    # override=True: update_env_value()로 갱신된 값이 즉시 반영되도록 함
    load_dotenv(dotenv_path=path, override=True)
    return {key: os.getenv(key, "") for key in ENV_KEYS}


def update_env_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    """.env 파일에서 특정 키의 값을 갱신 (없으면 줄 추가)하고 os.environ에도 반영.

    토큰 자동 갱신 시 호출되어 다음 프로세스 실행에서도 최신 토큰을 쓸 수 있게 한다.
    """
    lines: list[str] = []
    found = False

    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for raw in f.readlines():
                stripped = raw.lstrip()
                # 주석/빈 줄은 그대로 보존
                if stripped.startswith("#") or not stripped.strip():
                    lines.append(raw)
                    continue
                if "=" in stripped and stripped.split("=", 1)[0].strip() == key:
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(raw)

    if not found:
        # 파일 끝에 개행이 없으면 보정 후 추가
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # 현재 프로세스에서도 즉시 반영
    os.environ[key] = value

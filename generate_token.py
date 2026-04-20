"""
Gmail API OAuth 토큰 발급 스크립트

사전 준비:
  1. Google Cloud Console에서 OAuth 2.0 클라이언트 ID 생성
  2. credentials.json을 이 스크립트와 같은 폴더에 배치

사용법:
  python generate_token.py
"""

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def main():
    print("=" * 50)
    print(" Gmail API 토큰 발급")
    print("=" * 50)

    if not CREDENTIALS_PATH.exists():
        print(f"[ERROR] {CREDENTIALS_PATH} 파일이 없습니다.")
        return

    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        print("[OK] 기존 token.json이 유효합니다.")
        return

    if creds and creds.expired and creds.refresh_token:
        print("[INFO] 토큰 만료, 갱신 중...")
        creds.refresh(Request())
    else:
        print("[INFO] 브라우저에서 Google 계정 로그인 후 동의해주세요.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), SCOPES
        )
        creds = flow.run_local_server(port=9997)

    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"[OK] token.json 저장 완료: {TOKEN_PATH}")
    print("=" * 50)


if __name__ == "__main__":
    main()

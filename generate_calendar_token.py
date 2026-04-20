"""
구글 캘린더 API OAuth 토큰 발급 스크립트

소유 일정관리 / 소캠스 예약일정이 있는 구글 계정으로 인증.
생성 파일: token_calendar.json (calendar 읽기/쓰기)

사용법:
  python generate_calendar_token.py
"""

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token_calendar.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main():
    print("=" * 50)
    print(" 구글 캘린더 API 토큰 발급 (캘린더 계정)")
    print("=" * 50)

    if not CREDENTIALS_PATH.exists():
        print(f"[ERROR] {CREDENTIALS_PATH} 파일이 없습니다.")
        return

    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        print("[OK] 기존 token_calendar.json이 유효합니다.")
        return

    if creds and creds.expired and creds.refresh_token:
        print("[INFO] 토큰 만료, 갱신 중...")
        creds.refresh(Request())
    else:
        print("[INFO] 브라우저에서 캘린더 계정으로 로그인 후 동의해주세요.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), SCOPES
        )
        creds = flow.run_local_server(port=9996)

    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"[OK] {TOKEN_PATH} 저장 완료")
    print("=" * 50)


if __name__ == "__main__":
    main()

"""
네이버 캘린더 API OAuth 토큰 발급 스크립트

사전 준비:
  1. https://developers.naver.com 에서 애플리케이션 등록
  2. [API 권한] > '캘린더' 추가
  3. [로그인 오픈 API 서비스 환경] > Callback URL에 http://localhost:9998/oauth 등록
  4. .env에 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 입력

사용법:
  python naver_token.py
"""

import webbrowser
import http.server
import urllib.parse
import requests
import sys
import os
import re
from dotenv import load_dotenv

load_dotenv()

PORT = 9998
REDIRECT_URI = f"http://localhost:{PORT}/oauth"
CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

if not CLIENT_ID or not CLIENT_SECRET:
    print("[ERROR] .env에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 설정되지 않았습니다.")
    sys.exit(1)


class OAuthHandler(http.server.BaseHTTPRequestHandler):
    auth_code = None
    state = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            OAuthHandler.auth_code = params["code"][0]
            OAuthHandler.state = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h2>인가 코드 수신 완료!</h2>"
                "<p>이 창을 닫고 터미널을 확인하세요.</p></body></html>".encode()
            )
        else:
            error = params.get("error_description", ["알 수 없는 오류"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body><h2>오류: {error}</h2></body></html>".encode())

    def log_message(self, format, *args):
        pass


def get_auth_code():
    import secrets
    state = secrets.token_urlsafe(16)

    auth_url = (
        f"https://nid.naver.com/oauth2.0/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&response_type=code"
        f"&state={state}"
        f"&scope=openid%20calendar"
        f"&prompt=consent"
    )

    server = http.server.HTTPServer(("localhost", PORT), OAuthHandler)

    print(f"[DEBUG] auth_url = {auth_url}")
    print(f"[INFO] 브라우저에서 네이버 로그인 후 동의해주세요.")
    print(f"[INFO] 대기 중... (http://localhost:{PORT})")
    webbrowser.open(auth_url)

    server.handle_request()
    server.server_close()

    if not OAuthHandler.auth_code:
        print("[ERROR] 인가 코드를 받지 못했습니다.")
        sys.exit(1)

    return OAuthHandler.auth_code


def get_tokens(auth_code):
    resp = requests.post(
        "https://nid.naver.com/oauth2.0/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": auth_code,
        },
    )

    data = resp.json()

    if "error" in data:
        print(f"[ERROR] 토큰 발급 실패: {data.get('error_description', data['error'])}")
        sys.exit(1)

    return data["access_token"], data["refresh_token"]


def save_to_env(refresh_token):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(
        r"^NAVER_REFRESH_TOKEN=.*$",
        f"NAVER_REFRESH_TOKEN={refresh_token}",
        content,
        flags=re.MULTILINE,
    )

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)


if __name__ == "__main__":
    print("=" * 50)
    print(" 네이버 캘린더 API 토큰 발급")
    print("=" * 50)

    code = get_auth_code()
    print(f"[OK] 인가 코드 수신 완료")

    access_token, refresh_token = get_tokens(code)
    print(f"[OK] 토큰 발급 완료")

    save_to_env(refresh_token)
    print(f"[OK] .env에 NAVER_REFRESH_TOKEN 저장 완료")

    print()
    print(f"  ACCESS_TOKEN  = {access_token[:20]}...")
    print(f"  REFRESH_TOKEN = {refresh_token[:20]}...")
    print()
    print("=" * 50)

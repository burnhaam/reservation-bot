"""
카카오 '나에게 보내기' 토큰 발급 스크립트

사전 준비:
  1. https://developers.kakao.com 에서 앱 생성
  2. [카카오 로그인] 활성화
  3. [동의항목] > '카카오톡 메시지 전송' 동의 설정
  4. [카카오 로그인] > Redirect URI에 http://localhost:9999/oauth 등록
  5. .env에 KAKAO_REST_API_KEY 입력

사용법:
  python kakao_token.py
"""

import webbrowser
import http.server
import urllib.parse
import requests
import sys
import os
import re
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH)

PORT = 9999
REDIRECT_URI = f"http://localhost:{PORT}/oauth"
REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
CLIENT_SECRET = os.getenv("KAKAO_CLIENT_SECRET", "")

if not REST_API_KEY:
    print("[ERROR] .env에 KAKAO_REST_API_KEY가 설정되지 않았습니다.")
    sys.exit(1)


class OAuthHandler(http.server.BaseHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            OAuthHandler.auth_code = params["code"][0]
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
    auth_url = (
        f"https://kauth.kakao.com/oauth/authorize"
        f"?client_id={REST_API_KEY}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=talk_message"
    )

    server = http.server.HTTPServer(("localhost", PORT), OAuthHandler)

    print(f"[INFO] 브라우저에서 카카오 로그인 후 동의해주세요.")
    print(f"[INFO] 대기 중... (http://localhost:{PORT})")
    webbrowser.open(auth_url)

    server.handle_request()
    server.server_close()

    if not OAuthHandler.auth_code:
        print("[ERROR] 인가 코드를 받지 못했습니다.")
        sys.exit(1)

    return OAuthHandler.auth_code


def get_tokens(auth_code):
    payload = {
        "grant_type": "authorization_code",
        "client_id": REST_API_KEY,
        "redirect_uri": REDIRECT_URI,
        "code": auth_code,
        "client_secret": CLIENT_SECRET,
    }

    print("\n[DEBUG] === 토큰 요청 파라미터 ===")
    for k, v in payload.items():
        print(f"  {k} = {v}")

    resp = requests.post("https://kauth.kakao.com/oauth/token", data=payload)

    print(f"\n[DEBUG] === 응답 ===")
    print(f"  status_code = {resp.status_code}")
    print(f"  headers =")
    for k, v in resp.headers.items():
        print(f"    {k}: {v}")
    print(f"  body = {resp.text}")

    if resp.status_code != 200:
        print(f"\n[ERROR] 토큰 발급 실패")
        sys.exit(1)

    data = resp.json()
    return data["access_token"], data["refresh_token"]


def save_to_env(access_token, refresh_token):
    with open(ENV_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    content = re.sub(
        r"^KAKAO_ACCESS_TOKEN=.*$",
        f"KAKAO_ACCESS_TOKEN={access_token}",
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r"^KAKAO_REFRESH_TOKEN=.*$",
        f"KAKAO_REFRESH_TOKEN={refresh_token}",
        content,
        flags=re.MULTILINE,
    )

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def verify_send(access_token):
    resp = requests.get(
        "https://kapi.kakao.com/v1/api/talk/friends",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return resp.status_code == 200


if __name__ == "__main__":
    print("=" * 50)
    print(" 카카오 '나에게 보내기' 토큰 발급")
    print("=" * 50)

    code = get_auth_code()
    print(f"[OK] 인가 코드 수신 완료")

    access_token, refresh_token = get_tokens(code)
    print(f"[OK] 토큰 발급 완료")

    save_to_env(access_token, refresh_token)
    print(f"[OK] .env 저장 완료")

    print()
    print(f"  ACCESS_TOKEN  = {access_token[:20]}...")
    print(f"  REFRESH_TOKEN = {refresh_token[:20]}...")
    print()
    print("=" * 50)

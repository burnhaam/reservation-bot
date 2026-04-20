"""
blocked.ics HTTP 서버

ical 폴더를 포트 8080으로 서빙한다.
에어비앤비 등 외부 서비스에서 http://<IP>:8080/blocked.ics 로 구독 가능.
"""

import os
import sys
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8080
ICAL_DIR = Path(__file__).resolve().parent / "ical"


class ICalHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.endswith(".ics"):
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, format, *args):
        print(f"[iCal] {self.address_string()} - {format % args}")


def main():
    if not ICAL_DIR.exists():
        print(f"[ERROR] ical 폴더가 없습니다: {ICAL_DIR}")
        sys.exit(1)

    handler = partial(ICalHandler, directory=str(ICAL_DIR))
    server = HTTPServer(("0.0.0.0", PORT), handler)

    print(f"[OK] iCal 서버 시작: http://localhost:{PORT}/blocked.ics")
    print(f"[OK] 서빙 폴더: {ICAL_DIR}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 서버 종료")
        server.server_close()


if __name__ == "__main__":
    main()

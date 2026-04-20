"""
프로세스 감시 스크립트.

webhook_server.py(5000), serve_ical.py(8080)가 응답하는지 확인하고
꺼져있으면 자동 재시작 + 카카오톡 알림.

사용법:
  python watchdog.py          # 1회 점검
  Task Scheduler로 5분마다 실행
"""

import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

PYTHON = sys.executable
PYTHONW = str(Path(PYTHON).parent / "pythonw.exe")

formatter = logging.Formatter(
    fmt="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("watchdog")
logger.setLevel(logging.INFO)
logger.handlers.clear()

fh = logging.FileHandler(LOG_DIR / "watchdog.log", encoding="utf-8")
fh.setFormatter(formatter)
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(formatter)
logger.addHandler(ch)

TARGETS = [
    {
        "name": "webhook_server",
        "script": "webhook_server.py",
        "port": 5000,
        "health": "http://localhost:5000/health",
    },
    {
        "name": "serve_ical",
        "script": "serve_ical.py",
        "port": 8080,
        "health": "http://localhost:8080/blocked.ics",
    },
]


def _is_alive(url: str) -> bool:
    try:
        resp = requests.get(url, timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def _start_process(script: str) -> bool:
    script_path = PROJECT_ROOT / script
    if not script_path.exists():
        logger.error("[Watchdog] 스크립트 없음: %s", script_path)
        return False

    try:
        subprocess.Popen(
            [PYTHONW, str(script_path)],
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        logger.info("[Watchdog] 프로세스 시작: %s", script)
        return True
    except Exception as e:
        logger.error("[Watchdog] 프로세스 시작 실패 (%s): %s", script, e)
        return False


def _send_kakao_alert(message: str) -> None:
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from modules.notifier import _send_kakao_message
        _send_kakao_message(message)
    except Exception as e:
        logger.error("[Watchdog] 카카오 알림 실패: %s", e)


def main():
    for target in TARGETS:
        name = target["name"]
        if _is_alive(target["health"]):
            logger.info("[Watchdog] %s OK (port %d)", name, target["port"])
            continue

        logger.warning("[Watchdog] %s 응답 없음 — 재시작 시도", name)

        if _start_process(target["script"]):
            import time
            time.sleep(3)

            if _is_alive(target["health"]):
                msg = f"[시스템 알림] {name} 재시작됨 ✅"
                logger.info("[Watchdog] %s 재시작 성공", name)
            else:
                msg = f"[시스템 알림] {name} 재시작 실패 ❌"
                logger.error("[Watchdog] %s 재시작 후에도 응답 없음", name)

            _send_kakao_alert(msg)
        else:
            _send_kakao_alert(f"[시스템 알림] {name} 시작 실패 ❌")


if __name__ == "__main__":
    main()

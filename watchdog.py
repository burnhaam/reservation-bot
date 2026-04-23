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
from datetime import date, datetime
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


# =============================================================
# 파이프라인 헬스체크 (main.py --check 및 watchdog.py에서 공용)
# =============================================================

def check_pipeline_health(max_age_hours: int = 3) -> tuple[bool, str]:
    """data/last_success.txt의 최근 성공 기록으로 파이프라인 건강도 판단.

    main.run_pipeline() 정상 완료 시 이 파일이 갱신된다. cron/Task Scheduler에서
    1시간마다 실행되므로, 3시간 이상 갱신이 없으면 파이프라인이 멈춘 것으로 간주.

    Returns:
        (healthy: bool, message: str) — 메시지는 콘솔/알림에 그대로 사용 가능.
    """
    flag_path = PROJECT_ROOT / "data" / "last_success.txt"
    if not flag_path.exists():
        return False, "파이프라인이 아직 1회도 성공하지 못함"
    try:
        last_time = datetime.fromisoformat(
            flag_path.read_text(encoding="utf-8").strip()
        )
    except Exception as e:
        return False, f"last_success.txt 파싱 실패: {e}"
    age = datetime.now() - last_time
    if age.total_seconds() > max_age_hours * 3600:
        return False, (
            f"마지막 성공 {age.total_seconds()/3600:.1f}시간 전 — 점검 필요"
        )
    return True, f"정상 (마지막 성공 {age.total_seconds()/60:.0f}분 전)"


_FAIL_COUNT_FILE = LOG_DIR / "watchdog_fails.json"


def _load_fail_counts() -> dict:
    import json
    if _FAIL_COUNT_FILE.exists():
        try:
            return json.loads(_FAIL_COUNT_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_fail_counts(counts: dict) -> None:
    import json
    _FAIL_COUNT_FILE.write_text(json.dumps(counts))


def main():
    fail_counts = _load_fail_counts()
    # 5분 간격 × 288 = 24시간
    ALERT_THRESHOLD = 288

    for target in TARGETS:
        name = target["name"]
        if _is_alive(target["health"]):
            logger.info("[Watchdog] %s OK (port %d)", name, target["port"])
            fail_counts[name] = 0
            continue

        fail_counts[name] = fail_counts.get(name, 0) + 1
        logger.warning("[Watchdog] %s 응답 없음 (연속 %d회) — 재시작 시도", name, fail_counts[name])

        if _start_process(target["script"]):
            import time
            time.sleep(3)

            if _is_alive(target["health"]):
                logger.info("[Watchdog] %s 재시작 성공", name)
                fail_counts[name] = 0
            else:
                logger.error("[Watchdog] %s 재시작 후에도 응답 없음", name)

        if fail_counts[name] == ALERT_THRESHOLD:
            _send_kakao_alert(f"[서버 장애] {name} 24시간 이상 복구 불가, 수동 확인 필요")

    _save_fail_counts(fail_counts)


if __name__ == "__main__":
    main()

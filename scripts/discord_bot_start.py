"""
Discord 봇 실행 런처.

로그온 시 Task Scheduler에 의해 자동 실행. pythonw.exe로 콘솔 없이 백그라운드.
봇은 프로세스가 살아있는 동안 Discord Gateway 연결 유지 + 슬래시 커맨드 수신.

재시작/크래시 시: Task Scheduler가 다시 띄우지 않으므로 수동 재실행 필요.
(discord.py 내부 재연결은 자동으로 처리됨)

수동 실행:
  python scripts/discord_bot_start.py
"""
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 파일 + 콘솔 로그 (pythonw에선 콘솔 무의미하나 개발용 python도 지원)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "discord_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    try:
        from modules.discord_bot import run
        logger.info("[Discord] 봇 시작")
        run()
    except KeyboardInterrupt:
        logger.info("[Discord] 사용자 중단 (Ctrl+C)")
    except Exception:
        logger.exception("[Discord] 봇 실행 중 치명 오류")
        raise

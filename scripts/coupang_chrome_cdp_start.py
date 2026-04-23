"""
실제 Chrome을 CDP(remote-debugging-port=9222) 모드로 띄우는 런처.

modules/coupang_orderer.init_browser()가 이 Chrome에 attach해서
실제 Chrome의 디바이스 지문 + 쿠키로 쿠팡 자동화를 수행한다.
이래야 Akamai의 _abck 쿠키 디바이스 매칭/behavioral challenge를 통과할 수 있다.

사용 절차:
  1. python scripts/coupang_chrome_cdp_start.py
  2. 뜬 Chrome 창에서 쿠팡(www.coupang.com) 로그인 — 최초 1회
  3. Chrome 창은 켜둔 상태로 자동화 실행 (python main.py 등)
  4. 자동화는 Chrome에 attach해서 새 탭으로 작업 → 끝나면 그 탭만 닫음

주의:
  - 이 Chrome은 사용자 일상 Chrome과 분리된 별도 프로필 (chrome_cdp_profile)
  - 자동화가 동작하는 동안 이 Chrome 창을 닫지 마세요
  - 재부팅/Chrome 종료 후엔 이 스크립트를 다시 실행해야 함
"""
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

CDP_PORT = 9222
USER_DATA_DIR = Path(os.environ.get("USERPROFILE", "")) / "chrome_cdp_profile"

# Chrome 실행 파일 후보 경로
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
]


def find_chrome() -> str | None:
    for p in CHROME_PATHS:
        if p and Path(p).exists():
            return p
    found = shutil.which("chrome.exe") or shutil.which("chrome")
    return found


def is_port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    print("=" * 60)
    print(" Chrome CDP 런처 (port 9222)")
    print("=" * 60)

    if is_port_open(CDP_PORT):
        print(f"\n[정보] 포트 {CDP_PORT}에 이미 응답하는 Chrome이 있습니다.")
        print("       추가 작업 없이 바로 자동화를 실행할 수 있습니다.")
        print("       (다른 Chrome인지 확인하려면: http://localhost:9222/json/version)")
        return

    chrome = find_chrome()
    if not chrome:
        print("\n[오류] chrome.exe를 찾을 수 없습니다. 다음 경로를 확인하세요:")
        for p in CHROME_PATHS:
            print(f"   {p}")
        sys.exit(1)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # 시각적 방해 최소화 (작업표시줄 아이콘만 보임).
        # headless는 Akamai가 차단하므로 안 됨 — minimized로 우회.
        "--start-minimized",
        "--window-position=10000,10000",  # 화면 밖에 위치 (혹시 복원돼도 안 보임)
        "https://www.coupang.com",
    ]

    print(f"\n Chrome    : {chrome}")
    print(f" 프로필     : {USER_DATA_DIR}")
    print(f" CDP 포트  : {CDP_PORT}")
    print()
    print("Chrome 창이 뜨면:")
    print("  1) 최초 실행이라면 쿠팡에 로그인하세요")
    print("  2) 자동화 실행 동안 이 Chrome은 켜둔 상태로 두세요")
    print("  3) 일상 Chrome과는 별도 프로필이라 동시 실행 가능합니다")
    print()

    # DETACHED_PROCESS=0x08 + CREATE_NEW_PROCESS_GROUP=0x200 → 이 콘솔 닫혀도 Chrome 살아있음
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

    try:
        subprocess.Popen(
            args,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as e:
        print(f"[오류] Chrome 실행 실패: {e}")
        sys.exit(1)

    # 포트 활성화 대기 (최대 20초)
    print("[대기] CDP 포트 활성화 중...", end="", flush=True)
    for _ in range(40):
        if is_port_open(CDP_PORT):
            print(" OK")
            print(f"\n CDP 포트 {CDP_PORT} 활성. 다음 명령으로 자동화 동작 확인:")
            print("   python scripts/coupang_dryrun.py")
            return
        time.sleep(0.5)
        print(".", end="", flush=True)

    print()
    print(f"[경고] {CDP_PORT} 활성화를 확인하지 못했습니다.")
    print("       Chrome 창이 떴는지, 백신/방화벽이 막지 않는지 확인하세요.")


if __name__ == "__main__":
    main()

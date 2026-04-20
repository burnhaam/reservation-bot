# 숙박업 예약 자동화 (reservation-bot)

네이버 플레이스 예약과 에어비앤비 예약을 주기적으로 감지해서
네이버 캘린더 2곳에 일정을 자동 등록/삭제하고, 상대 플랫폼의
해당 날짜를 자동으로 차단/해제하며, 처리 결과를 카카오톡으로
나에게 보내기로 알려주는 자동화 도구입니다.

---

## 디렉터리 구조

```
reservation-bot/
├── main.py              # 진입점 (전체 파이프라인)
├── config.json          # 운영값 (담당자명, 폴링 주기, 캘린더명 등)
├── .env                 # API 키·토큰 (커밋 금지)
├── requirements.txt
├── db/
│   └── reservations.db  # SQLite — 처리된 예약 ID 저장
├── ical/
│   └── blocked.ics      # 에어비앤비 차단용 iCal 파일
├── logs/
│   └── YYYY-MM-DD.log   # 실행 로그 (자동 생성)
└── modules/
    ├── config_loader.py
    ├── env_loader.py
    ├── db.py
    ├── detector.py      # 1단계: 예약 감지
    ├── calendar.py      # 2단계: 네이버 캘린더 연동
    ├── notifier.py      # 3단계: 카카오 알림
    └── blocker.py       # 4단계: 플랫폼 간 날짜 차단/해제
```

---

## 1. 설치

Python 3.10 이상을 권장합니다.

```bash
# 1) 가상환경 생성 (선택)
python -m venv .venv
source .venv/bin/activate        # Mac/Linux
# .venv\Scripts\activate          # Windows

# 2) 의존성 설치
pip install -r requirements.txt

# 3) (Playwright 방식으로 네이버를 차단할 경우만)
pip install playwright
playwright install chromium
```

---

## 2. `.env` 작성

`.env` 파일에 다음 값을 채워주세요.

| 키 | 설명 |
|---|---|
| `NAVER_CLIENT_ID` | 네이버 개발자센터 앱의 Client ID |
| `NAVER_CLIENT_SECRET` | 네이버 개발자센터 앱의 Client Secret |
| `NAVER_REFRESH_TOKEN` | 최초 OAuth 인증으로 얻은 리프레시 토큰 |
| `AIRBNB_ICAL_URL` | 에어비앤비 숙소 관리 → 캘린더 → 캘린더 내보내기 URL |
| `NAVER_PLACE_ID` | 네이버 스마트플레이스 업체 ID |
| `KAKAO_REST_API_KEY` | 카카오 개발자 콘솔 앱의 REST API 키 |
| `KAKAO_ACCESS_TOKEN` | 카카오 로그인 후 얻은 액세스 토큰 |
| `KAKAO_REFRESH_TOKEN` | 위와 함께 얻은 리프레시 토큰 |

토큰은 실행 중 자동 갱신되어 `.env` 파일에 다시 저장됩니다.

네이버 플레이스 예약 감지는 Gmail 메일 파싱 방식을 사용하므로,
Gmail API 사용 설정 후 프로젝트 루트에 `token.json`
(scope: `gmail.readonly`)을 준비해주세요.

---

## 3. `config.json` 설정

```json
{
  "staff_name": "최자임님",
  "polling_interval_minutes": 60,
  "naver_owner_calendar": "소유 일정관리",
  "naver_staff_calendar": "소캠스 예약일정",
  "platform_prefix": { "naver": "네", "airbnb": "에" },
  "blocked_ical_path": "ical/blocked.ics",
  "naver_block_method": "api"
}
```

- `naver_block_method`: `"api"` 또는 `"playwright"` 중 선택
- 캘린더명은 네이버 캘린더에서 사용하는 실제 라벨과 일치해야 합니다.

---

## 4. 실행

```bash
python main.py              # 파이프라인 1회 실행 (cron/스케줄러 용)
python main.py --check      # 설정 점검만 수행
python main.py --install    # OS별 자동 실행 등록 명령 출력
```

처음에는 `--check`로 모든 설정이 정상인지 확인하세요.

---

## 5. 자동 실행 등록

`python main.py --install` 을 실행하면 현재 Python 경로와 프로젝트
절대 경로가 반영된 등록 명령을 그대로 출력해줍니다. 그대로 복사하여
사용하세요.

### Mac / Linux (cron)

```bash
crontab -e
# 아래 한 줄 추가 (1시간마다 실행 예시)
0 * * * * /usr/bin/python3 /absolute/path/reservation-bot/main.py >> /absolute/path/reservation-bot/logs/cron.log 2>&1
```

### Windows (Task Scheduler)

PowerShell(관리자 권한)에서:

```powershell
$action  = New-ScheduledTaskAction -Execute 'C:\Python310\python.exe' -Argument '"C:\path\reservation-bot\main.py"'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 60)
Register-ScheduledTask -TaskName 'ReservationBot' -Action $action -Trigger $trigger -Description '숙박업 예약 자동화' -Force
```

---

## 6. 로그

- `logs/YYYY-MM-DD.log` — 파이프라인 실행 로그 (매일 파일 분리)
- `logs/cron.log` — cron/Task Scheduler stdout/stderr (설치 시 지정)
- 형식: `[YYYY-MM-DD HH:MM:SS] [LEVEL] 메시지`
- 개별 예약 처리 실패 시 스택트레이스가 함께 기록됩니다.

---

## 7. 문제 해결

| 증상 | 확인 사항 |
|---|---|
| `python main.py --check`에서 토큰 항목 FAIL | `.env`의 CLIENT_ID/SECRET/REFRESH_TOKEN 확인 |
| 한글이 `???`로 깨짐 | Windows 기본 콘솔 대신 Windows Terminal 사용 권장 |
| Gmail 메일이 조회되지 않음 | `token.json` 존재 여부 + scope(`gmail.readonly`) 확인 |
| `blocked.ics`가 에어비앤비에 반영되지 않음 | 파일을 외부에서 접근 가능한 URL로 노출했는지 확인 |
| Playwright 방식이 실패 | 모듈 주석의 `TODO: selector` 위치에 실제 셀렉터 입력 필요 |

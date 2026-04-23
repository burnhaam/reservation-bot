# PRD: 재고 자동주문 시스템 (Stock Auto-Order)

> **버전**: v2.0 (실구현은 v2.1로 진화 — 아래 "변경 이력" 참조)
> **작성일**: 2026-04-20  
> **최종 반영**: 2026-04-23
> **변경**: 사장님 승인 단계 제거 → 자동 차단 + 카카오 결과 알림 방식  
> **기반 레포**: `burnhaam/reservation-bot` (기존 모듈 70% 재활용)  
> **개발 기간 추정**: 약 3일 (코딩 1.5~2일 + 셋업/테스트 1일)

---

## 0. 변경 이력 (v2 → 실구현)

아래는 PRD v2.0 작성 이후 실제 구현 단계에서 생긴 변경/추가 사항. **이 섹션을 근거로 이하 본문의 해당 부분을 해석**하면 됨 (본문은 역사 보존 목적으로 v2.0 상태 유지).

### 0.1 AI 모델 교체: Claude Haiku → Gemini 2.5 Flash
- **이유**: 비용 (Gemini 무료 할당량 활용), Anthropic 쿼터 이슈 회피
- **영향 범위**: `modules/stock_parser.py`, `config.json`의 `stock.gemini_model`, `.env`의 `GEMINI_API_KEY`
- **기능 동등성**: `response_schema`로 JSON 배열 출력 강제, 30초 timeout, 배치 파싱 폴백 구조 동일
- **비고**: PRD 본문의 "Claude API" / "claude-haiku-4-5-20251001" 문구는 모두 "Gemini 2.5 Flash"로 읽으면 됨

### 0.2 재고 수량 기반 부족분 계산 추가
- **신규 필드**: 매핑표의 `max_stock` + Gemini 추출 결과의 `current_stock`
- **동작**: `max_stock > 0` 이고 `current_stock`이 추출되면, `needed = max_stock - current_stock` 만큼만 주문. 재고 충분 시 skip.
- **예시**: 장작 max=6, 메모에 "장작 2박스" → 4박스 주문
- **영향 범위**: `modules/stock_parser.py`의 JSON 스키마, `main.py:run_stock_pipeline` 의 주문 분류 로직

### 0.3 쿠팡 Akamai 우회 — CDP Chrome 모드
PRD v2에서 "Playwright로 쿠팡 조작" 단순 명시된 부분이 실제로는 Akamai의 디바이스 지문 검증 때문에 다단계 접근이 필요했음:
- **초기 시도**: Playwright 번들 Chromium → Akamai 차단 (Access Denied)
- **중간 시도**: patchright (Playwright fork) → blob worker CSP 차단으로 로그인 실패
- **최종 채택**: **사용자 실제 Chrome을 `--remote-debugging-port=9222`로 띄우고 CDP attach**. 별도 프로필 (`%USERPROFILE%\chrome_cdp_profile`).
- **쿠키 주입 경로**: DevTools에서 쿠키 JSON export → `scripts/coupang_convert_cookies.py`로 Playwright storage_state 형식 변환 → `data/coupang_session.json` 저장 → `init_browser()`에서 로드
- **안전망**: CDP 포트 미응답 시 자동주문 스킵 + 카카오 "수동주문 필요" 알림 (영구 1회)
- **관련 스크립트**:
  - `scripts/coupang_chrome_cdp_start.py` — CDP 모드 Chrome 런처
  - `scripts/coupang_convert_cookies.py` — DevTools 쿠키 → storage_state 변환
  - `scripts/coupang_dryrun.py` — 6단계 E2E 점검 (클릭 없음)
  - `scripts/coupang_mini_live_test.py` — 1품목 라이브 카트 담기
  - `scripts/coupang_replay_memo.py` — 과거 메모 재생 테스트

### 0.4 실행 트리거 변경: 오후 2시 고정 → 메모 변경 즉시
- **이전**: 하루 1회 오후 2시(13:30~14:30 윈도우)에만 재고 파이프라인 실행
- **현재**: 매 폴링(5분)마다 실행. `stock_detector`가 `(event_id, memo_hash)` 기준으로 이미 처리된 메모를 걸러내므로 변경 없으면 1~2초 내 early-return
- **효과**: 메모 작성 후 **최대 5분 내** 카트에 담김 (이전 최대 24시간)

### 0.5 카카오 알림 — 모든 알림 영구 1회
- 총 15종 알림 모두 `dedup_key` + `cooldown_hours=None`으로 영구 1회 발송
- 재발송 필요 시 `data/notify_state.json` 에서 해당 키 수동 제거
- **신규 알림 2종**:
  - `cdp_unavailable` (CDP Chrome 미실행)
  - `coupang_session_expired` (쿠팡 쿠키 만료 → 재임포트 필요)
  - `gemini_parse_failed` (Gemini API 3회 연속 실패)
- **한도 완화**: `stock.max_daily_orders` 기본값 5 → 100 (사실상 무제한, 폭주 방지 안전캡은 유지)

### 0.6 예외 격리 강화
- `run_pipeline()` 전체를 `try/except/finally`로 감싸 어떠한 예외도 프로세스를 죽이지 못하게 함
- `finally` 블록에서 `_record_last_success()` / 로그 정리 / GC 항상 실행 → watchdog 오탐 방지
- `add_items_to_cart()` 내부에도 `try/except/finally`로 브라우저 정리 보장

---

## 1. 개요

### 1.1 목적
숙박업 운영 중 발생하는 소모품 재고 관리를 자동화한다. 알바생이 청소 후 구글 캘린더에 재고 부족을 메모하면, 시스템이 자동으로 쿠팡 장바구니에 해당 상품을 담아두고 카카오톡으로 결과를 알린다. 사장님은 알림 받고 쿠팡 앱에서 결제만 클릭한다.

### 1.2 배경
- 알바생이 카카오톡으로 재고 부족을 보고 → 사장님이 매번 수동으로 쿠팡에서 검색·주문 → 시간 소모 큼
- 기존 `reservation-bot` 레포에 예약 자동화 인프라가 이미 구축되어 있음
- 같은 인프라(설정, DB, 카카오 알림, 로깅, 스케줄러)를 재활용하여 빠르게 확장 가능

### 1.3 v2 핵심 설계 원칙
**"사장님 승인 단계를 자동 차단 로직으로 대체"**
- 가격/중복/횟수 자동 차단으로 안전성 확보
- 양방향 통신 인프라(텔레그램, 웹페이지) 일체 불필요
- 결제는 어차피 사장님이 직접 하므로 결제 직전이 최종 검토 시점

### 1.4 성공 지표
- 재고 메모 작성 → 장바구니 담김까지 평균 소요시간: **1시간 이내**
- 매핑된 품목의 주문 성공률: **90% 이상**
- 사장님 수동 작업 시간: 주당 **30분 이하**
- 잘못 담긴 품목으로 인한 결제 취소: **월 0건**

---

## 2. 사용자 시나리오

### 2.1 정상 흐름
1. 알바생이 청소 후 구글 캘린더 `소캠스 예약일정`의 해당 일정에 메모 작성  
   예: `"세정티슈재고없음, 키친타월3개, 장작3박스, 빈츠 15개정도 남았습니다"`
2. 시스템이 매시간 캘린더를 조회하여 새 메모 감지
3. Claude API로 메모를 파싱 → 부족 품목 추출  
   결과: `["세정티슈"]` (재고없음/부족 표현이 있는 것만)
4. 매핑표(`product_mapping.json`)에서 쿠팡 URL 조회
5. 매핑 없는 품목은 쿠팡 주문내역에서 검색하여 가장 최근 주문 자동 선택
6. 자동 안전장치 통과 시 → Playwright로 쿠팡 장바구니 자동 담기
7. 카카오톡으로 결과 알림 (성공/스킵/실패 + 총액)
8. 사장님이 쿠팡 앱에서 결제 1번 클릭

### 2.2 자동 차단(스킵) 케이스
- **중복**: 동일 품목이 3일 내 주문된 경우
- **가격 초과 (매핑 기준)**: 매핑표 `최대가격` 초과
- **가격 초과 (이력 기준)**: 쿠팡 최근 주문가의 1.5배 초과
- **품절**: 매핑된 상품이 품절 → 주문내역에서 대체 검색 → 그것도 없으면 스킵
- **매핑 없음**: 매핑표 + 주문내역 모두 매칭 실패 → 스킵 + 별도 알림
- **하루 한도 초과**: 하루 5회 주문 한도 도달 → 다음 사이클로 이연

### 2.3 시스템 중단 케이스 (사람 개입 필요)
- **SMS/캡차 발생**: 자동화 즉시 중단 → 사장님 알림 → 수동 처리 후 재개
- **세션 만료**: 자동화 즉시 중단 → "쿠팡 재로그인 필요" 알림

---

## 3. 기능 요구사항

### 3.1 캘린더 메모 감지 (FR-1)
- **대상 캘린더**: `소캠스 예약일정` (기존 예약용 캘린더 공용 사용)
- **감지 위치**: 일정의 `description` (메모란)
- **트리거 키워드**: `"없"`, `"재고"`, `"부족"`, `"떨어"`, `"다 썼"`, `"품절"` (config로 확장 가능)
- **폴링 주기**: 1시간 (기존 예약 파이프라인과 동일)
- **중복 방지**: 처리 완료된 메모는 DB에 저장, 재처리 안 함
- **메모 변경 감지**: 같은 일정의 메모가 수정된 경우 새 부분만 처리 (해시 비교)

### 3.2 품목 추출 (FR-2)
- **추출 도구**: Claude API (`claude-haiku-4-5-20251001`)
- **입력**: 자연어 메모 텍스트
- **출력**: JSON 배열
  ```json
  [
    {"품목명": "세정티슈", "사유": "없음"},
    {"품목명": "키친타월", "사유": "부족"}
  ]
  ```
- **수량은 추출하지 않음** (메모에 안 적힘 → 매핑표 기본값 사용)
- **충분한 재고 표현은 제외** (예: "장작 3박스 있음" → 무시)
- **품목명 정규화**: "키친타올" → "키친타월" 등 표준 형태로 통일

### 3.3 상품 매핑 (FR-3)
- **1순위**: `data/product_mapping.json` 사전 매핑표 조회
  - 정확 일치 + 별칭 일치 모두 검색
- **2순위**: 쿠팡 주문내역(`마이쿠팡 > 주문목록`)에서 품목명으로 검색
  - 가장 최근 주문 자동 선택 (180일 이내)
  - 매칭 결과 가격을 매핑표 부재 시 임시 `최대가격` 기준으로 사용 (1.5배 룰)
- **3순위**: 매칭 실패 → 스킵 + 카카오 알림에 별도 표시
- **매핑표 형식**:
  ```json
  {
    "키친타월": {
      "url": "https://www.coupang.com/vp/products/123456",
      "기본수량": 1,
      "최대가격": 30000,
      "별칭": ["키친타올", "주방타올", "타월"],
      "옵션": {}
    }
  }
  ```

### 3.4 가격 검증 (FR-4)
- 장바구니 담기 전 현재 페이지 가격을 추출
- **차단 조건 1**: 매핑표의 `최대가격` 초과 → 스킵
- **차단 조건 2**: 쿠팡 최근 주문가의 **1.5배 초과** → 스킵
- **품절 상품**: 즉시 스킵 (대체 검색 시도)
- **차단된 항목은 알림에 사유와 가격 표시**

### 3.5 중복 주문 방지 (FR-5)
- DB의 `stock_orders` 테이블에 모든 주문 이력 저장
- 동일 품목이 **3일 내 'ordered' 상태로 존재**하면 자동 스킵
- 별칭도 같이 검사 (예: "키친타월"과 "키친타올" 동일 취급)
- 스킵된 항목은 알림에 "이미 최근 주문됨"으로 표시

### 3.6 하루 주문 한도 (FR-6)
- 하루 최대 5회 주문 (config: `max_daily_orders`)
- 한도 초과 시 다음 사이클로 이연 (스킵 아님, 다음 1시간 후 재시도)
- 한도 도달 시 카카오 알림: "오늘 주문 한도 도달, 내일 처리 예정"

### 3.7 쿠팡 장바구니 담기 (FR-7)
- **도구**: Playwright (Chromium) + `playwright-stealth`
- **세션 관리**: `data/coupang_session.json`에 storage_state 저장
- **headless 모드 OFF** (봇 탐지 회피)
- **각 클릭 사이 2~5초 랜덤 딜레이**
- **각 품목 처리 후 다음 품목까지 5~10초 대기**
- **실패 시 재시도 3회**, 그래도 실패하면 스크린샷 저장 (`logs/coupang_error_{품목}_{시각}.png`)
- **Anti-bot 감지** (캡차/SMS 페이지 selector 발견) → 즉시 중단

### 3.8 결과 알림 (FR-8)
- 모든 처리 완료 후 카카오톡 1건의 메시지 발송
- 메시지 형식:
  ```
  [재고 자동주문 완료]
  
  ✅ 장바구니에 담음 (3건):
  1. 세정티슈 (1개) - 12,800원
  2. 키친타월 (3개) - 24,500원
  3. 빈츠 (2개) - 9,800원
  
  ⏭️ 자동 스킵 (2건):
  - 주방세제: 3일 내 이미 주문됨
  - 장작: 가격 1.7배 (평소 18,000원 → 30,600원)
  
  ⚠️ 매핑 필요 (1건):
  - 수세미: 매핑표 + 주문내역에 없음
  
  총 47,100원
  👉 쿠팡 앱에서 결제해주세요.
  ```
- 처리 결과 0건이면 알림 보내지 않음 (조용히)
- 매핑 필요 항목이 있으면 별도 강조

### 3.9 안전장치 통합 (FR-9)
- **하루 최대 주문 횟수**: 5회 (config 조정 가능)
- **SMS/캡차 감지 시**: 즉시 중단, 사장님 알림 + 스크린샷 저장
- **세션 만료 감지**: 즉시 중단, "쿠팡 재로그인 필요" 알림
- **연속 실패**: 3사이클 연속 실패 시 자동 비활성화 + 알림

---

## 4. 비기능 요구사항

### 4.1 안정성
- 개별 품목 처리 실패가 전체 파이프라인을 중단시키지 않음 (try/except 격리)
- 모든 예외는 `logger.exception`으로 스택트레이스 기록
- 30일 이상 된 로그 자동 삭제 (기존 패턴 그대로)
- Playwright 브라우저는 finally 블록에서 반드시 close

### 4.2 성능
- 메모리 사용: 작업 완료 후 `gc.collect()` (기존 패턴 그대로)
- 한 사이클 총 실행 시간: **5분 이내** (5품목 기준)
- Playwright 1회 실행에 모든 품목 처리 (브라우저 재시작 X)

### 4.3 보안
- `.env` 파일에만 자격증명 저장 (gitignore)
- `data/coupang_session.json` gitignore
- 카카오 토큰은 자동 갱신, `.env`에 재저장 (기존 패턴)
- Anthropic API 키도 .env에 저장

### 4.4 운영
- `python main.py --check`에 재고 시스템 점검 항목 추가
  - 쿠팡 세션 파일 존재 여부 + 유효성
  - `product_mapping.json` 유효성 (JSON 파싱 + 필수 필드)
  - Claude API 키 유효성 (테스트 호출)
  - 캘린더 접근 가능 여부
- 기존 cron/Task Scheduler 그대로 사용 (별도 등록 불필요)

---

## 5. 시스템 구조

### 5.1 디렉터리 구조 (기존 + 추가)
```
reservation-bot/
├── main.py                       # 기존 + run_stock_pipeline() 추가
├── config.json                   # 기존 + stock 섹션 추가
├── .env                          # 기존 + ANTHROPIC_API_KEY 추가
├── requirements.txt              # + playwright, playwright-stealth, anthropic
│
├── modules/
│   ├── config_loader.py          # ✅ 기존 그대로
│   ├── env_loader.py             # ✅ 기존 + ENV_KEYS에 ANTHROPIC_API_KEY 추가
│   ├── db.py                     # ✅ 기존 + stock_orders 테이블 추가
│   ├── detector.py               # ✅ 기존 그대로 (예약용)
│   ├── calendar.py               # ✅ 기존 + read_stock_memos() 추가
│   ├── notifier.py               # ✅ 기존 + send_stock_result() 함수 추가
│   ├── blocker.py                # ✅ 기존 그대로 (예약용)
│   │
│   ├── stock_detector.py         # NEW: 메모에서 부족 품목 감지
│   ├── stock_parser.py           # NEW: Claude API로 품목 추출
│   ├── product_matcher.py        # NEW: 매핑표 + 주문내역 매칭
│   └── coupang_orderer.py        # NEW: Playwright 쿠팡 자동화
│
├── data/
│   ├── product_mapping.json      # NEW: 품목 매핑표
│   └── coupang_session.json      # NEW: 쿠팡 세션 (gitignore)
│
├── scripts/
│   └── coupang_login.py          # NEW: 최초 1회 수동 로그인 스크립트
│
└── (기존 파일들 그대로)
```

### 5.2 main.py 통합 흐름
```python
def run_pipeline():
    # === 기존 예약 처리 ===
    detect_new_reservations() → handle_new/cancel
    update_reservations_from_gmail()
    send_checkin_day_samhaengsi()
    cleanup_cancelled_events()
    sync_github_if_needed()
    
    # === 신규 재고 처리 (추가) ===
    run_stock_pipeline()
    
    cleanup_old_logs()
    gc.collect()


def run_stock_pipeline():
    1. read_stock_memos()              # 캘린더에서 재고 메모 조회
    2. parse_shortage_items()          # Claude API로 부족 품목 추출
    3. apply_skip_filters()            # 중복 + 한도 체크 → 스킵 분류
    4. match_products()                # 매핑표 + 주문내역 매칭 → 매핑 실패 분류
    5. validate_prices_and_order()     # 가격 검증 + Playwright 장바구니 담기
    6. send_result_notification()      # 카카오 결과 알림
```

### 5.3 DB 스키마 (추가)
```sql
CREATE TABLE IF NOT EXISTS stock_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,           -- 메모 감지 시각 (ISO)
    calendar_event_id TEXT,              -- 출처 캘린더 이벤트 ID
    memo_hash TEXT,                      -- 메모 텍스트 해시 (중복 처리 방지)
    item_name TEXT NOT NULL,             -- 품목명 (정규화 후)
    matched_url TEXT,                    -- 쿠팡 상품 URL
    matched_source TEXT,                 -- "mapping" | "order_history" | "none"
    quantity INTEGER DEFAULT 1,
    price INTEGER,                       -- 주문 시 가격 (원)
    status TEXT NOT NULL,                -- "ordered" | "skipped" | "failed" | "unmapped"
    skip_reason TEXT,                    -- 스킵 사유
    fail_reason TEXT,                    -- 실패 사유 + 스택트레이스 일부
    ordered_at TEXT                      -- 장바구니 담은 시각
);

CREATE INDEX IF NOT EXISTS idx_stock_orders_item_date 
    ON stock_orders(item_name, detected_at);

CREATE INDEX IF NOT EXISTS idx_stock_orders_status_date
    ON stock_orders(status, detected_at);

CREATE INDEX IF NOT EXISTS idx_stock_orders_event 
    ON stock_orders(calendar_event_id, memo_hash);
```

### 5.4 config.json 추가 섹션
```json
{
  "_existing_keys": "...",
  
  "stock": {
    "enabled": true,
    "calendar_name": "소캠스 예약일정",
    "trigger_keywords": ["없", "재고", "부족", "떨어", "다 썼", "품절"],
    "duplicate_window_days": 3,
    "max_daily_orders": 5,
    "price_threshold_multiplier": 1.5,
    "claude_model": "claude-haiku-4-5-20251001",
    "memo_lookback_days": 7,
    "playwright": {
      "headless": false,
      "delay_min_sec": 2,
      "delay_max_sec": 5,
      "between_items_min_sec": 5,
      "between_items_max_sec": 10,
      "retry_count": 3,
      "browser_timeout_sec": 30
    }
  }
}
```

### 5.5 .env 추가 키
```
# Claude API (재고 메모 파싱용)
ANTHROPIC_API_KEY=sk-ant-...

# 쿠팡은 .env에 저장하지 않음 (수동 로그인 후 session.json만 사용)
```

---

## 6. 모듈별 상세 명세

### 6.1 `modules/stock_detector.py`
**책임**: 캘린더 메모에서 처리 대상 후보 텍스트 추출

**주요 함수**:
- `detect_stock_memos() -> list[dict]`
  - 입력: 없음 (config의 calendar_name + memo_lookback_days 사용)
  - 출력: `[{"event_id", "memo_text", "memo_hash", "event_date"}]`
  - 동작:
    1. 최근 N일 + 향후 1일 일정 조회
    2. description에 trigger_keywords 중 하나라도 포함된 것만 필터
    3. 각 메모의 SHA256 해시 계산
    4. DB에서 이미 처리된 (event_id, memo_hash) 조합 제외
    5. 메모가 수정된 경우 → 새 해시이므로 다시 처리됨

### 6.2 `modules/stock_parser.py`
**책임**: 자연어 메모 → 부족 품목 리스트

**주요 함수**:
- `parse_shortage_items(memo_text: str) -> list[dict]`
  - 입력: 메모 텍스트 (한글 자유 형식)
  - 출력: `[{"item_name": str, "reason": str}]`
  - 동작: Claude API 호출, 시스템 프롬프트로 추출 규칙 정의
  - 시스템 프롬프트:
    ```
    당신은 숙박업 재고 메모 분석기입니다. 다음 규칙으로 JSON 배열만 출력하세요.
    
    추출 규칙:
    1. "없음", "없다", "부족", "떨어짐", "재고없음", "다 썼" 표현이 있는 품목만 추출
    2. "충분", "있음", "남음", "정도 남았" 표현은 제외 (재고 충분)
    3. 수량 정보는 무시 (별도 매핑표에서 처리)
    4. 품목명은 가장 일반적인 형태로 정규화 (예: "키친타올" → "키친타월")
    5. 출력은 반드시 JSON 배열, 다른 설명 금지
    
    출력 예시:
    [{"item_name": "세정티슈", "reason": "없음"}, {"item_name": "키친타월", "reason": "부족"}]
    
    품목이 하나도 없으면 빈 배열 [] 반환.
    ```
  - 결과 파싱 실패 시 빈 배열 반환 (예외 발생 X, 로그만)

### 6.3 `modules/product_matcher.py`
**책임**: 품목명 → 쿠팡 상품 URL 매칭

**주요 함수**:
- `load_mapping() -> dict`
  - `data/product_mapping.json` 로드
  - 별칭 인덱스 자동 생성 (역방향 lookup용)
- `match_from_mapping(item_name: str, mapping: dict) -> dict | None`
  - 정확 일치 + 별칭 일치 검색
  - 반환: `{"url", "quantity", "max_price", "source": "mapping"}`
- `match_from_order_history(item_name: str, page) -> dict | None`
  - Playwright page 인자로 받음
  - 쿠팡 주문내역 페이지에서 품목명으로 검색
  - 가장 최근 주문 1건 선택 (180일 이내)
  - 반환: `{"url", "quantity": 1, "max_price": 최근가*1.5, "source": "order_history"}`
- `match_product(item_name, page=None) -> dict | None`
  - 위 두 함수를 순차 호출, 매칭 실패 시 None

### 6.4 `modules/coupang_orderer.py`
**책임**: Playwright로 쿠팡 장바구니 담기

**주요 함수**:
- `init_browser() -> tuple`
  - storage_state 로드, stealth 적용
  - headless=False로 시작
  - 반환: (browser, context, page)
- `is_session_valid(page) -> bool`
  - 마이쿠팡 페이지 접속 → 로그인 상태 확인
- `detect_anti_bot(page) -> str | None`
  - 캡차/SMS 인증 페이지 selector 감지
  - 반환: "captcha" | "sms" | "session_expired" | None
- `add_items_to_cart(items: list[dict]) -> dict`
  - 입력: `[{"item_name", "url", "quantity", "max_price"}]`
  - 출력: `{"success": [...], "failed": [...], "stopped": bool, "stop_reason": str}`
  - 각 품목별 try/except로 격리
  - anti-bot 감지 시 즉시 중단 (`stopped=True`)
- `add_single_item(page, item) -> dict`
  - 1품목 처리 (재시도 3회 내장)
  - 성공: `{"status": "ordered", "price": int}`
  - 실패: `{"status": "failed", "reason": str}`
- `close_browser(browser, context)`
  - storage_state 갱신 저장 후 종료
  - finally 블록에서 호출 보장

### 6.5 `modules/calendar.py` 추가 함수
- `read_stock_memos(calendar_name, lookback_days) -> list[dict]`
  - 기존 `_get_google_calendar_service()` 재활용
  - 지정 캘린더의 최근 N일 일정에서 description이 있는 것만 반환
  - 출력: `[{"event_id", "summary", "description", "start_date"}]`

### 6.6 `modules/notifier.py` 추가 함수
- `send_stock_result(result: dict)`
  - 입력: `{"success": [], "skipped": [], "unmapped": [], "failed": []}`
  - 메시지 포맷팅 후 기존 `_send_kakao_message()` 호출
  - 처리 항목 0건이면 발송 안 함
- `send_stock_alert(message: str)`
  - SMS/세션 만료 등 즉시 알림용
  - 기존 `_send_kakao_message()` 직접 호출

### 6.7 `main.py` 추가 함수
- `run_stock_pipeline() -> int`
  - 위 모듈들을 순차 호출, 통계 반환
  - 기존 예약 파이프라인과 동일한 try/except 패턴
- `_setup_check_stock() -> list[str]`
  - `--check`에서 호출, 문제점 리스트 반환

### 6.8 `scripts/coupang_login.py` (수동 1회 실행)
- Playwright 띄우고 사장님이 직접 로그인 + SMS 인증
- 완료 후 `data/coupang_session.json` 저장
- 30일에 1회 정도 재실행 필요할 수 있음

---

## 7. 매핑표 셋업 가이드 (사장님 수행)

### 7.1 초기 셋업 (1회, 1~2시간)
1. 자주 사용하는 소모품 30~50개 리스트업
2. 쿠팡에서 각 품목의 "단골 상품" URL 수집
3. `data/product_mapping.json` 작성
4. 별칭(알바생이 다르게 부를 수 있는 표현) 추가

### 7.2 운영 중 추가
- 매핑 안 된 품목은 카카오톡 알림으로 안내됨
- 사장님이 그때그때 매핑표에 추가
- 매핑표는 GitHub에서 직접 편집 가능 (push 시 자동 반영)

### 7.3 예시
```json
{
  "키친타월": {
    "url": "https://www.coupang.com/vp/products/123456?vendorItemId=789",
    "기본수량": 1,
    "최대가격": 30000,
    "별칭": ["키친타올", "주방타올", "타월"],
    "옵션": {}
  },
  "세정티슈": {
    "url": "https://www.coupang.com/vp/products/234567",
    "기본수량": 2,
    "최대가격": 15000,
    "별칭": ["청소티슈", "세정물티슈"],
    "옵션": {}
  }
}
```

---

## 8. 위험 및 제약사항

### 8.1 기술적 제약
| 항목 | 제약 | 영향도 | 대응 |
|------|------|--------|------|
| 쿠팡 공식 주문 API 없음 | Playwright 우회 필수 | 높음 | playwright-stealth |
| 쿠팡 봇 탐지 강화 | 100% 보장 X | 높음 | 랜덤 딜레이, headless OFF |
| SMS 인증 발생 가능 | 사람 개입 필요 | 중간 | 즉시 중단 + 알림 |
| 쿠팡 HTML 변경 | selector 깨짐 | 중간 | 분기별 점검 |
| 카카오 단방향 API | 사장님 답장 못 받음 | **해소** | v2에서 승인 단계 제거 |

### 8.2 운영 제약
- PC 24시간 켜두기 필수 (또는 미니PC)
- 카카오 토큰 만료 시 사장님 재인증 필요
- 매핑표 누적 관리 책임은 사장님

### 8.3 법적 회색지대
- 쿠팡 이용약관 "자동화된 수단 접근 금지" 조항 존재
- 개인 본인 계정 본인 주문은 단속 사례 거의 없으나, **공식적으로 약관 위반**
- 사장님이 인지하고 진행 → 책임 본인

### 8.4 v2 안전성 보장 메커니즘 (승인 대체)
승인 단계가 없는 만큼 다음 자동 차단이 핵심 안전장치:

1. **가격 1.5배 룰**: 어떤 이유로든 평소보다 비싸지면 자동 스킵
2. **3일 중복 차단**: 알바생이 같은 메모를 두 번 적어도 안전
3. **하루 5회 한도**: 코드 버그로도 폭주 못 함
4. **매핑 없는 신규 품목 스킵**: 엉뚱한 상품 잘못 담는 위험 차단
5. **결제는 어차피 사장님**: 장바구니에 잘못 담겼더라도 결제 직전 최종 확인 가능

→ 이 5겹 안전장치가 사실상 "사장님 사전 승인" 역할을 자동으로 수행

### 8.5 명시적 제외 (Out of Scope)
- ❌ 결제 자동화 (위험성 큼)
- ❌ 사장님 사전 승인 (v1에서 제외, 자동 차단으로 대체)
- ❌ 다른 쇼핑몰(이마트, 홈플러스 등) 동시 주문
- ❌ 가격 비교 후 최저가 자동 선택 (봇 탐지 트리거)
- ❌ 100% 무인 운영 (월 1~3회 사람 개입 필수)
- ❌ 텔레그램/웹페이지 등 양방향 통신 인프라

---

## 9. 개발 일정 (실측 기준)

| Day | 작업 | 산출물 |
|-----|------|--------|
| **Day 1** | PRD v2 확정 → Claude Code 5단계 프롬프트 작성 | prompts.md |
| **Day 2** | Claude Code로 모듈 4개 + main.py 통합 + DB 스키마 | 코드 PR |
| **Day 3** | 매핑표 셋업 + 쿠팡 첫 로그인 + 통합 테스트 | 가동 시작 |
| Day 4+ | 운영 중 발견된 엣지 케이스 보완 | - |

---

## 10. 검수 기준 (DoD)

### 10.1 코드 검수
- [ ] 모든 신규 모듈에 docstring 작성
- [ ] `python main.py --check` 통과
- [ ] 기존 예약 파이프라인 영향 없음 (회귀 테스트)
- [ ] 모든 외부 호출에 try/except + logger.exception
- [ ] Playwright 브라우저는 finally 블록에서 close 보장
- [ ] gc.collect() 호출 (메모리 관리)

### 10.2 기능 검수
- [ ] 캘린더 메모 → 부족 품목 추출 정확도 90% 이상 (10건 테스트)
- [ ] 매핑된 상품 장바구니 담기 성공률 90% 이상
- [ ] 3일 내 중복 주문 정상 차단
- [ ] 가격 1.5배 초과 시 정상 중단
- [ ] 매핑 없는 품목 정상 스킵 + 알림
- [ ] SMS 인증 발생 시 즉시 중단 + 알림
- [ ] 처리 0건 시 알림 안 옴 (스팸 방지)

### 10.3 운영 검수
- [ ] 1주일 무중단 가동
- [ ] 로그 정상 기록 (logs/YYYY-MM-DD.log)
- [ ] 카카오 알림 정상 수신
- [ ] cron/Task Scheduler 정상 트리거
- [ ] 메모리 누수 없음 (1주일 후 메모리 사용량 안정)

---

## 11. 후속 개선 (Phase 2)

현 PRD 범위 밖이지만 향후 검토:
- 재고 트렌드 분석 (월별 소모량 그래프)
- 자주 떨어지는 품목 자동 대량 주문 추천
- 네이버 스토어, 이마트몰 등 다중 쇼핑몰 가격 비교
- 객실별 재고 분리 관리
- OCR로 영수증 자동 인식 → 매핑표 자동 업데이트

---

**문서 끝.**

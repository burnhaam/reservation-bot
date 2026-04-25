"""
SQLite 데이터베이스 초기화 및 접속 모듈.

이미 처리한 예약(네이버/에어비앤비)을 중복 처리하지 않도록
예약 ID와 연관 메타데이터를 영속 저장하는 역할을 한다.
재고 자동주문 기능에서는 처리된 재고 메모와 주문 이력을 저장한다.
"""

import sqlite3
from pathlib import Path


# 프로젝트 루트 기준 DB 파일 경로
DB_PATH = Path(__file__).resolve().parent.parent / "db" / "reservations.db"


# reservations 테이블 생성 SQL
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reservations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    platform             TEXT    NOT NULL,           -- 'naver' 또는 'airbnb'
    booking_id           TEXT    NOT NULL UNIQUE,    -- 플랫폼 예약 고유 ID
    guest_name           TEXT,                       -- 예약자 이름
    guests               INTEGER,                    -- 인원 수
    checkin              TEXT,                       -- 체크인 (YYYY-MM-DD)
    checkout             TEXT,                       -- 체크아웃 (YYYY-MM-DD)
    status               TEXT,                       -- 예약 상태 (confirmed/cancelled 등)
    calendar_event_id_a  TEXT,                       -- 소유자 캘린더 이벤트 ID
    calendar_event_id_b  TEXT,                       -- 담당자 캘린더 이벤트 ID
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


# stock_orders 테이블 생성 SQL (재고 자동주문)
CREATE_STOCK_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS stock_orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at         TEXT    NOT NULL,            -- 메모 감지 시각 (ISO)
    calendar_event_id   TEXT,                        -- 출처 캘린더 이벤트 ID
    memo_hash           TEXT,                        -- 메모 텍스트 해시 (중복 처리 방지)
    item_name           TEXT    NOT NULL,            -- 품목명 (정규화 후)
    matched_url         TEXT,                        -- 쿠팡 상품 URL
    matched_source      TEXT,                        -- 'mapping' | 'order_history' | 'none'
    quantity            INTEGER DEFAULT 1,
    price               INTEGER,                     -- 주문 시 가격 (원)
    status              TEXT    NOT NULL,            -- 'ordered' | 'skipped' | 'failed' | 'unmapped'
    skip_reason         TEXT,                        -- 스킵 사유
    fail_reason         TEXT,                        -- 실패 사유 + 스택트레이스 일부
    ordered_at          TEXT,                        -- 장바구니 담은 시각
    confirmed_at        TEXT,                        -- 실주문 확인 시각 (주문내역에 등장)
    scan_done_at        TEXT                         -- 주문내역 스캔 완료 시각 (재스캔 방지)
);
"""

CREATE_STOCK_INDEX_ITEM_DATE_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_stock_orders_item_date "
    "ON stock_orders(item_name, detected_at);"
)

CREATE_STOCK_INDEX_STATUS_DATE_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_stock_orders_status_date "
    "ON stock_orders(status, detected_at);"
)

CREATE_STOCK_INDEX_EVENT_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_stock_orders_event "
    "ON stock_orders(calendar_event_id, memo_hash);"
)

CREATE_STOCK_INDEX_SCAN_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_stock_orders_scan "
    "ON stock_orders(scan_done_at, detected_at) "
    "WHERE status = 'ordered';"
)


def _migrate_stock_orders_columns(conn: sqlite3.Connection) -> None:
    """기존 DB에 confirmed_at / scan_done_at 컬럼이 없으면 추가한다.

    ALTER TABLE은 트랜잭션 외부에서도 안전하며, 컬럼이 이미 존재하면 에러.
    PRAGMA table_info로 먼저 확인 후 선택적으로 실행.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(stock_orders)")}
    if "confirmed_at" not in cols:
        conn.execute("ALTER TABLE stock_orders ADD COLUMN confirmed_at TEXT")
    if "scan_done_at" not in cols:
        conn.execute("ALTER TABLE stock_orders ADD COLUMN scan_done_at TEXT")


def get_connection(path: Path = DB_PATH) -> sqlite3.Connection:
    """SQLite 커넥션을 반환. 컬럼명 기반 접근을 위해 Row Factory 설정.

    안정성 보강:
    - timeout=30: 다른 커넥션이 쓰기 락을 쥐고 있어도 30초까지 대기.
    - WAL 모드: 읽기/쓰기 동시성이 기존 rollback journal보다 크게 향상.
      웹훅 서버, 메인 파이프라인, 워치독 등 여러 프로세스가 DB를 동시에 쓸 때 안전.
    - busy_timeout=30000ms: SQLite 레벨에서도 30초 재시도.
    isolation_level은 건드리지 않아 기존 명시적 commit() 코드와 호환.
    """
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.DatabaseError:
        # 손상된 DB 등 극단적 상황에서 PRAGMA가 실패해도 커넥션 자체는 반환
        pass
    return conn


def init_db(path: Path = DB_PATH) -> None:
    """DB 파일과 reservations/stock_orders 테이블이 없으면 생성한다."""
    # db 디렉터리가 없을 경우 대비해 자동 생성
    path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(path) as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_STOCK_ORDERS_SQL)
        _migrate_stock_orders_columns(conn)
        conn.execute(CREATE_STOCK_INDEX_ITEM_DATE_SQL)
        conn.execute(CREATE_STOCK_INDEX_STATUS_DATE_SQL)
        conn.execute(CREATE_STOCK_INDEX_EVENT_SQL)
        conn.execute(CREATE_STOCK_INDEX_SCAN_SQL)
        conn.commit()

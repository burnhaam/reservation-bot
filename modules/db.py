"""
SQLite 데이터베이스 초기화 및 접속 모듈.

이미 처리한 예약(네이버/에어비앤비)을 중복 처리하지 않도록
예약 ID와 연관 메타데이터를 영속 저장하는 역할을 한다.
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


def get_connection(path: Path = DB_PATH) -> sqlite3.Connection:
    """SQLite 커넥션을 반환한다. 컬럼명 기반 접근을 위해 Row Factory 설정."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = DB_PATH) -> None:
    """DB 파일과 reservations 테이블이 없으면 생성한다."""
    # db 디렉터리가 없을 경우 대비해 자동 생성
    path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(path) as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()

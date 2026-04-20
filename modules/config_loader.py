"""
config.json 설정 파일 로드 모듈.

사용자가 코드 수정 없이 운영값(담당자명, 폴링 주기, 캘린더명 등)을
변경할 수 있도록 JSON 파일에서 읽어오는 역할을 담당한다.
"""

import json
from pathlib import Path


# 프로젝트 루트 기준 config.json 경로
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """config.json 파일을 읽어 딕셔너리로 반환한다."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

"""
품목명 → 쿠팡 상품 URL 매칭 모듈.

매핑 우선순위:
  1순위) data/product_mapping.json 사전 매핑표 (별칭 + 스킵 포함)
  2순위) 쿠팡 마이쿠팡 주문내역 검색 (match_from_order_history)
         — Gemini로 오타 정정 + 검색 키워드 2~3개 생성 후 각각 시도
         — 브라우저(page) 필요하므로 add_items_to_cart 내부에서 호출

주요 함수:
- load_mapping(): 매핑표 로드 + 별칭/스킵 인덱스 생성
- is_skip_item(): 자동주문 제외 품목 여부 판정
- normalize_to_canonical(): 별칭을 표준명으로 정규화
- match_from_mapping(): 매핑표에서 검색 (1순위)
- match_from_order_history(): 주문내역 검색 (2순위, 품절 대체에도 사용)
- match_product(): 통합 매칭 API
"""

import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Optional


# Gemini 키워드 생성용 (지연 import — 없어도 폴백 동작)
try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
    _HAS_GENAI = True
except ImportError:
    _HAS_GENAI = False


logger = logging.getLogger(__name__)


# 프로젝트 루트 기준 파일 경로
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MAPPING_PATH = _PROJECT_ROOT / "data" / "product_mapping.json"


def load_mapping(path: Path = _MAPPING_PATH) -> dict:
    """data/product_mapping.json을 로드하고 별칭 인덱스 + 스킵 인덱스를 붙여 반환.

    반환 구조: {
        "items": {원본 키: 원본 값, ...},
        "alias_index": {별칭/키 소문자: 원본 키, ...},
        "skip_items": {스킵 품목명: {"reason", "aliases"}, ...},
        "skip_index": {스킵 별칭/키 소문자: 스킵 품목명, ...}
    }
    매핑표 상단의 "_skip_items" 키 아래에 자동주문 제외 품목(예: 카메라필름)을
    정의하면 main.py 파이프라인이 is_skip_item()으로 조기 차단한다.
    파일이 없거나 파싱 실패 시 빈 구조 반환.
    """
    empty = {"items": {}, "alias_index": {}, "skip_items": {}, "skip_index": {}}

    if not path.exists():
        logger.warning("[Matcher] 매핑표 없음: %s", path)
        return empty

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        logger.exception("[Matcher] 매핑표 로드/파싱 실패: %s", path)
        return empty

    if not isinstance(raw, dict):
        logger.error("[Matcher] 매핑표 형식 비정상 (dict 아님): %s", path)
        return empty

    items: dict = {}
    alias_index: dict = {}
    skip_items: dict = {}
    skip_index: dict = {}

    for name, body in raw.items():
        if not isinstance(name, str):
            continue

        # 자동주문 제외 품목 섹션 (다른 채널에서 구매하는 품목 등)
        if name == "_skip_items":
            if isinstance(body, dict):
                for skip_name, skip_body in body.items():
                    if not isinstance(skip_name, str) or not isinstance(skip_body, dict):
                        continue
                    skip_items[skip_name] = skip_body
                    skip_index[skip_name.lower()] = skip_name
                    for alias in skip_body.get("aliases", []) or []:
                        if isinstance(alias, str) and alias:
                            skip_index[alias.lower()] = skip_name
            continue

        # 기타 _로 시작하는 주석 키는 건너뜀
        if name.startswith("_"):
            continue
        if not isinstance(body, dict):
            continue

        items[name] = body
        alias_index[name.lower()] = name
        for alias in body.get("별칭", []) or []:
            if isinstance(alias, str) and alias:
                alias_index[alias.lower()] = name

    logger.info("[Matcher] 매핑표 로드 OK — %d개 품목, %d개 별칭, 스킵 %d개",
                len(items), len(alias_index), len(skip_items))
    return {
        "items": items,
        "alias_index": alias_index,
        "skip_items": skip_items,
        "skip_index": skip_index,
    }


def is_skip_item(item_name: str, mapping: dict) -> Optional[dict]:
    """자동주문 제외 품목이면 {"name", "reason"} 반환. 아니면 None.

    매핑표 _skip_items에 등록된 이름/별칭과 대소문자 무시 일치로 판단한다.
    """
    if not item_name:
        return None
    skip_index = mapping.get("skip_index", {}) or {}
    canonical = skip_index.get(item_name.lower())
    if not canonical:
        return None
    body = (mapping.get("skip_items", {}) or {}).get(canonical, {}) or {}
    return {
        "name": canonical,
        "reason": body.get("reason", "자동주문 제외 품목"),
    }


def normalize_to_canonical(item_name: str, mapping: dict) -> str:
    """품목명을 매핑표의 canonical(키) 이름으로 정규화.

    별칭으로 들어온 이름을 표준명으로 바꿔 중복 체크/DB 저장을
    일관되게 처리하기 위한 헬퍼.
    매핑표에 없으면 원본 그대로 반환한다.

    예: "키친타올" → "키친타월" (매핑표 키 "키친타월"의 별칭에 "키친타올"이 있을 때)
    """
    if not item_name:
        return item_name
    alias_index = mapping.get("alias_index", {}) or {}
    canonical = alias_index.get(item_name.lower())
    return canonical if canonical else item_name


def match_from_mapping(item_name: str, mapping: dict) -> Optional[dict]:
    """사전 매핑표(별칭 포함)에서 품목을 검색하여 매칭 정보 반환.

    반환: {"url", "quantity", "max_price", "source": "mapping"} 또는 None
    """
    if not item_name or not mapping:
        return None

    items = mapping.get("items", {}) or {}
    alias_index = mapping.get("alias_index", {}) or {}

    canonical = alias_index.get(item_name.lower())
    if not canonical:
        return None

    body = items.get(canonical) or {}
    url = body.get("url", "")
    if not url:
        logger.warning("[Matcher] 매핑 항목에 url 없음: %s", canonical)
        return None

    return {
        "url": url,
        "quantity": int(body.get("기본수량", 1) or 1),
        "max_price": int(body.get("최대가격", 0) or 0),
        # 최대재고가 정의된 품목(예: 장작=6)은 메모의 current_stock과 비교해
        # 부족분만큼만 주문하도록 main.py run_stock_pipeline에서 활용된다.
        "max_stock": int(body.get("최대재고", 0) or 0),
        "source": "mapping",
        "canonical_name": canonical,
        "options": body.get("옵션", {}) or {},
    }


def match_product(item_name: str,
                  mapping: Optional[dict] = None) -> Optional[dict]:
    """매핑표에서 품목을 매칭. 매핑 없으면 None.

    mapping을 주면 재로드 없이 재사용. None이면 내부에서 로드.
    """
    if not item_name:
        return None

    if mapping is None:
        mapping = load_mapping()

    hit = match_from_mapping(item_name, mapping)
    if hit:
        logger.info("[Matcher] 매핑표 매칭: %s → %s", item_name, hit.get("canonical_name"))
        return hit

    logger.warning("[Matcher] 매칭 실패 (매핑표에 없음): %s", item_name)
    return None


# =============================================================
# 주문내역 기반 매칭 (Phase A)
# =============================================================

_ORDER_SEARCH_URL = "https://mc.coupang.com/ssr/desktop/order/list?isSearch=true&keyword={kw}"


def _generate_search_keywords(raw_name: str) -> list[str]:
    """품목명 오타 정정 + 검색 키워드 2~3개 생성.

    Gemini 2.5 Flash로 짧은 JSON 배열 응답. 실패 시 토큰 분할 폴백.
    - 토큰 폴백: [전체명, 마지막 토큰] (예: "롯데웰푸드 빈츠" → ["롯데웰푸드 빈츠", "빈츠"])

    반환 키워드 순서: 가장 정확(전체명 정정)부터 축약까지.
    """
    cleaned = (raw_name or "").strip()
    if not cleaned:
        return []

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not _HAS_GENAI or not api_key:
        return _fallback_tokens(cleaned)

    prompt = (
        f"""다음 품목명을 쿠팡 주문내역 검색용 키워드 배열(JSON)로 변환:
- 오타 있으면 정정 (예: "빈쯔" → "빈츠")
- 가장 정확한 전체명부터 짧은 핵심 단어 순으로 2~3개
- 핵심 규칙: 각 키워드는 "고유 제품명"을 포함해야 함. 단독 카테고리어 금지
  금지: "샴푸", "티슈", "물", "세제", "휴지", "과자", "장작", "치즈", "우유"
  허용: "탐사 샴푸", "엘리트 세정티슈", "참나무장작"
- 모든 키워드는 최소 3글자 이상. 2글자 단일어는 허용하지 않음

예시:
  "롯데웰푸드 빈쯔" → ["롯데웰푸드 빈츠", "롯데 빈츠"]
  "세정 티슈우" → ["세정티슈"]
  "탐사 실키 데일리 퍼퓸 샴푸" → ["탐사 실키 퍼퓸 샴푸", "탐사 실키 샴푸", "탐사 샴푸"]
  "참나무장작" → ["참나무장작"]

입력: "{cleaned}"
""")

    try:
        client = genai.Client(api_key=api_key)
        schema = {"type": "ARRAY", "items": {"type": "STRING"}}
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                max_output_tokens=128,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                http_options=types.HttpOptions(timeout=15000),
            ),
        )
        text = getattr(response, "text", None) or ""
        if not text:
            return _fallback_tokens(cleaned)
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            return _fallback_tokens(cleaned)
        keywords = []
        seen = set()
        for k in parsed:
            if isinstance(k, str):
                k = k.strip()
                if k in seen:
                    continue
                # 3글자 미만 단독어 혹은 블랙리스트 단독어는 거른다
                if len(k) < 3:
                    continue
                if " " not in k and k in _GENERIC_BLACKLIST:
                    continue
                seen.add(k)
                keywords.append(k)
        if not keywords:
            return _fallback_tokens(cleaned)
        return keywords[:3]
    except APIError as e:
        logger.warning("[Matcher] 키워드 생성 API 오류 — 폴백: %s", e)
        return _fallback_tokens(cleaned)
    except Exception:
        logger.warning("[Matcher] 키워드 생성 실패 — 폴백", exc_info=True)
        return _fallback_tokens(cleaned)


_GENERIC_BLACKLIST = {
    "샴푸", "티슈", "휴지", "과자", "장작", "치즈", "우유", "물", "세제",
    "수건", "칫솔", "비누", "로션", "소금", "설탕", "간장", "케찹", "고추장",
    "수전", "숯", "탕", "밥", "면", "빵", "젤", "차",
}


def _fallback_tokens(cleaned: str) -> list[str]:
    """Gemini 사용 불가 시 단순 토큰 분할 폴백. 블랙리스트 필터 적용."""
    tokens = cleaned.split()
    result = [cleaned]
    if len(tokens) >= 2:
        last = tokens[-1]
        # 3글자 미만 단독어 혹은 카테고리 블랙리스트 단독어 제외
        if len(last) >= 3 and last not in _GENERIC_BLACKLIST:
            result.append(last)
    return result


def match_from_order_history(item_name: str, page) -> Optional[dict]:
    """쿠팡 마이쿠팡 주문내역에서 키워드로 과거 주문을 검색해 상품 URL을 찾는다.

    흐름:
      1. _generate_search_keywords()로 검색어 2~3개 (오타 정정 + 축약)
      2. 각 키워드로 ?isSearch=true&keyword=X 페이지 goto
      3. 검색 결과 중 키워드 포함한 sdp/link 앵커 href 추출
      4. sdp/link goto → redirect 후 page.url이 /vp/products/XXX 형태면 그걸 반환

    반환: {
      "url": "https://www.coupang.com/vp/products/XXX",
      "canonical_name": item_name,
      "max_price": 0,  # 가격 가드 미사용 (매핑표에 없어 기준 없음)
      "최대가격": 0,
      "source": "order_history",
      "search_keyword": 실제 매치된 키워드,
    } 또는 None (주문내역에 없음).

    주의: page를 goto하면서 기존 탭을 재사용하므로 이 함수 호출 후
          반드시 호출자가 원래 작업으로 page.goto를 복귀시켜야 한다.
    """
    if not item_name or page is None:
        return None

    keywords = _generate_search_keywords(item_name)
    if not keywords:
        return None
    logger.info("[Matcher:OrderHist] 검색 키워드 생성: '%s' → %s", item_name, keywords)

    # 상호 import를 피하려고 지연 import
    try:
        from modules.coupang_orderer import _wait_for_akamai_challenge_clear
    except Exception:
        def _wait_for_akamai_challenge_clear(p, max_sec=25):
            return True

    for kw in keywords:
        hit = _search_order_for_keyword(page, kw, _wait_for_akamai_challenge_clear)
        if hit:
            logger.info("[Matcher:OrderHist] 매치: '%s' 키워드 '%s' → %s",
                        item_name, kw, hit["url"])
            return {
                "url": hit["url"],
                "canonical_name": item_name,
                "max_price": 0,
                "최대가격": 0,
                "source": "order_history",
                "search_keyword": kw,
            }

    logger.info("[Matcher:OrderHist] 주문내역에 없음: '%s' (시도 %s)", item_name, keywords)
    return None


def _search_order_for_keyword(page, keyword: str, wait_akamai) -> Optional[dict]:
    """단일 키워드 검색 → 첫 매치의 상품 상세 URL 추출.

    page는 goto로 이동되므로 caller가 복귀시켜야 한다.
    """
    encoded = urllib.parse.quote(keyword)
    search_url = _ORDER_SEARCH_URL.format(kw=encoded)
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        wait_akamai(page, 25)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception:
        logger.exception("[Matcher:OrderHist] 검색 페이지 goto 실패: %s", search_url)
        return None

    # 키워드 포함 sdp/link 앵커 첫 개 찾기
    js = f"""
        () => {{
            const kw = {keyword!r};
            const anchors = document.querySelectorAll('a[href*="ssr/sdp/link"]');
            for (const a of anchors) {{
                const text = (a.innerText || a.textContent || '').trim();
                if (text.includes(kw)) return {{ href: a.href, text: text.slice(0, 100) }};
            }}
            return null;
        }}
    """
    try:
        match = page.evaluate(js)
    except Exception:
        logger.exception("[Matcher:OrderHist] 검색 결과 파싱 실패")
        return None
    if not match:
        return None

    # sdp/link goto → redirect 후 /vp/products/XXX 획득
    try:
        page.goto(match["href"], wait_until="domcontentloaded", timeout=60000)
        wait_akamai(page, 25)
        final_url = page.url or ""
    except Exception:
        logger.exception("[Matcher:OrderHist] sdp/link goto 실패")
        return None

    if "/vp/products/" not in final_url:
        logger.warning("[Matcher:OrderHist] 최종 URL이 상품 페이지 아님: %s", final_url[:100])
        return None

    # 파라미터 제거 (sourceType 등) — 깔끔한 상품 URL만
    clean_url = final_url.split("?")[0]
    return {"url": clean_url, "title": match.get("text", "")}

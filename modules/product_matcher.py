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


def match_from_mapping(
    item_name: str,
    mapping: dict,
    raw_name: Optional[str] = None,
) -> Optional[dict]:
    """사전 매핑표(별칭 포함)에서 품목을 검색하여 매칭 정보 반환.

    raw_name: 정규화 이전의 원본 메모 품목명. variant 선별에 사용.
              (예: canonical="곰곰 쌀과자" 인데 raw_name="곰곰 쌀과자 달콤한맛"
               이면 구분 토큰 {달콤, 한맛} 을 variant name 과 비교해 특정 variant 만 선택)

    반환: {"url", "quantity", "max_price", "variants", "source": "mapping", ...} 또는 None
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

    # variants 원본 정리
    variants_raw = body.get("variants") or []
    variants_all: list[dict] = []
    for v in variants_raw:
        if not isinstance(v, dict):
            continue
        vurl = v.get("url", "")
        if not vurl:
            continue
        variants_all.append({
            "url": vurl,
            "name": v.get("name", canonical),
            "quantity": int(v.get("기본수량", 1) or 1),
        })

    # variant 선별 — raw_name 이 canonical 보다 구체적이면 매치하는 variant 만.
    # 메모에 맛/색/사이즈 구분자가 있는 경우만 특정 variant 로 좁힘.
    variants_out = variants_all
    if variants_all:
        raw = (raw_name or item_name or "").strip()
        if raw and raw.lower() != canonical.lower():
            canonical_bi = _text_bigrams(canonical)
            raw_bi = _text_bigrams(raw)
            extra_bi = raw_bi - canonical_bi  # 메모가 canonical 외에 추가로 가진 구분 bigrams
            if extra_bi:
                scored: list[tuple[int, dict]] = []
                for v in variants_all:
                    vname_bi = _text_bigrams(v.get("name", ""))
                    score = len(vname_bi & extra_bi)
                    if score >= 1:
                        scored.append((score, v))
                if scored:
                    scored.sort(key=lambda x: x[0], reverse=True)
                    top_score = scored[0][0]
                    # 동점 상위만 채택 (드문 경우: 두 variant 가 동일 구분 토큰 공유)
                    picked = [v for s, v in scored if s == top_score]
                    variants_out = picked
                    logger.info(
                        "[Matcher] variant 선별: '%s' → canonical '%s' 중 %d/%d variant 선택",
                        raw, canonical, len(picked), len(variants_all),
                    )

    # variant 가 1개로 좁혀졌으면 그 URL 을 primary 로, variants 는 비움 (단건 주문)
    if variants_all and len(variants_out) == 1:
        chosen = variants_out[0]
        return {
            "url": chosen["url"],
            "quantity": chosen.get("quantity", int(body.get("기본수량", 1) or 1)),
            "max_price": int(body.get("최대가격", 0) or 0),
            "max_stock": int(body.get("최대재고", 0) or 0),
            "source": "mapping",
            "canonical_name": canonical,
            "options": body.get("옵션", {}) or {},
            "variants": [],  # 단건이므로 확장 없음
        }

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
        "variants": variants_out,  # 비어있으면 단일 url, 여럿이면 모두 주문
    }


def match_product(item_name: str,
                  mapping: Optional[dict] = None,
                  raw_name: Optional[str] = None) -> Optional[dict]:
    """매핑표에서 품목을 매칭. 매핑 없으면 None.

    mapping을 주면 재로드 없이 재사용. None이면 내부에서 로드.
    raw_name: 정규화 이전 원본 메모 품목명. variants 선별용 (match_from_mapping 참고).
    """
    if not item_name:
        return None

    if mapping is None:
        mapping = load_mapping()

    hit = match_from_mapping(item_name, mapping, raw_name=raw_name)
    if hit:
        logger.info("[Matcher] 매핑표 매칭: %s → %s", item_name, hit.get("canonical_name"))
        return hit

    logger.warning("[Matcher] 매칭 실패 (매핑표에 없음): %s", item_name)
    return None


# =============================================================
# 토큰/바이그램 매칭 (어순 무관 결정론적 매칭)
# =============================================================

# 단위/수량 표현 — 토큰화 시 제거
_UNIT_RE_PATTERN = r"(\d+(?:\.\d+)?\s*(?:g|kg|ml|L|l|cm|mm|m|개|개입|매|장|팩|박스|입|p|P|ea))"


def _strip_units_and_numbers(text: str) -> str:
    """단위/수량/문장부호 제거. 한글/영문/공백만 유지."""
    import re
    if not text:
        return ""
    t = re.sub(_UNIT_RE_PATTERN, " ", text)  # "294g", "10개입" 제거
    t = re.sub(r"[\d,./\-_()\[\]{}!?·,.:;\"'`]", " ", t)  # 숫자/특수문자 제거
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _text_bigrams(text: str) -> set[str]:
    """한글/영문 2-gram 집합 반환.

    공백/단위 제거 후 연속된 2문자 윈도우. 어순 무관 유사도 판정에 사용.
    예: "커피캡슐" → {"커피", "피캡", "캡슐"}
         "캡슐커피" → {"캡슐", "슐커", "커피"}
         교집합 {"커피", "캡슐"} — 이걸로 동일성 판정 가능.
    """
    cleaned = _strip_units_and_numbers(text).replace(" ", "").lower()
    if len(cleaned) < 2:
        return set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def token_overlap_match(name: str, title: str, threshold: float = 0.6) -> bool:
    """name 이 title 안에 포함되어 있는지 어순·단위 무관 판정.

    동작:
      1. substring 매치 성공 → 즉시 True (빠른 경로)
      2. 단위/수량/공백 제거 후 바이그램 집합 생성
      3. 짧은 이름(≤3 bi) 은 교집합 2개 이상 필수 (false positive 방지)
      4. coverage = |name_bi ∩ title_bi| / |name_bi| ≥ threshold 이면 True

    예:
      "커피캡슐" vs "네스프레소 버츄오 볼테소 캡슐커피, 5.2g"
        → name_bi={커피,피캡,캡슐}, 교집합={커피,캡슐} = 2/3 = 0.67 ≥ 0.6 ✓
      "안전장갑" vs "비닐장갑 100매"
        → 교집합={장갑} = 1/3 = 0.33, 짧은 이름인데 매치 1개 → False (FP 방지) ✓
    """
    if not name or not title:
        return False
    # 빠른 경로: 완전 포함
    if name in title:
        return True
    name_bi = _text_bigrams(name)
    if len(name_bi) < 2:
        return False  # 너무 짧은 이름 — substring 외엔 신뢰 불가
    title_bi = _text_bigrams(title)
    if not title_bi:
        return False
    inter = name_bi & title_bi
    # 짧은 이름(2~3 bigrams) 은 단일 매치 시 false positive 위험 크므로 2개 이상 필수
    if len(name_bi) <= 3 and len(inter) < 2:
        return False
    coverage = len(inter) / len(name_bi)
    return coverage >= threshold


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
        from modules.gemini_client import generate_content_with_fallback
        schema = {"type": "ARRAY", "items": {"type": "STRING"}}
        response = generate_content_with_fallback(
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

    # 쿠팡이 이미 keyword 필터링해 반환한 결과. 유의미한 첫 sdp/link 앵커 채택.
    # (기존 text.includes(kw) 엄격 매치는 변형 표기를 놓침: "마시멜로" vs "마시멜로우")
    js = """
        () => {
            const anchors = document.querySelectorAll('a[href*="ssr/sdp/link"]');
            for (const a of anchors) {
                const text = (a.innerText || a.textContent || '').trim();
                if (text.length < 3) continue;  // 이미지 링크/빈 앵커 제외
                return { href: a.href, text: text.slice(0, 100) };
            }
            return null;
        }
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


# =============================================================
# 주문 묶음 fetch (로직 ①②③ 공통 기반)
# =============================================================

_ORDER_LIST_URL = "https://mc.coupang.com/ssr/desktop/order/list"


def fetch_recent_order_groups(page, lookback_days: int = 14) -> list[dict]:
    """마이쿠팡 주문내역 페이지에서 최근 N일 주문 묶음을 파싱.

    반환: [{
      "order_date": "2026-04-22",      # ISO 날짜 (본문의 "YYYY. M. D 주문"에서 파싱)
      "products": [                     # 이 주문에 포함된 상품들
        {"title": "롯데웰푸드 빈츠 204g 2개", "sdp_href": "..."},
        ...
      ]
    }, ...]

    sc-* styled-component 클래스는 변할 수 있으므로 날짜 텍스트를 앵커로 삼아
    그 조상/자손 내의 sdp/link 앵커들을 묶음으로 모은다.
    """
    if page is None:
        return []

    try:
        from modules.coupang_orderer import _wait_for_akamai_challenge_clear
    except Exception:
        def _wait_for_akamai_challenge_clear(p, max_sec=25):
            return True

    try:
        page.goto(_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=60000)
        _wait_for_akamai_challenge_clear(page, 25)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
    except Exception:
        logger.exception("[Matcher:OrderGroup] 주문내역 페이지 goto 실패")
        return []

    import time
    time.sleep(2)

    # JS에서 주문 묶음 추출.
    # 전략: "YYYY. M. D 주문" 텍스트를 가진 엘리먼트를 앵커로, 그 부모 체인 위로
    # 올라가면서 sdp/link 앵커들을 모은다. 한 주문 묶음 = 같은 조상 아래 모든 sdp/link.
    groups = page.evaluate(r"""
        () => {
            const DATE_RE = /^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\s*주문/;
            const out = [];

            // 1) 날짜 텍스트 노드 탐색
            const dateNodes = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const t = (walker.currentNode.nodeValue || '').trim();
                const m = DATE_RE.exec(t);
                if (m) {
                    dateNodes.push({
                        text: t,
                        year: parseInt(m[1], 10),
                        month: parseInt(m[2], 10),
                        day: parseInt(m[3], 10),
                        node: walker.currentNode.parentElement,
                    });
                }
            }

            // 2) 각 날짜 앵커마다 상위 로 올라가서 sdp/link 앵커 2~15개 포함하는
            //    가장 작은 조상을 "주문 묶음 컨테이너"로 간주
            for (const d of dateNodes) {
                let cur = d.node;
                let bestEl = null;
                let bestAnchors = [];
                for (let i = 0; i < 10 && cur; i++) {
                    const anchors = cur.querySelectorAll ?
                        cur.querySelectorAll('a[href*="ssr/sdp/link"]') : [];
                    if (anchors.length >= 1) {
                        // 처음 앵커가 보이는 조상에서 멈춤 (가장 타이트한 컨테이너)
                        bestEl = cur;
                        bestAnchors = Array.from(anchors);
                        break;
                    }
                    cur = cur.parentElement;
                }

                if (!bestEl || bestAnchors.length === 0) continue;

                // vendorItemId 기반 중복 제거 + 빈 title 앵커(이미지 링크 등) 제외
                const seenIds = new Set();
                const products = [];
                for (const a of bestAnchors) {
                    const title = (a.innerText || a.textContent || '').trim();
                    if (!title || title.length < 3) continue;
                    const href = a.href || '';
                    const m = href.match(/vendorItemId=(\d+)/);
                    const key = m ? m[1] : href;
                    if (seenIds.has(key)) continue;
                    seenIds.add(key);
                    products.push({
                        title: title.slice(0, 200),
                        sdp_href: href,
                        vendor_item_id: m ? m[1] : "",
                    });
                }
                if (products.length === 0) continue;

                const iso = `${d.year}-${String(d.month).padStart(2,'0')}-${String(d.day).padStart(2,'0')}`;
                out.push({ order_date: iso, products });
            }

            return out;
        }
    """)

    # 날짜 필터 (최근 N일)
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=lookback_days)).date()
    filtered = []
    for g in groups or []:
        try:
            odate = datetime.strptime(g["order_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if odate < cutoff:
            continue
        filtered.append(g)

    logger.info("[Matcher:OrderGroup] 최근 %d일 주문 묶음: %d건 (전체 %d건 중)",
                lookback_days, len(filtered), len(groups or []))
    return filtered


def find_orders_containing_items(
    page,
    item_names: list[str],
    lookback_days: int = 14,
) -> list[dict]:
    """각 item_name 을 쿠팡 주문내역 검색(keyword=X)으로 조회 → 검색 결과에서 주문 묶음 파싱.

    기본 /order/list 페이지는 최근 5건만 노출하므로 놓치는 사업 주문이 생김.
    검색은 전체 내역에서 쿠팡이 유사어까지 매칭해 반환하므로 누락 방지.

    반환: fetch_recent_order_groups 와 동일 스키마 리스트 (dedup: order_date + vendor_item_id 집합)
    """
    if not item_names or page is None:
        return []

    try:
        from modules.coupang_orderer import _wait_for_akamai_challenge_clear
    except Exception:
        def _wait_for_akamai_challenge_clear(p, max_sec=25):
            return True

    import time
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=lookback_days)).date()

    # 묶음 dedup 키: (order_date, frozenset(vendor_item_ids))
    merged: dict[tuple, dict] = {}

    for raw_name in item_names:
        name = (raw_name or "").strip()
        if not name:
            continue

        encoded = urllib.parse.quote(name)
        search_url = _ORDER_SEARCH_URL.format(kw=encoded)
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            _wait_for_akamai_challenge_clear(page, 25)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
        except Exception:
            logger.exception("[Matcher:OrderSearch] goto 실패 '%s'", name)
            continue

        time.sleep(1)

        try:
            raw_groups = page.evaluate(r"""
                () => {
                    const DATE_RE = /^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\s*주문/;
                    const out = [];
                    const dateNodes = [];
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    while (walker.nextNode()) {
                        const t = (walker.currentNode.nodeValue || '').trim();
                        const m = DATE_RE.exec(t);
                        if (m) dateNodes.push({
                            year: parseInt(m[1], 10),
                            month: parseInt(m[2], 10),
                            day: parseInt(m[3], 10),
                            node: walker.currentNode.parentElement,
                        });
                    }
                    for (const d of dateNodes) {
                        let cur = d.node;
                        let bestAnchors = [];
                        for (let i = 0; i < 10 && cur; i++) {
                            const anchors = cur.querySelectorAll ?
                                cur.querySelectorAll('a[href*="ssr/sdp/link"]') : [];
                            if (anchors.length >= 1) {
                                bestAnchors = Array.from(anchors);
                                break;
                            }
                            cur = cur.parentElement;
                        }
                        if (bestAnchors.length === 0) continue;
                        const seenIds = new Set();
                        const products = [];
                        for (const a of bestAnchors) {
                            const title = (a.innerText || a.textContent || '').trim();
                            if (!title || title.length < 3) continue;
                            const href = a.href || '';
                            const m2 = href.match(/vendorItemId=(\d+)/);
                            const key = m2 ? m2[1] : href;
                            if (seenIds.has(key)) continue;
                            seenIds.add(key);
                            products.push({
                                title: title.slice(0, 200),
                                sdp_href: href,
                                vendor_item_id: m2 ? m2[1] : "",
                            });
                        }
                        if (products.length === 0) continue;
                        const iso = `${d.year}-${String(d.month).padStart(2,'0')}-${String(d.day).padStart(2,'0')}`;
                        out.push({ order_date: iso, products });
                    }
                    return out;
                }
            """)
        except Exception:
            logger.exception("[Matcher:OrderSearch] 파싱 실패 '%s'", name)
            continue

        for g in raw_groups or []:
            try:
                odate = datetime.strptime(g["order_date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if odate < cutoff:
                continue

            vids = frozenset(
                p.get("vendor_item_id", "")
                for p in g.get("products", [])
                if p.get("vendor_item_id")
            )
            key = (g["order_date"], vids)
            if key in merged:
                # 이미 수집된 묶음 — 상품 리스트 병합 (vendorItemId dedup)
                existing = {p["vendor_item_id"]: p for p in merged[key]["products"]}
                for p in g.get("products", []):
                    vid = p.get("vendor_item_id")
                    if vid and vid not in existing:
                        merged[key]["products"].append(p)
                        existing[vid] = p
            else:
                merged[key] = g

    # 보강: 기본 주문목록(/order/list) 결과도 병합 (검색 결과 가변성 대응).
    # 검색 API가 간헐적으로 최근 묶음을 누락하는 경우 기본 목록에서 보완된다.
    try:
        base_groups = fetch_recent_order_groups(page, lookback_days=lookback_days)
        for g in base_groups or []:
            vids = frozenset(
                p.get("vendor_item_id", "")
                for p in g.get("products", [])
                if p.get("vendor_item_id")
            )
            key = (g["order_date"], vids)
            if key in merged:
                existing = {p["vendor_item_id"]: p for p in merged[key]["products"]}
                for p in g.get("products", []):
                    vid = p.get("vendor_item_id")
                    if vid and vid not in existing:
                        merged[key]["products"].append(p)
                        existing[vid] = p
            else:
                merged[key] = g
    except Exception:
        logger.warning("[Matcher:OrderSearch] 기본 목록 병합 실패 (계속)", exc_info=True)

    result = list(merged.values())
    logger.info(
        "[Matcher:OrderSearch] 병합 묶음 %d건 (검색 키워드 %d개 + 기본 목록)",
        len(result), len(item_names),
    )
    return result


def resolve_product_url(page, sdp_href: str) -> Optional[str]:
    """sdp/link URL → redirect 따라가 /vp/products/XXX 획득. 실패 시 None."""
    if not sdp_href or page is None:
        return None

    try:
        from modules.coupang_orderer import _wait_for_akamai_challenge_clear
    except Exception:
        def _wait_for_akamai_challenge_clear(p, max_sec=25):
            return True

    try:
        page.goto(sdp_href, wait_until="domcontentloaded", timeout=60000)
        _wait_for_akamai_challenge_clear(page, 25)
        final = page.url or ""
    except Exception:
        logger.exception("[Matcher] sdp/link goto 실패: %s", sdp_href[:80])
        return None

    if "/vp/products/" not in final:
        return None
    return final.split("?")[0]


# =============================================================
# 로직 ①②③ — 매핑 자동 학습
# =============================================================

def _generalize_product_title(detailed_title: str) -> str:
    """상세 상품명을 일반화된 canonical 이름으로 변환 (Gemini).

    예:
      "쓰리엠 프로그립 1000 안전장갑, 블랙, 1개, L"  → "쓰리엠 프로그립 안전장갑"
      "코멧 3중 방습 국산 참나무 장작, 10kg, 1개"   → "코멧 참나무장작"
      "곰곰 쌀과자 고소한맛, 294g, 1개"             → "곰곰 쌀과자"
      "네스프레소 버츄오 볼테소 캡슐커피, 5.2g, 10개입" → "네스프레소 버츄오 캡슐커피"

    규칙(프롬프트에 전달):
      - 제품명을 유지 (브랜드 + 핵심 명사)
      - 색상/사이즈/수량/용량/포장 단위 제거
      - 쉼표 앞 첫 구가 핵심. 그 안에서도 수량/용량 표현은 걷어냄
      - 2~4 토큰 권장, 한국어 기준 10~25자

    실패 시 원본 반환 (절삭 없음 — caller 가 판단).
    """
    cleaned = (detailed_title or "").strip()
    if not cleaned:
        return cleaned
    if genai is None:
        return cleaned

    api_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GEMINI_API_KEY_BACKUP", "")
    if not api_key:
        return cleaned

    prompt = (
        "다음 쿠팡 상세 상품명을 '일반화된 canonical 이름'으로 변환해.\n"
        "규칙:\n"
        "- 브랜드 + 핵심 명사 유지, 그 외 변형(색/사이즈/수량/용량/포장)은 제거\n"
        "- 쉼표 앞 구에서도 '1000' '500g' '10kg' 같은 단위/수량은 제거\n"
        "- 2~4 토큰 / 한국어 10~25자 권장\n"
        "- 결과는 canonical 이름 1개만, 설명/따옴표/접두어 없이\n\n"
        "예시:\n"
        "  '쓰리엠 프로그립 1000 안전장갑, 블랙, 1개, L' → 쓰리엠 프로그립 안전장갑\n"
        "  '코멧 3중 방습 국산 참나무 장작, 10kg, 1개' → 코멧 참나무장작\n"
        "  '곰곰 쌀과자 고소한맛, 294g, 1개' → 곰곰 쌀과자\n"
        "  '네스프레소 버츄오 볼테소 캡슐커피, 5.2g, 10개입, 1개' → 네스프레소 버츄오 캡슐커피\n"
        "  '청정원순창 양념쌈장, 190g, 6개' → 청정원 양념쌈장\n\n"
        f"입력: '{cleaned}'"
    )

    try:
        from modules.gemini_client import generate_content_with_fallback
        response = generate_content_with_fallback(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=32,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                http_options=types.HttpOptions(timeout=10000),
            ),
        )
        result = (getattr(response, "text", None) or "").strip()
        # 개행/따옴표/마침표 제거
        result = result.strip().strip("'\"`").strip(".,;:").strip()
        # 첫 줄만 사용 (Gemini가 여러 줄 반환 시)
        result = result.split("\n", 1)[0].strip()
        # 과하게 길면 원본 사용 (일반화 실패로 간주)
        if not result or len(result) > 40 or len(result) < 2:
            return cleaned
        return result
    except Exception:
        logger.warning("[Matcher] canonical 일반화 실패 — 원본 유지: %s", cleaned[:40])
        return cleaned


def _gemini_similarity_check(memo_item: str, product_title: str) -> dict:
    """'이 메모 품목과 이 주문 상품이 동일 제품인가?' Gemini 판정.

    반환: {"same": bool, "confidence": "high"|"medium"|"low", "reason": str}
    Gemini 미가용/오류 시: confidence="low" (안전 측으로 기본값)
    """
    cleaned_memo = (memo_item or "").strip()
    cleaned_title = (product_title or "").strip()
    if not cleaned_memo or not cleaned_title:
        return {"same": False, "confidence": "low", "reason": "입력 비어있음"}

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not _HAS_GENAI or not api_key:
        return {"same": False, "confidence": "low", "reason": "Gemini 미가용"}

    prompt = f"""다음 두 제품이 '동일한 제품'인지 판단:
- 메모에 적힌 이름: "{cleaned_memo}"
- 쿠팡 주문 상품명: "{cleaned_title}"

규칙:
- 같은 종류/브랜드/용량이면 same=true
- 서로 다른 브랜드, 사양 크게 다르면 same=false
- 모호하면 confidence="medium", 확실하면 "high", 일반어 매치 수준이면 "low"

JSON 한 줄로만 답변:"""

    schema = {
        "type": "OBJECT",
        "properties": {
            "same": {"type": "BOOLEAN"},
            "confidence": {"type": "STRING"},
            "reason": {"type": "STRING"},
        },
        "required": ["same", "confidence"],
    }
    try:
        from modules.gemini_client import generate_content_with_fallback
        response = generate_content_with_fallback(
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
            return {"same": False, "confidence": "low", "reason": "빈 응답"}
        parsed = json.loads(text)
        return {
            "same": bool(parsed.get("same", False)),
            "confidence": str(parsed.get("confidence", "low")).lower(),
            "reason": str(parsed.get("reason", ""))[:100],
        }
    except APIError as e:
        logger.warning("[Matcher:Sim] Gemini API 오류: %s", e)
        return {"same": False, "confidence": "low", "reason": "API 오류"}
    except Exception:
        logger.exception("[Matcher:Sim] 예외")
        return {"same": False, "confidence": "low", "reason": "예외"}


def _stock_ordered_last_n_days(n_days: int = 14) -> list[dict]:
    """stock_orders 최근 N일 (item_name, matched_url, status) 반환.

    ordered/skipped/failed 모두 포함 — 품절로 skip 된 품목도 실주문에서 대체 URL을
    발견하면 mapping_update 후보가 되므로 학습 대상에 넣음. unmapped 는 원래 매핑이
    없으니 제외.
    """
    from modules.db import get_connection
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT item_name, matched_url, status "
                "FROM stock_orders "
                "WHERE status IN ('ordered', 'skipped', 'failed') "
                "AND detected_at >= datetime('now', ?)",
                (f"-{int(n_days)} days",),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("[Matcher:Sync] stock_orders 조회 실패")
        return []


def _stock_unmapped_last_n_days(n_days: int = 14) -> list[str]:
    """stock_orders에서 최근 N일 'unmapped' 상태 메모 품목명 리스트."""
    from modules.db import get_connection
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT item_name "
                "FROM stock_orders "
                "WHERE status = 'unmapped' AND detected_at >= datetime('now', ?)",
                (f"-{int(n_days)} days",),
            )
            return [r["item_name"] for r in cur.fetchall() if r["item_name"]]
    except Exception:
        logger.exception("[Matcher:Sync] stock_orders unmapped 조회 실패")
        return []


def _stock_orders_pending_scan(min_age_days: int = 3) -> list[dict]:
    """min_age_days 경과 + scan_done_at IS NULL 인 레코드 반환.

    ordered/skipped/failed 모두 포함:
    - ordered: 실주문 확인 → confirmed_at, 없으면 disable 제안
    - skipped/failed: 품절/실패로 봇이 못 담았어도 사용자가 다른 상품으로 수동 주문한
                     경우 주문내역에 존재 → 발견 시 매핑 URL 업데이트 제안 (mapping_update)

    각 행: {id, item_name, matched_url, status, detected_at}
    """
    from modules.db import get_connection
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "SELECT id, item_name, matched_url, status, detected_at "
                "FROM stock_orders "
                "WHERE status IN ('ordered', 'skipped', 'failed') "
                "AND scan_done_at IS NULL "
                "AND detected_at <= datetime('now', ?)",
                (f"-{int(min_age_days)} days",),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("[Matcher:Sync] 미확정 레코드 조회 실패")
        return []


def _mark_scan_result(confirmed_ids: list[int], unconfirmed_ids: list[int]) -> None:
    """스캔 결과를 stock_orders 에 반영.

    confirmed_ids : confirmed_at + scan_done_at 모두 세팅 (실주문 확인)
    unconfirmed_ids : scan_done_at 만 세팅 (재스캔 방지, 확인 실패)
    """
    from modules.db import get_connection
    if not confirmed_ids and not unconfirmed_ids:
        return
    try:
        with get_connection() as conn:
            if confirmed_ids:
                placeholders = ",".join(["?"] * len(confirmed_ids))
                conn.execute(
                    f"UPDATE stock_orders "
                    f"SET confirmed_at = datetime('now'), scan_done_at = datetime('now') "
                    f"WHERE id IN ({placeholders})",
                    confirmed_ids,
                )
            if unconfirmed_ids:
                placeholders = ",".join(["?"] * len(unconfirmed_ids))
                conn.execute(
                    f"UPDATE stock_orders "
                    f"SET scan_done_at = datetime('now') "
                    f"WHERE id IN ({placeholders})",
                    unconfirmed_ids,
                )
            conn.commit()
    except Exception:
        logger.exception("[Matcher:Sync] scan 결과 저장 실패")


def _save_mapping(mapping: dict) -> bool:
    """load_mapping() 이 반환한 구조를 원본 파일 포맷으로 역변환하여 저장.

    원본 파일 포맷:
        {
          "_skip_items": {...},
          "크라운 참쌀설병": {...},
          "스파클라": {...},
          ...
        }

    메모리 구조 (load_mapping 반환):
        {
          "items": {canonical: body, ...},
          "alias_index": {...},   # 파생 — 저장 불필요
          "skip_items": {...},    # → "_skip_items" 키로 저장
          "skip_index": {...},    # 파생 — 저장 불필요
        }

    주의: 직접 mapping 을 json.dumps 하면 파일이 {"items":...,"alias_index":...}
    포맷으로 저장되고, 다음 load_mapping() 호출 시 "items"가 canonical 로 오해됨 → 매핑 0개.
    """
    try:
        raw: dict = {}
        skip_items = mapping.get("skip_items", {}) or {}
        if skip_items:
            raw["_skip_items"] = skip_items
        for name, body in (mapping.get("items", {}) or {}).items():
            if isinstance(name, str) and isinstance(body, dict):
                raw[name] = body

        backup = _MAPPING_PATH.with_suffix(".json.bak")
        if _MAPPING_PATH.exists():
            backup.write_text(_MAPPING_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        _MAPPING_PATH.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return True
    except Exception:
        logger.exception("[Matcher:Sync] 매핑 저장 실패")
        return False


def sync_mapping_from_orders(page, mode: str = "instant", lookback_days: int = 14) -> dict:
    """로직 ①②③ 통합 실행.

    mode:
      "instant"   — 메모 처리 직후. 로직 ②: 최근 lookback_days 기간 stock_orders 품목명을
                    키워드로 주문내역 검색 → 동일 사업 묶음에서 새 상품 발견 시 매핑 자동 추가.
                    confirmed_at/scan_done_at 은 건드리지 않음.
      "scheduled" — 매일 07시 1회. 로직 ①③:
                    * 3일+ 경과 scan_done_at NULL 레코드 (ordered/skipped/failed) 를 대상
                    * 각 item_name 으로 주문내역 검색 → 묶음 수집
                    * 매치 발견 → confirmed_at 세팅. 미발견 → scan_done_at 만
                    * 새 상품(매핑 없음) 발견 시 매핑 자동 추가
                    * skipped/failed 품목의 매핑 URL과 실주문 URL 이 다르면 mapping_update 승인 대기
                    * 자동주문_허용=True 인 미발견 품목은 mapping_disable 승인 대기

    반환: {"added":[], "updated":[], "pending":[], "confirmed":[], "unconfirmed":[]}
    """
    result = {"added": [], "updated": [], "pending": [], "confirmed": [], "unconfirmed": []}

    # --- 모드별 대상 집합 결정 ---
    name_to_ids: dict[str, list[int]] = {}
    # skipped/failed 품목 — mapping_update 후보 판정용 (인덱스: item_name → 현재 매핑 URL)
    skipped_names_to_current_url: dict[str, str] = {}

    if mode == "scheduled":
        pending_records = _stock_orders_pending_scan(min_age_days=3)
        if not pending_records:
            logger.info("[Matcher:Sync] 3일+ 미확정 레코드 없음 — 스킵")
            return result
        for r in pending_records:
            nm = r.get("item_name")
            if nm:
                name_to_ids.setdefault(nm, []).append(r["id"])
                if r.get("status") in ("skipped", "failed"):
                    # 가장 마지막 matched_url 유지 (동일 name 여러 건)
                    if r.get("matched_url"):
                        skipped_names_to_current_url[nm] = r["matched_url"]
        ordered_names = set(name_to_ids.keys())
        if not ordered_names:
            logger.info("[Matcher:Sync] 미확정 레코드에 item_name 없음 — 스킵")
            return result
    else:  # instant
        ordered = _stock_ordered_last_n_days(lookback_days)
        if not ordered:
            logger.info("[Matcher:Sync] 최근 %d일 stock 기록 없음 — 스킵", lookback_days)
            return result
        ordered_names = {o["item_name"] for o in ordered if o.get("item_name")}
        for o in ordered:
            if o.get("status") in ("skipped", "failed") and o.get("item_name") and o.get("matched_url"):
                skipped_names_to_current_url[o["item_name"]] = o["matched_url"]

    # --- 검색 기반 묶음 수집 (기본 페이지 5건 한계 회피) ---
    groups = find_orders_containing_items(page, sorted(ordered_names), lookback_days=lookback_days)
    if not groups:
        logger.info("[Matcher:Sync] 검색 기반 묶음 0건")
        if mode == "scheduled":
            all_ids = [rid for ids in name_to_ids.values() for rid in ids]
            _mark_scan_result([], all_ids)
            result["unconfirmed"] = sorted(ordered_names)
        return result

    mapping = load_mapping()
    items = mapping.setdefault("items", {})
    alias_index = mapping.setdefault("alias_index", {})
    unmapped_memo_items = _stock_unmapped_last_n_days(lookback_days)

    confirmed_names: set[str] = set()
    # mapping_update 후보 중복 방지 (item_name → best url 후보 1회만 제안)
    update_proposed: set[str] = set()

    # --- 사업용 묶음 선별 + 신규 매핑 + mapping_update 탐지 ---
    for group in groups:
        order_date = group["order_date"]
        products = group["products"]

        matched_names: set[str] = set()
        matched_product_by_name: dict[str, dict] = {}  # name → 매치된 product dict
        unmatched_products = []

        # Phase 1: 어순·단위 무관 매칭 (substring OR 바이그램 교집합)
        # — "커피캡슐" ↔ "네스프레소 버츄오 볼테소 캡슐커피" 같은 어순차 자동 인식
        for prod in products:
            title = prod["title"]
            matched_here = None
            for name in ordered_names:
                if name and token_overlap_match(name, title):
                    matched_here = name
                    break
            if matched_here:
                matched_names.add(matched_here)
                matched_product_by_name.setdefault(matched_here, prod)
            else:
                unmatched_products.append(prod)

        # Phase 2: Gemini similarity — 이미 사업용으로 확정된 묶음(substring 매치 있음)에서만.
        # skipped/failed 품목은 봇이 담기 실패했으므로 "커피캡슐" 같은 축약형이
        # 실주문 타이틀 "네스프레소 버츄오 볼테소 캡슐커피"와 substring 매치 안 됨.
        # 유사도로 보완해 mapping_update 후보로 연결.
        if matched_names and skipped_names_to_current_url:
            still_unmatched = []
            for prod in unmatched_products:
                similar_skipped = None
                for skipped_name in skipped_names_to_current_url:
                    if skipped_name in matched_names:
                        continue  # 이미 substring 매치됨
                    sim = _gemini_similarity_check(skipped_name, prod["title"])
                    if sim["same"] and sim["confidence"] in ("high", "medium"):
                        similar_skipped = skipped_name
                        logger.info(
                            "[Matcher:Sync] Gemini 유사 매칭: '%s' ≈ '%s' (%s)",
                            skipped_name, prod["title"][:40], sim["confidence"],
                        )
                        break
                if similar_skipped:
                    matched_names.add(similar_skipped)
                    matched_product_by_name.setdefault(similar_skipped, prod)
                else:
                    still_unmatched.append(prod)
            unmatched_products = still_unmatched

        if not matched_names:
            continue  # 순수 개인 주문

        confirmed_names.update(matched_names)
        logger.info("[Matcher:Sync] 사업용 주문 [%s] 확인=%d, 새 상품=%d",
                    order_date, len(matched_names), len(unmatched_products))

        # === mapping_update 제안 (skipped/failed 품목에서 실제 주문된 상품 URL 발견) ===
        for name in matched_names:
            if name in update_proposed:
                continue
            current_url = skipped_names_to_current_url.get(name)
            if not current_url:
                continue
            prod = matched_product_by_name.get(name)
            if not prod:
                continue
            new_url = resolve_product_url(page, prod["sdp_href"])
            if not new_url:
                continue
            # URL 비교 (파라미터 제거 후)
            cur_clean = current_url.split("?")[0].rstrip("/")
            new_clean = new_url.split("?")[0].rstrip("/")
            if cur_clean == new_clean:
                continue  # 동일 상품, 업데이트 불필요
            # mapping_update 승인 대기
            from modules.discord_bot import add_pending
            pid = add_pending(
                item_type="mapping_update",
                memo_item=name,
                current_url=current_url,
                suggested_url=new_url,
                reason=(
                    f"'{name}' 봇은 담기 실패/품절. "
                    f"실주문({order_date})에 '{prod['title'][:50]}' 발견 — URL 교체?"
                ),
            )
            result["pending"].append({
                "id": pid, "title": name, "action": "update",
                "current_url": current_url, "suggested_url": new_url,
            })
            result["updated"].append({
                "title": name, "current_url": current_url, "suggested_url": new_url,
                "order_date": order_date,
            })
            update_proposed.add(name)
            logger.info("[Matcher:Sync] mapping_update 대기: #%d %s", pid, name[:40])

        # === 로직 ① — 신규 상품 자동 매핑 (사업용 확정 묶음) ===
        for prod in unmatched_products:
            title = prod["title"]
            if not title:
                continue

            # 상세 title → 일반화 canonical (Gemini)
            # 동일 브랜드·제품의 색/사이즈/맛 변형이 같은 canonical 로 수렴하도록 함.
            canonical = _generalize_product_title(title)

            # URL 선해석 — variants 추가/신규 공통으로 필요
            clean_url = resolve_product_url(page, prod["sdp_href"])
            if not clean_url:
                logger.warning("[Matcher:Sync] URL 해석 실패 — 매핑 스킵: %s", title[:40])
                continue

            # 일반화 결과가 이미 매핑에 있으면 variants 에 추가 (같은 메모 → 여러 URL 주문)
            if canonical in items:
                entry = items[canonical]
                entry_url_base = (entry.get("url", "") or "").split("?")[0].rstrip("/")
                new_url_base = clean_url.split("?")[0].rstrip("/")

                # 동일 URL 이면 중복 저장 불필요
                if entry_url_base == new_url_base:
                    continue

                # variants 초기화 (첫 확장 시 기존 url 을 첫 variant 로 이동)
                variants = entry.get("variants") or []
                if not variants:
                    variants = [{
                        "url": entry.get("url", ""),
                        "name": entry.get("상품명", canonical),
                        "기본수량": int(entry.get("기본수량", 1) or 1),
                    }]
                # 이미 동일 URL 의 variant 가 있으면 스킵
                if any((v.get("url", "").split("?")[0].rstrip("/")) == new_url_base for v in variants):
                    continue
                variants.append({
                    "url": clean_url,
                    "name": title,
                    "기본수량": 1,
                })
                entry["variants"] = variants

                # 별칭·alias_index 동기화 (원본 title 이 메모와 매치 가능하도록)
                existing_aliases = set(entry.get("별칭", []) or [])
                if title not in existing_aliases:
                    existing_aliases.add(title)
                    entry["별칭"] = sorted(existing_aliases)
                alias_index[title.lower()] = canonical
                result["added"].append({
                    "title": canonical, "original": title,
                    "url": clean_url, "as_variant": True,
                    "order_date": order_date,
                })
                logger.info("[Matcher:Sync] 변형 추가 '%s' → variants=%d개 (신규: %s)",
                            canonical[:40], len(variants), title[:50])
                continue

            # 원본 title 자체가 이미 매핑 키로 있으면 스킵 (중복 방지)
            if title in items:
                continue

            best_alias = None
            best_conf = None
            for memo_item in unmapped_memo_items:
                sim = _gemini_similarity_check(memo_item, title)
                if sim["same"] and sim["confidence"] in ("high", "medium"):
                    best_alias = memo_item
                    best_conf = sim["confidence"]
                    break

            # 원본 title 은 별칭으로 포함 — 후속 매칭에 도움
            aliases = [title]
            if best_alias and best_alias != title:
                aliases.append(best_alias)

            new_entry = {
                "url": clean_url,
                "상품명": title,  # 상세 원본 보존
                "기본수량": 1,
                "최대가격": 0,
                "최근주문일": order_date,
                "주문횟수": 1,
                "카테고리": "기타",
                "분류": "기타",
                "자동주문_허용": True,
                "별칭": aliases,
                "그룹_후보": [],
                "옵션": {},
                # variants 는 두 번째 변형이 들어올 때 동적으로 생성 (위 분기 참고)
            }
            items[canonical] = new_entry
            alias_index[canonical.lower()] = canonical
            for a in aliases:
                alias_index[a.lower()] = canonical
            result["added"].append({
                "title": canonical,  # 요약 알림에선 canonical 표시
                "original": title,
                "url": clean_url,
                "alias": best_alias, "confidence": best_conf,
                "order_date": order_date,
            })
            logger.info("[Matcher:Sync] 신규 매핑 추가: '%s' ← '%s' (메모별칭=%s)",
                        canonical[:40], title[:40], best_alias)

    # --- 로직 ③ + 스캔 결과 기록 (scheduled 모드만) ---
    if mode == "scheduled":
        unconfirmed_names = ordered_names - confirmed_names
        for name in sorted(unconfirmed_names):
            entry = items.get(name)
            if not entry or not entry.get("자동주문_허용", True):
                continue
            from modules.discord_bot import add_pending
            pid = add_pending(
                item_type="mapping_disable",
                memo_item=name,
                current_url=entry.get("url", ""),
                reason="3일 경과 — 장바구니에 담았으나 결제 미완료. 자동주문 비활성화?",
            )
            result["pending"].append({"id": pid, "title": name, "action": "disable"})
            logger.info("[Matcher:Sync] disable 승인 대기: #%d %s", pid, name[:40])

        confirmed_ids: list[int] = []
        unconfirmed_ids: list[int] = []
        for name, ids in name_to_ids.items():
            if name in confirmed_names:
                confirmed_ids.extend(ids)
            else:
                unconfirmed_ids.extend(ids)
        _mark_scan_result(confirmed_ids, unconfirmed_ids)
        result["confirmed"] = sorted(confirmed_names)
        result["unconfirmed"] = sorted(unconfirmed_names)

    # --- 매핑 저장 + 디스코드 요약 ---
    if result["added"]:
        _save_mapping(mapping)

    if any([result["added"], result["pending"], result["confirmed"], result["updated"]]):
        try:
            from modules.notifier import _send_discord_webhook
            lines = [f"**매핑 자동 학습 — mode={mode}**"]
            if result["confirmed"]:
                lines.append(f"\n실주문 확인 {len(result['confirmed'])}건:")
                for name in result["confirmed"][:10]:
                    lines.append(f"  • {name[:60]}")
            if result["added"]:
                lines.append(f"\n신규 매핑 {len(result['added'])}건:")
                for a in result["added"][:10]:
                    alias_str = f" (별칭: {a['alias']})" if a.get("alias") else ""
                    lines.append(f"  • {a['title'][:60]}{alias_str}")
            if result["updated"]:
                lines.append(f"\nURL 교체 제안 {len(result['updated'])}건:")
                for u in result["updated"][:10]:
                    lines.append(f"  • {u['title'][:40]} → 실주문 URL")
            if result["pending"]:
                lines.append(f"\n승인 대기 {len(result['pending'])}건 — `/list` 후 `/approve <id>`")
                for p in result["pending"][:10]:
                    lines.append(f"  • #{p['id']} [{p.get('action', '?')}] {p.get('title', '')[:50]}")
            _send_discord_webhook("\n".join(lines))
        except Exception:
            logger.exception("[Matcher:Sync] 디스코드 요약 알림 실패")

    return result

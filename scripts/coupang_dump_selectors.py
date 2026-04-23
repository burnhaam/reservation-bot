"""
쿠팡 상품 상세 페이지에서 '가격으로 보이는' DOM 엘리먼트들의 셀렉터를 덤프.

modules/coupang_orderer._extract_current_price() 의 셀렉터 목록이 노후되어
실제 가격을 못 찾을 때, 현재 페이지 DOM을 분석해 갱신용 후보를 출력한다.

기준: 텍스트가 "3,490원" 또는 "3490" 같은 가격 패턴인 leaf 엘리먼트.

사용:
  python scripts/coupang_dump_selectors.py [URL]
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DEFAULT_URL = "https://www.coupang.com/vp/products/8304592330"

_JS_FIND_PRICES = r"""
() => {
  // 텍스트 노드 순회해서 가격스러운 조각을 담고 있는 부모 엘리먼트 수집.
  // leaf 제약 없음 — <strong>3,490</strong><span>원</span> 같은 분할 구조도 포착.
  const out = [];
  const re = /\d{1,3}(,\d{3})+\s*원?|\d{3,6}\s*원/;  // 3,490 / 3490원 / 63,600원
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const raw = (node.nodeValue || '').trim();
    if (!raw || raw.length > 30) continue;
    if (!re.test(raw)) continue;
    const el = node.parentElement;
    if (!el) continue;
    // 자기 + 조상 4대까지 path
    const chain = [];
    let cur = el;
    for (let i = 0; i < 5 && cur && cur.tagName; i++) {
      let part = cur.tagName.toLowerCase();
      if (cur.id) part += '#' + cur.id;
      if (cur.className && typeof cur.className === 'string') {
        const cls = cur.className.trim().split(/\s+/).filter(Boolean).slice(0, 3).join('.');
        if (cls) part += '.' + cls;
      }
      chain.unshift(part);
      cur = cur.parentElement;
    }
    const path = chain.join(' > ');
    const key = path + '|' + raw;
    if (seen.has(key)) continue;
    seen.add(key);
    // 부모 전체 innerText도 같이 반환 (조립 텍스트 확인용)
    const innerText = (el.innerText || '').trim().slice(0, 60);
    out.push({ text: raw, inner: innerText, path });
    if (out.length >= 80) break;
  }
  return out;
}
"""


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_URL
    print(f"[정보] 대상: {url}\n")

    from modules.coupang_orderer import init_browser, close_browser, is_session_valid

    p, browser, context, page = init_browser()
    try:
        if not is_session_valid(page):
            print("[중단] 세션 무효")
            return

        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Akamai behavioral challenge 자동 통과 대기.
        print("[대기] 페이지 본문 로드까지 최대 30초 대기...")
        last_body = ""
        for i in range(30):
            try:
                state = page.evaluate(r"""
                    () => ({
                        bodyLen: (document.body?.innerText || '').length,
                        bodyText: (document.body?.innerText || '').slice(0, 500),
                        hasChallenge: !!document.getElementById('sec-if-cpt-container'),
                        title: document.title,
                        url: location.href
                    })
                """)
            except Exception as e:
                print(f"  [{i+1}s] evaluate 실패 (타겟 종료): {e}")
                break
            last_body = state["bodyText"]
            if state["bodyLen"] > 500 and not state["hasChallenge"]:
                print(f"  [{i+1}s] 본문 로드 완료 (body={state['bodyLen']}, title='{state['title'][:40]}')")
                break
            if i % 3 == 0:
                marker = "챌린지" if state["hasChallenge"] else "로딩"
                print(f"  [{i+1}s] {marker}={state['hasChallenge']} body={state['bodyLen']} url={state['url'][:60]}")
            time.sleep(1)
        else:
            print("  [시간초과] 30초 내 본문 로드 실패")

        # 최종 본문 스냅샷 (진단용)
        print("\n=== 최종 본문 (첫 500자) ===")
        print(last_body)
        print("=" * 30)

        # 상세 HTML 덤프 (항상)
        try:
            html = page.content()
            html_path = PROJECT_ROOT / "logs" / "coupang_dump.html"
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html, encoding="utf-8")
            print(f"[덤프] {html_path} ({len(html)} bytes)")
        except Exception as e:
            print(f"[덤프실패] {e}")

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # 기본 진단
        diag = page.evaluate(r"""
            () => ({
                title: document.title,
                url: location.href,
                bodyLen: (document.body?.innerText || '').length,
                bodySample: (document.body?.innerText || '').slice(0, 400),
                wonCount: ((document.body?.innerText || '').match(/원/g) || []).length,
                digitRuns: ((document.body?.innerText || '').match(/\d{3,}/g) || []).slice(0, 20),
                htmlLen: document.documentElement.outerHTML.length
            })
        """)
        print("=== 페이지 진단 ===")
        print(f"  title     : {diag['title']}")
        print(f"  url       : {diag['url']}")
        print(f"  body len  : {diag['bodyLen']}")
        print(f"  html len  : {diag['htmlLen']}")
        print(f"  원 count  : {diag['wonCount']}")
        print(f"  숫자 샘플 : {diag['digitRuns']}")
        print(f"  body 앞부분:\n    {diag['bodySample'][:300]}")
        print()

        candidates = page.evaluate(_JS_FIND_PRICES)
        if not candidates:
            print("[경고] 가격 패턴 텍스트 leaf를 찾지 못함.")
            print("       페이지 가격이 동적 로드 중이거나 패턴 불일치 가능.")
            # HTML 일부를 파일로 덤프 — 수동 분석용
            html_path = PROJECT_ROOT / "logs" / "coupang_dump.html"
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(page.content(), encoding="utf-8")
            print(f"       전체 HTML 덤프: {html_path}")
            return

        print(f"=== 가격 후보 {len(candidates)}개 ===\n")
        seen_paths = set()
        for c in candidates:
            text = c["text"]
            inner = c.get("inner", "")
            path = c["path"]
            if path in seen_paths:
                continue
            seen_paths.add(path)
            print(f"  [{text:>10}] ({inner[:25]:<25}) {path}")

        # 통계: 클래스 빈도 (말단 엘리먼트만)
        print("\n=== 말단 클래스 빈도 ===")
        from collections import Counter
        leaf_tokens = Counter()
        for c in candidates:
            tail = c["path"].rsplit(" > ", 1)[-1]
            leaf_tokens[tail] += 1
        for token, n in leaf_tokens.most_common(15):
            print(f"  {n:>3}x  {token}")
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

"""
쿠팡 마이쿠팡 주문내역 검색 DOM 탐색 (recon).

product_matcher.match_from_order_history 구현 전에, 실제 주문내역 검색 UI의
URL 쿼리 파라미터, 검색 결과 DOM 구조를 파악한다.

테스트 키워드: 사용자가 최근 주문한 '빈츠'. 이 품목이 주문내역에 존재해야 함.
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    from modules.coupang_orderer import init_browser, close_browser, is_session_valid, _wait_for_akamai_challenge_clear

    keyword = sys.argv[1] if len(sys.argv) > 1 else "빈츠"
    p, browser, context, page = init_browser()
    try:
        if not is_session_valid(page):
            print("[중단] 세션 무효")
            return

        # 1) 주문내역 기본 페이지로 이동
        list_url = "https://mc.coupang.com/ssr/desktop/order/list"
        print(f"\n[1] 기본 페이지 goto: {list_url}")
        page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        _wait_for_akamai_challenge_clear(page, max_sec=25)
        time.sleep(2)

        print(f"   현재 URL: {page.url}")
        print(f"   title   : {page.title()[:60]}")

        # 2) 검색 입력창 / 버튼 후보 덤프
        print("\n[2] 검색 입력창 및 제출 버튼 후보 탐색...")
        search_inputs = page.evaluate(r"""
            () => {
                const inputs = Array.from(document.querySelectorAll('input[type="text"], input[type="search"], input:not([type])'));
                return inputs.map(el => ({
                    id: el.id, name: el.name, cls: el.className,
                    placeholder: el.placeholder, visible: el.offsetParent !== null,
                    form: el.form ? (el.form.id || el.form.className) : null
                })).filter(x => x.visible).slice(0, 20);
            }
        """)
        for i, inp in enumerate(search_inputs):
            print(f"   [{i}] placeholder={inp['placeholder']!r} id={inp['id']!r} name={inp['name']!r} cls={inp['cls'][:60]!r}")

        # 3) 실제 검색창에 입력 → 엔터 방식 (JS 기반 검색 지원)
        print(f"\n[3] 검색창 직접 입력: '{keyword}'")
        try:
            search_input = page.locator("input[placeholder*='주문한 상품']").first
            search_input.click()
            time.sleep(0.5)
            search_input.fill(keyword)
            time.sleep(0.3)
            search_input.press("Enter")
            _wait_for_akamai_challenge_clear(page, max_sec=25)
            # 검색 결과 렌더링 대기
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(2)
            print(f"   검색 후 URL: {page.url[:100]}")
        except Exception as e:
            print(f"   검색창 입력 실패: {e}")

        # 4) 검색 결과 DOM 탐색
        print("\n[4] 검색 결과 페이지의 상품 링크/이미지 후보 탐색...")
        # 4-a) 키워드 텍스트를 포함하는 엘리먼트
        print(f"   [4a] 키워드 '{keyword}' 텍스트 포함 엘리먼트:")
        text_els = page.evaluate(f"""
            () => {{
                const kw = {keyword!r};
                const out = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {{
                    if (el.children.length !== 0) continue;
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t.length === 0 || t.length > 80) continue;
                    if (!t.includes(kw)) continue;
                    const chain = [];
                    let cur = el;
                    for (let i = 0; i < 5 && cur && cur.tagName; i++) {{
                        let part = cur.tagName.toLowerCase();
                        if (cur.id) part += '#' + cur.id;
                        if (cur.className && typeof cur.className === 'string') {{
                            const c = cur.className.trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.');
                            if (c) part += '.' + c;
                        }}
                        chain.unshift(part);
                        cur = cur.parentElement;
                    }}
                    out.push({{ text: t, path: chain.join(' > ') }});
                    if (out.length >= 10) break;
                }}
                return out;
            }}
        """)
        for i, el in enumerate(text_els):
            print(f"      [{i}] '{el['text'][:50]}'")
            print(f"          {el['path']}")

        # 4-b) /vp/products/ 링크
        print("\n   [4b] /vp/products/ 링크:")
        candidates = page.evaluate(r"""
            () => {
                const out = [];
                // a 태그 중 상품 상세 페이지로 가는 링크 (/vp/products/)
                const links = document.querySelectorAll('a[href*="/vp/products/"]');
                for (const a of links) {
                    const text = (a.innerText || a.textContent || '').trim().slice(0, 60);
                    const href = a.href;
                    // 부모 체인에서 가격/제품 정보 엘리먼트 탐색
                    let nearText = '';
                    let parent = a.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const t = (parent.innerText || '').trim();
                        if (t.length > nearText.length && t.length < 300) {
                            nearText = t;
                        }
                        parent = parent.parentElement;
                    }
                    out.push({
                        text, href,
                        nearText: nearText.slice(0, 200),
                        cls: a.className || '',
                        path: (() => {
                            const chain = [];
                            let cur = a;
                            for (let i = 0; i < 4 && cur && cur.tagName; i++) {
                                let part = cur.tagName.toLowerCase();
                                if (cur.id) part += '#' + cur.id;
                                if (cur.className && typeof cur.className === 'string') {
                                    const c = cur.className.trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
                                    if (c) part += '.' + c;
                                }
                                chain.unshift(part);
                                cur = cur.parentElement;
                            }
                            return chain.join(' > ');
                        })()
                    });
                    if (out.length >= 10) break;
                }
                return out;
            }
        """)
        print(f"   상품 링크 {len(candidates)}개:")
        for i, c in enumerate(candidates):
            print(f"\n   [{i}] text='{c['text']}'")
            print(f"       href: {c['href']}")
            print(f"       path: {c['path']}")
            print(f"       near: {c['nearText'][:150]}")

        # 5) HTML 덤프
        html_path = PROJECT_ROOT / "logs" / "coupang_order_recon.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(page.content(), encoding="utf-8")
        print(f"\n[5] 전체 HTML 덤프: {html_path} ({html_path.stat().st_size} bytes)")

    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

"""주문내역 검색 결과 카드의 앵커 href + 가격 요소 상세 탐색."""
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

        # 검색 URL 직접 goto (이전 recon에서 확인)
        url = f"https://mc.coupang.com/ssr/desktop/order/list?isSearch=true&keyword={keyword}"
        print(f"[goto] {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _wait_for_akamai_challenge_clear(page, max_sec=25)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        # 전체 <a> 태그의 href 수집해서 쿠팡 상품 URL 패턴 찾기
        result = page.evaluate(rf"""
            () => {{
                const kw = {keyword!r};
                const anchors = Array.from(document.querySelectorAll('a'));
                const out = [];
                for (const a of anchors) {{
                    const href = a.href || '';
                    const text = (a.innerText || a.textContent || '').trim();
                    // 상품 관련 href + 키워드 포함 텍스트만
                    if (!text.includes(kw)) continue;
                    out.push({{
                        href: href,
                        text: text.slice(0, 80),
                        cls: (typeof a.className === 'string' ? a.className : '').slice(0, 100),
                        hasVpProducts: href.includes('/vp/products/'),
                        hasOrderDetail: href.includes('order') || href.includes('Order'),
                    }});
                }}
                return out;
            }}
        """)

        print(f"\n[결과] '{keyword}' 포함 앵커 {len(result)}개:\n")
        for i, r in enumerate(result):
            print(f"  [{i}] text: {r['text'][:60]}")
            print(f"      href: {r['href']}")
            print(f"      cls : {r['cls']}")
            print(f"      vp={r['hasVpProducts']} order={r['hasOrderDetail']}")
            print()

        # 검색 결과 카드 전체 구조 파악 — 반복되는 컨테이너 찾기
        print("\n[카드 컨테이너 패턴 탐색]")
        # sc-d4252421-5 같은 top-level 컨테이너가 카드마다 있을 것
        containers = page.evaluate(r"""
            () => {
                // 주요 주문내역 리스트의 반복 구조 찾기:
                // 같은 class를 가진 div가 N개 이상 모여있는 영역
                const classCounts = {};
                document.querySelectorAll('div').forEach(d => {
                    if (!d.className || typeof d.className !== 'string') return;
                    const cls = d.className.trim().split(/\s+/).filter(c => c.startsWith('sc-')).join('.');
                    if (!cls) return;
                    classCounts[cls] = (classCounts[cls] || 0) + 1;
                });
                // 2~20개 반복되는 클래스가 리스트 아이템 후보
                return Object.entries(classCounts)
                    .filter(([cls, n]) => n >= 2 && n <= 20)
                    .sort((a,b) => b[1] - a[1])
                    .slice(0, 10)
                    .map(([cls, n]) => ({ cls, count: n }));
            }
        """)
        for c in containers:
            print(f"   {c['count']:>3}회 반복: div.{c['cls']}")

        # 가격 후보 (원 단위 포함 텍스트)
        print("\n[가격 후보]")
        prices = page.evaluate(r"""
            () => {
                const out = [];
                const re = /\d{1,3}(,\d{3})*\s*원/;
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                const seen = new Set();
                while (walker.nextNode()) {
                    const t = (walker.currentNode.nodeValue || '').trim();
                    if (t.length > 30 || !re.test(t)) continue;
                    const parent = walker.currentNode.parentElement;
                    if (!parent) continue;
                    const chain = [];
                    let cur = parent;
                    for (let i = 0; i < 4 && cur && cur.tagName; i++) {
                        let part = cur.tagName.toLowerCase();
                        if (cur.className && typeof cur.className === 'string') {
                            const c = cur.className.trim().split(/\s+/).filter(c => c.startsWith('sc-')).slice(0, 2).join('.');
                            if (c) part += '.' + c;
                        }
                        chain.unshift(part);
                        cur = cur.parentElement;
                    }
                    const path = chain.join(' > ');
                    const key = path + '|' + t;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({ text: t, path });
                    if (out.length >= 15) break;
                }
                return out;
            }
        """)
        for i, p in enumerate(prices):
            print(f"   [{i}] {p['text']:<12}  {p['path']}")

    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

"""주문내역 페이지 pageNum / page 파라미터 동작 확인."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING)

from modules.coupang_orderer import (  # noqa: E402
    init_browser,
    close_browser,
    is_session_valid,
    _is_cdp_available,
)


def count_dates_on_page(page) -> tuple[int, list[str]]:
    return page.evaluate(r"""
        () => {
            const DATE_RE = /^\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\s*주문/;
            const dates = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
                const t = (walker.currentNode.nodeValue || '').trim();
                const m = DATE_RE.exec(t);
                if (m) dates.push(`${m[1]}-${m[2]}-${m[3]}`);
            }
            return [dates.length, dates.slice(0, 10)];
        }
    """)


def main():
    if not _is_cdp_available():
        print("CDP 미가용")
        return 1

    p, browser, ctx, page = init_browser()
    try:
        if not is_session_valid(page):
            print("세션 무효")
            return 1

        # 여러 URL 파라미터 테스트
        # 구버전/대체 URL 테스트
        print("\n[대체 URL 실험]")
        test_urls = [
            "https://mc.coupang.com/mypage/order/list",
            "https://www.coupang.com/mypage/order/list",
            "https://mcus.coupang.com/order/list",
            "https://mc.coupang.com/order/list",
            "https://mc.coupang.com/ssr/desktop/my/orderlist",
            "https://mc.coupang.com/ssr/desktop/my/orders",
            "https://mc.coupang.com/ssr/desktop/order/allList",
            "https://mc.coupang.com/ssr/desktop/order/listAll",
        ]
        for tu in test_urls:
            try:
                page.goto(tu, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                cur_url = page.url[:100]
                n_t, s_t = count_dates_on_page(page)
                print(f"  {tu[-40:]} → cur={cur_url[-40:]} / 날짜 {n_t}개")
            except Exception as e:
                print(f"  FAIL {tu}: {str(e)[:60]}")

        # 1) 기본 페이지
        url = "https://mc.coupang.com/ssr/desktop/order/list"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        n, sample = count_dates_on_page(page)
        print(f"[초기] {n}개 날짜: {sample[:10]}")

        # 1.5) 기간 필터 버튼 찾기 (1개월/3개월/6개월)
        print("\n[기간 필터 버튼]")
        page.goto("https://mc.coupang.com/ssr/desktop/order/list", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        filters = page.evaluate(r"""
            () => {
                const out = [];
                const all = document.querySelectorAll('button, a, li, div, span');
                for (const el of all) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (!t || t.length > 20) continue;
                    if (/(1|3|6)\s*개?월|\d+\s*년|\d+일|전체\s*주문/.test(t)) {
                        out.push({
                            tag: el.tagName,
                            text: t,
                            href: (el.href || '').slice(0, 80),
                            click: typeof el.click === 'function',
                        });
                    }
                }
                // 중복 제거 (text+href 기준)
                const seen = new Set();
                return out.filter(o => {
                    const k = o.text + '|' + o.href;
                    if (seen.has(k)) return false;
                    seen.add(k); return true;
                }).slice(0, 15);
            }
        """)
        print(f"기간 필터 후보 {len(filters)}개:")
        for f in filters:
            print(f"  [{f['tag']}] '{f['text']}' href={f.get('href', '')[:50]}")

        # 2) "더보기" 버튼 href 추출 + 네비게이션
        print("\n[DOM 분석 - 더보기 href]")
        page.goto("https://mc.coupang.com/ssr/desktop/order/list", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        buttons = page.evaluate(r"""
            () => {
                const candidates = [];
                const all = document.querySelectorAll('a');
                for (const el of all) {
                    const t = (el.innerText || el.textContent || '').trim();
                    if (!t || t.length > 30) continue;
                    if (/더\s*보기|전체\s*보기|주문\s*목록|다음/.test(t)) {
                        candidates.push({
                            text: t.slice(0, 40),
                            href: (el.href || '').slice(0, 200),
                        });
                    }
                }
                return candidates.slice(0, 15);
            }
        """)
        print(f"더보기/전체보기 링크 {len(buttons)}개:")
        seen_href = set()
        for b in buttons:
            if b.get('href') and b['href'] not in seen_href:
                seen_href.add(b['href'])
                print(f"  {b['text']}: {b['href']}")

        # 3) 첫 번째 더보기 href로 이동해 날짜 개수 재측정
        if seen_href:
            first_href = list(seen_href)[0]
            print(f"\n[첫 href 이동: {first_href}]")
            page.goto(first_href, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            n3, s3 = count_dates_on_page(page)
            print(f"  → {n3}개 날짜")

        # 3) 스크롤 다운 반복
        print("\n[스크롤 테스트]")
        for i in range(1, 6):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            n2, sample2 = count_dates_on_page(page)
            print(f"  스크롤 {i}회: {n2}개 (증가={n2 - n})")
            if n2 == n and i > 2:
                break
            n = n2

        # 4) 마지막에 사용자 상품들 존재 여부
        print("\n[4개 품목 타이틀 검색]")
        products_in_dom = page.evaluate(r"""
            () => {
                const terms = ['스파클라', '쌈장', '장작', '참쌀', '마시멜로', '캡슐', '전병'];
                const anchors = document.querySelectorAll('a[href*="ssr/sdp/link"]');
                const hits = {};
                for (const term of terms) hits[term] = [];
                for (const a of anchors) {
                    const text = (a.innerText || a.textContent || '').trim();
                    for (const term of terms) {
                        if (text.includes(term)) hits[term].push(text.slice(0, 80));
                    }
                }
                return hits;
            }
        """)
        for k, v in products_in_dom.items():
            print(f"  {k}: {len(v)}건 — {v[:2]}")
    finally:
        close_browser(p, browser, ctx, page)
    return 0


if __name__ == "__main__":
    sys.exit(main())

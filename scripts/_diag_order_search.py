"""4개 사업 품목이 주문내역 검색에서 발견되는지 확인."""
import sys
from pathlib import Path
import urllib.parse

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


def main():
    if not _is_cdp_available():
        print("CDP 미가용")
        return 1

    p, browser, ctx, page = init_browser()
    try:
        if not is_session_valid(page):
            print("세션 무효")
            return 1

        test_names = ["스파클라", "쌈장", "장작", "참쌀", "전병", "마시멜로", "캡슐"]
        for name in test_names:
            url = (
                "https://mc.coupang.com/ssr/desktop/order/list"
                f"?isSearch=true&keyword={urllib.parse.quote(name)}"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            result = page.evaluate(r"""
                () => {
                    const DATE_RE = /(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\s*주문/;
                    const dates = [];
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    while (walker.nextNode()) {
                        const t = (walker.currentNode.nodeValue || '').trim();
                        const m = DATE_RE.exec(t);
                        if (m) dates.push(`${m[1]}-${m[2]}-${m[3]}`);
                    }
                    const anchors = document.querySelectorAll('a[href*="ssr/sdp/link"]');
                    const titles = [];
                    for (const a of anchors) {
                        const t = (a.innerText || a.textContent || '').trim();
                        if (t && t.length >= 3) titles.push(t.slice(0, 80));
                    }
                    return { dates, titles: titles.slice(0, 10) };
                }
            """)
            print(f"\n[{name}] 검색")
            print(f"  날짜 {len(result['dates'])}개: {result['dates']}")
            print(f"  상품 타이틀 {len(result['titles'])}건:")
            for t in result["titles"]:
                print(f"    - {t}")
    finally:
        close_browser(p, browser, ctx, page)
    return 0


if __name__ == "__main__":
    sys.exit(main())

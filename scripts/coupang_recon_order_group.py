"""
쿠팡 주문내역 '주문 묶음' 구조 정찰.

목표: 사용자가 한 번에 결제한 주문 = "같은 주문 ID 묶음"을 식별하기 위한 DOM 파악.
한 주문에 들어간 여러 상품들이 어떤 컨테이너로 묶여 있는지 확인.

사용: python scripts/coupang_recon_order_group.py
"""
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main():
    from modules.coupang_orderer import (
        init_browser, close_browser, is_session_valid,
        _wait_for_akamai_challenge_clear,
    )

    p, browser, context, page = init_browser()
    try:
        if not is_session_valid(page):
            print("[중단] 세션 무효")
            return

        list_url = "https://mc.coupang.com/ssr/desktop/order/list"
        print(f"[goto] {list_url}")
        page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
        _wait_for_akamai_challenge_clear(page, max_sec=25)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        print("\n[1] 주문 단위 컨테이너 후보 (반복되는 큰 div):")
        # 한 주문 안에 여러 상품이 들어가는 경우 → 주문 컨테이너 1개에 여러 a[href*="sdp/link"] 자식
        # 주문 컨테이너 후보: sdp/link 앵커 2개 이상 포함하는 부모
        groups = page.evaluate(r"""
            () => {
                const anchors = document.querySelectorAll('a[href*="ssr/sdp/link"]');
                // 각 앵커에서 조상 방향으로 5단계 올라가며 해당 앵커 갯수 집계
                // 2개 이상 포함하는 가장 가까운 조상을 '주문 컨테이너'로 간주
                const parentCounts = new Map();
                for (const a of anchors) {
                    let cur = a.parentElement;
                    const visited = new Set();
                    for (let i = 0; i < 6 && cur; i++) {
                        if (visited.has(cur)) break;
                        visited.add(cur);
                        const count = parentCounts.get(cur) || { el: cur, anchors: new Set() };
                        count.anchors.add(a);
                        parentCounts.set(cur, count);
                        cur = cur.parentElement;
                    }
                }
                // 앵커 ≥2 포함 + 너무 크지 않은 조상들만
                const candidates = [];
                for (const [el, info] of parentCounts.entries()) {
                    if (info.anchors.size < 2) continue;
                    // 이 조상의 전체 앵커 수 = 주문 컨테이너면 여기 들어있는 주문 묶음 크기
                    const chain = [];
                    let cur = el;
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
                    candidates.push({
                        anchorCount: info.anchors.size,
                        path: chain.join(' > '),
                        textLen: (el.innerText || '').length,
                    });
                }
                // 가장 타이트(앵커 개수는 많되 textLen은 작은 것)한 후보 우선
                candidates.sort((a, b) => {
                    if (b.anchorCount !== a.anchorCount) return b.anchorCount - a.anchorCount;
                    return a.textLen - b.textLen;
                });
                return candidates.slice(0, 15);
            }
        """)
        for i, c in enumerate(groups):
            print(f"  [{i}] 앵커={c['anchorCount']}개  textLen={c['textLen']}")
            print(f"      {c['path']}")

        print("\n[2] 주문번호/날짜 텍스트 근방 탐색:")
        # "2026.4.23" 또는 "주문번호 1234567" 같은 텍스트
        order_meta = page.evaluate(r"""
            () => {
                const out = [];
                const patterns = [
                    /\d{4}\.\s?\d{1,2}\.\s?\d{1,2}/,  // 2026.4.23
                    /\d{4}-\d{2}-\d{2}/,
                    /주문번호\s*[\d\-]+/,
                    /\d{10,}/,  // 주문번호일 가능성
                ];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                const seen = new Set();
                while (walker.nextNode()) {
                    const t = (walker.currentNode.nodeValue || '').trim();
                    if (!t || t.length > 80) continue;
                    for (const re of patterns) {
                        if (re.test(t)) {
                            const parent = walker.currentNode.parentElement;
                            if (!parent) break;
                            let chain = parent.tagName.toLowerCase();
                            if (parent.className && typeof parent.className === 'string') {
                                const c = parent.className.trim().split(/\s+/).filter(Boolean).slice(0, 2).join('.');
                                if (c) chain += '.' + c;
                            }
                            const key = chain + '|' + t;
                            if (!seen.has(key)) {
                                seen.add(key);
                                out.push({ text: t.slice(0, 60), path: chain });
                            }
                            break;
                        }
                    }
                    if (out.length >= 15) break;
                }
                return out;
            }
        """)
        for i, m in enumerate(order_meta):
            print(f"  [{i}] '{m['text']:<45}' {m['path']}")

        # HTML 덤프
        html_path = PROJECT_ROOT / "logs" / "coupang_order_group_recon.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(page.content(), encoding="utf-8")
        print(f"\n[덤프] {html_path} ({html_path.stat().st_size} bytes)")

    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

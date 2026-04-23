"""
쿠팡 상품 상세 페이지에서 '장바구니/카트' 관련 버튼 후보 셀렉터 덤프.

modules/coupang_orderer._click_add_to_cart() 의 셀렉터 목록 갱신용.
실제 클릭은 절대 하지 않음 (사용자 카트 영향 0).

사용:
  python scripts/coupang_dump_cart_button.py [URL]
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DEFAULT_URL = "https://www.coupang.com/vp/products/8304592330"

# 1) "장바구니" 텍스트를 가진 클릭 가능한 엘리먼트
# 2) class/id에 cart 단어가 들어간 엘리먼트
# 3) data-* 속성에 cart/order 키워드가 있는 엘리먼트
_JS_FIND_CART = r"""
() => {
  const out = [];
  const seen = new Set();
  function pathOf(el) {
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
    return chain.join(' > ');
  }
  function pushIfNew(el, source) {
    const path = pathOf(el);
    if (seen.has(path)) return;
    seen.add(path);
    const r = el.getBoundingClientRect();
    out.push({
      source,
      tag: el.tagName.toLowerCase(),
      text: ((el.innerText || el.textContent || '').trim()).slice(0, 40),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 120),
      id: el.id || '',
      visible: r.width > 0 && r.height > 0,
      disabled: el.disabled || el.getAttribute('disabled') !== null,
      ariaLabel: el.getAttribute('aria-label') || '',
      dataAttrs: Array.from(el.attributes || [])
        .filter(a => a.name.startsWith('data-'))
        .map(a => a.name + '=' + (a.value || '').slice(0, 30))
        .join(' '),
      path
    });
  }

  // 1) "장바구니" 텍스트 포함 엘리먼트 (button/a/div/span 등 모두)
  const all = document.querySelectorAll('button, a, div, span, [role="button"]');
  for (const el of all) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 30) continue;
    if (t.includes('장바구니') || t.includes('담기') || t.includes('바로구매')) {
      pushIfNew(el, 'text');
    }
  }
  // 2) class 또는 id에 cart 들어간 엘리먼트
  const cartClass = document.querySelectorAll(
    '[class*="cart" i], [class*="Cart"], [id*="cart" i]'
  );
  for (const el of cartClass) pushIfNew(el, 'cart-class');

  // 3) data-* 속성에 cart/order 들어간 엘리먼트
  const allEls = document.querySelectorAll('*');
  for (const el of allEls) {
    for (const a of el.attributes || []) {
      if (!a.name.startsWith('data-')) continue;
      const v = (a.value || '').toLowerCase();
      if (v.includes('cart') || v.includes('order') || a.name.includes('cart')) {
        pushIfNew(el, 'data-attr');
        break;
      }
    }
    if (out.length >= 200) break;
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

        # 본문 로드 대기 (Akamai 챌린지 통과까지)
        for i in range(30):
            try:
                state = page.evaluate(r"""
                    () => ({
                        bodyLen: (document.body?.innerText || '').length,
                        hasChallenge: !!document.getElementById('sec-if-cpt-container')
                    })
                """)
            except Exception:
                break
            if state["bodyLen"] > 500 and not state["hasChallenge"]:
                break
            time.sleep(1)

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(1)

        candidates = page.evaluate(_JS_FIND_CART)
        if not candidates:
            print("[경고] 장바구니 후보 엘리먼트 없음")
            return

        print(f"=== 후보 {len(candidates)}개 ===\n")
        # 우선순위: text 매치 → 보이고 disabled 아닌 것 → cart-class
        def rank(c):
            score = 0
            if c["source"] == "text": score -= 10
            if c["visible"]: score -= 5
            if c["disabled"]: score += 5
            return score

        candidates.sort(key=rank)
        for c in candidates[:30]:
            flags = []
            if c["visible"]: flags.append("visible")
            if c["disabled"]: flags.append("disabled")
            if c["ariaLabel"]: flags.append(f"aria='{c['ariaLabel']}'")
            print(f"  [{c['source']:>10}] tag={c['tag']:<6} text='{c['text']:<25}' "
                  f"flags=[{','.join(flags)}]")
            print(f"             id={c['id']!r:<20} cls={c['cls'][:80]!r}")
            if c["dataAttrs"]:
                print(f"             data: {c['dataAttrs'][:120]}")
            print(f"             path: {c['path']}")
            print()
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

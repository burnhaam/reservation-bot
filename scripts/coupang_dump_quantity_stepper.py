"""
쿠팡 상품 상세 페이지의 '수량 stepper' 셀렉터 정찰.

modules/coupang_orderer.add_single_item가 quantity를 반영하려면
+/- 버튼 또는 input의 정확한 셀렉터를 알아야 함.

사용:
  python scripts/coupang_dump_quantity_stepper.py [URL]
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DEFAULT_URL = "https://www.coupang.com/vp/products/5381058739"  # 코멧 참나무장작

_JS = r"""
() => {
  // prod-buy-quantity-and-footer 안의 입력/버튼 후보 전부 덤프
  const out = [];
  const containers = document.querySelectorAll(
    '.prod-buy-quantity-and-footer, [class*="quantity"], [class*="prod-quantity"]'
  );
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
  function record(el, src) {
    const p = pathOf(el);
    if (seen.has(p)) return;
    seen.add(p);
    const r = el.getBoundingClientRect();
    out.push({
      source: src,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      text: ((el.innerText || el.textContent || '').trim()).slice(0, 40),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 120),
      id: el.id || '',
      value: el.value || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      disabled: el.disabled || el.getAttribute('disabled') !== null,
      visible: r.width > 0 && r.height > 0,
      path: p,
    });
  }
  for (const c of containers) {
    // 컨테이너 자체 + 내부 button/input/span 모두
    record(c, 'container');
    c.querySelectorAll('button, input, span[role="button"], [class*="plus"], [class*="minus"], [class*="up"], [class*="down"]').forEach(el => record(el, 'child'));
  }
  // class에 quantity가 들어간 input/button (컨테이너 밖에 있을 수도)
  document.querySelectorAll(
    'input[class*="quantity" i], button[class*="quantity" i], input[name*="quantity" i]'
  ).forEach(el => record(el, 'quantity-attr'));
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
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(2)

        rows = page.evaluate(_JS)
        if not rows:
            print("[경고] 후보 엘리먼트 없음")
            return

        print(f"=== 후보 {len(rows)}개 ===\n")
        for r in rows:
            flags = []
            if r["visible"]: flags.append("visible")
            if r["disabled"]: flags.append("disabled")
            print(f"  [{r['source']:>14}] tag={r['tag']:<6} type='{r['type']:<6}' "
                  f"text='{r['text']:<25}' flags=[{','.join(flags)}]")
            print(f"                  id={r['id']!r:<25} aria={r['ariaLabel']!r:<20} value={r['value']!r}")
            print(f"                  cls={r['cls'][:90]!r}")
            print(f"                  path: {r['path']}")
            print()
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

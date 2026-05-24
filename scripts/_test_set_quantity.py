"""
_set_quantity 최종 검증.
"""
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_DEFAULT_URL = "https://www.coupang.com/vp/products/5381058739"


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_URL
    qty = int(sys.argv[2]) if len(sys.argv) > 2 else 4

    from modules.coupang_orderer import (
        init_browser, close_browser, is_session_valid,
        _wait_for_akamai_challenge_clear, _set_quantity,
    )

    print(f"[INFO] URL={url}, target={qty}\n")
    p, browser, context, page = init_browser()
    try:
        if not is_session_valid(page):
            return
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _wait_for_akamai_challenge_clear(page, max_sec=25)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        time.sleep(2)

        before = page.evaluate("() => document.querySelector('.product-quantity input').value")
        print(f"[before] input.value = {before!r}")

        t0 = time.time()
        ok = _set_quantity(page, qty)
        dt = time.time() - t0
        print(f"[set_quantity] return = {ok}  elapsed={dt:.1f}s")

        after = page.evaluate("() => document.querySelector('.product-quantity input').value")
        print(f"[after]  input.value = {after!r}")
        print(f"\nresult: {'PASS' if str(after) == str(qty) else 'FAIL'}")
    finally:
        close_browser(p, browser, context, page)


if __name__ == "__main__":
    main()

"""
매핑표의 모든 URL을 HTTP GET으로 빠르게 점검.

Akamai 챌린지에 걸려도 HTTP 200을 반환하므로 엄밀한 '살아있는가' 검증은 안 되지만,
404/410/500 같은 명백한 죽은 링크는 잡을 수 있다. 풀 브라우저 렌더링으로 검증하려면
scripts/coupang_dryrun.py 를 품목별로 돌려야 하나 391건 × 5초 = 너무 김.

사용:
  python scripts/coupang_check_urls.py              # 전 품목
  python scripts/coupang_check_urls.py --sample 30  # 30건 샘플
"""
import argparse
import json
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def check(item_name: str, url: str, timeout: float = 10) -> dict:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        status = r.status_code
        final = r.url
        # 404/410 명백한 dead / 5xx 서버 장애 / 200이지만 redirect로 쿠팡 홈으로 튕김
        dead = status in (404, 410)
        home_redirect = "/vp/products/" not in final and "coupang.com" in final
        return {
            "item": item_name,
            "url": url,
            "status": status,
            "final": final,
            "dead": dead,
            "home_redirect": home_redirect,
        }
    except requests.RequestException as e:
        return {
            "item": item_name,
            "url": url,
            "status": "ERR",
            "final": "",
            "dead": True,
            "home_redirect": False,
            "error": str(e)[:100],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=0, help="N건 샘플링")
    parser.add_argument("--workers", type=int, default=10, help="동시 요청 수")
    args = parser.parse_args()

    mapping = json.loads((PROJECT_ROOT / "data" / "product_mapping.json").read_text(encoding="utf-8"))
    active = [(k, v["url"]) for k, v in mapping.items()
              if not k.startswith("_") and isinstance(v, dict)
              and v.get("자동주문_허용") and v.get("url")]

    if args.sample:
        import random
        random.seed(42)
        active = random.sample(active, min(args.sample, len(active)))

    print(f"점검 대상: {len(active)}건, 동시 {args.workers}")
    t0 = time.time()

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(check, name, url): (name, url) for name, url in active}
        done = 0
        for fut in as_completed(futures):
            results.append(fut.result())
            done += 1
            if done % 25 == 0:
                print(f"  진행: {done}/{len(active)}", flush=True)

    elapsed = time.time() - t0
    print(f"\n완료 ({elapsed:.1f}s)\n")

    dead = [r for r in results if r["dead"]]
    redirected = [r for r in results if not r["dead"] and r["home_redirect"]]
    ok_count = len(results) - len(dead) - len(redirected)

    print("=" * 60)
    print(f" 요약: OK {ok_count} / 리다이렉트 {len(redirected)} / DEAD {len(dead)}")
    print("=" * 60)

    if dead:
        print(f"\n[DEAD] 판매 중단/존재하지 않는 링크 ({len(dead)}건):")
        for r in dead:
            err = f" {r.get('error','')}" if r.get('error') else ""
            print(f"  [{r['status']}] {r['item']:<30} {r['url']}{err}")

    if redirected:
        print(f"\n[REDIRECT] 상품 페이지가 아닌 곳으로 튕김 ({len(redirected)}건):")
        for r in redirected:
            print(f"  [{r['status']}] {r['item']:<30}")
            print(f"     원본: {r['url']}")
            print(f"     최종: {r['final']}")

    if not dead and not redirected:
        print("\n모든 URL 정상 접근 가능.")


if __name__ == "__main__":
    main()

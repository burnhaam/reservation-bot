"""캘린더 B(소캠스 예약일정)에 누락된 알바 일정 보충 + 요일 불일치 수정.

- 캘린더 A(소유 일정관리)와 DB의 confirmed 예약에서 체크인 후보 날짜를 모음.
- 각 날짜에 대해 캘린더 B를 조회하여 알바 패턴 이벤트가 있는지 확인.
- 없으면: 평일=staff_name_weekday, 주말(토/일)=staff_name_weekend 으로 생성.
- 있으면 요일 룰과 맞지 않을 때 제목을 갱신(같은 인원/연박 표기 유지).

기본은 dryrun. 적용하려면 --apply.
대상 기간: --since(기본=오늘) ~ --until(기본=오늘+180일).
"""

import argparse
import io
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

# Windows cp949 콘솔에서도 한글/이모지가 그대로 나오도록 stdout 재인코딩.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.calendar import (  # noqa: E402
    _create_google_event,
    _get_google_calendar_service,
    _resolve_google_calendar_id,
    resolve_staff_name,
)
from modules.config_loader import load_config  # noqa: E402
from modules.db import get_connection  # noqa: E402


# "최자임님 / 성인 2명" / "조성은님 / 성인 4명 (연박2배)" / "최자임님 / 4인"
ALBA_PREFIX_RE = re.compile(r"^\s*(?P<staff>\S+님)\s*/\s*(?P<rest>.+)$")
# 캘린더 A의 예약 이벤트 패턴 — "네. 홍길동. 4인" / "에. John. 2인" / "에. 로건 ㅁ / 2인" 등
# prefix(. 또는 .) → 이름 → 인원(N인) 이 모두 등장해야 예약으로 인정.
GUESTS_FROM_A_RE = re.compile(r"(\d+)\s*인")
RESERVATION_A_RE = re.compile(r"^\s*(?:네|에)\s*[\.·\-/]\s*.+?\s*[\.·\-/]?\s*\d+\s*인\b")


def _parse_args() -> argparse.Namespace:
    today = date.today()
    p = argparse.ArgumentParser(description="알바 캘린더 보충 + 요일 불일치 수정")
    p.add_argument("--since", default=today.isoformat(), help="시작일 (YYYY-MM-DD, 포함)")
    p.add_argument(
        "--until",
        default=(today + timedelta(days=180)).isoformat(),
        help="종료일 (YYYY-MM-DD, 포함)",
    )
    p.add_argument("--apply", action="store_true", help="실제 적용 (없으면 dryrun)")
    return p.parse_args()


def _to_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _list_events_in_range(service, cal_name: str, start: date, end: date) -> list[dict]:
    cal_id = _resolve_google_calendar_id(service, cal_name)
    if not cal_id:
        print(f"[!] 캘린더를 찾을 수 없음: {cal_name}")
        return []

    items: list[dict] = []
    page_token = None
    time_min = start.isoformat() + "T00:00:00+09:00"
    time_max = (end + timedelta(days=1)).isoformat() + "T00:00:00+09:00"
    while True:
        resp = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def _alba_events_by_date(events: list[dict]) -> dict[date, list[dict]]:
    """캘린더 B 이벤트 중 '{이름}님 / ...' 패턴만 알바 이벤트로 간주."""
    out: dict[date, list[dict]] = {}
    for ev in events:
        summary = (ev.get("summary") or "").strip()
        m = ALBA_PREFIX_RE.match(summary)
        if not m:
            continue
        start = ev.get("start", {}).get("date")
        d = _to_date(start) if start else None
        if not d:
            continue
        out.setdefault(d, []).append({
            "event_id": ev.get("id", ""),
            "summary": summary,
            "staff": m.group("staff"),
            "rest": m.group("rest"),
        })
    return out


def _candidates_from_calendar_a(events: list[dict]) -> dict[date, dict]:
    """캘린더 A 이벤트 중 예약 패턴(`네/에. 이름. N인`)만 후보로 추출.

    캘린더 A에는 개인 메모/할일이 섞여있어 모든 이벤트를 후보로 삼으면 안 됨.
    """
    out: dict[date, dict] = {}
    for ev in events:
        summary = (ev.get("summary") or "").strip()
        if not RESERVATION_A_RE.match(summary):
            continue
        # 날짜 기반(종일) 이벤트만. dateTime은 시간 약속이라 제외.
        start = ev.get("start", {}).get("date")
        end = ev.get("end", {}).get("date")
        ci = _to_date(start) if start else None
        co = _to_date(end) if end else None
        if not ci:
            continue
        m = GUESTS_FROM_A_RE.search(summary)
        guests = int(m.group(1)) if m else None
        nights = (co - ci).days if co else 1
        if nights <= 0:
            nights = 1
        prev = out.get(ci)
        if not prev or nights > prev["nights"]:
            out[ci] = {"guests": guests, "nights": nights, "source_summary": summary}
    return out


def _candidates_from_db(start: date, end: date) -> dict[date, dict]:
    out: dict[date, dict] = {}
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT booking_id, guest_name, checkin, checkout, guests "
            "FROM reservations "
            "WHERE status='confirmed' AND checkin BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        for r in cur.fetchall():
            ci = _to_date(r["checkin"])
            co = _to_date(r["checkout"])
            if not ci:
                continue
            nights = (co - ci).days if co else 1
            if nights <= 0:
                nights = 1
            prev = out.get(ci)
            if not prev or nights > prev["nights"]:
                out[ci] = {
                    "guests": r["guests"],
                    "nights": nights,
                    "source_summary": f"DB:{r['booking_id']}",
                }
    return out


def _build_summary(staff_name: str, guests: Optional[int], nights: int) -> str:
    guests_str = str(guests if guests is not None else 2)
    if nights > 1:
        return f"{staff_name} / 성인 {guests_str}명 (연박{nights}배)"
    return f"{staff_name} / 성인 {guests_str}명"


def main() -> int:
    args = _parse_args()
    since = _to_date(args.since)
    until = _to_date(args.until)
    if not since or not until or since > until:
        print("[!] --since/--until 형식 오류")
        return 2

    cfg = load_config()
    owner_cal = cfg["naver_owner_calendar"]
    staff_cal = cfg["naver_staff_calendar"]
    weekday_name = cfg.get("staff_name_weekday") or cfg.get("staff_name", "")
    weekend_name = cfg.get("staff_name_weekend") or cfg.get("staff_name", "")

    print(f"기간: {since} ~ {until}")
    print(f"평일 담당: {weekday_name} / 주말 담당: {weekend_name}")
    print(f"모드: {'APPLY' if args.apply else 'DRYRUN'}")
    print()

    svc = _get_google_calendar_service()
    a_events = _list_events_in_range(svc, owner_cal, since, until)
    b_events = _list_events_in_range(svc, staff_cal, since, until)
    print(f"캘린더 A 이벤트: {len(a_events)}건 / 캘린더 B 이벤트: {len(b_events)}건")

    cand_a = _candidates_from_calendar_a(a_events)
    cand_db = _candidates_from_db(since, until)

    # 캘린더 A 후보 우선, DB는 보강용
    candidates: dict[date, dict] = dict(cand_a)
    for d, c in cand_db.items():
        if d not in candidates:
            candidates[d] = c

    print(f"후보 날짜: {len(candidates)}건 (A:{len(cand_a)}, DB-only:{len(cand_db) - len([d for d in cand_db if d in cand_a])})")
    print()

    alba_map = _alba_events_by_date(b_events)

    to_create: list[tuple[date, dict, str]] = []
    to_update: list[tuple[dict, str, str, date]] = []
    skipped_ok = 0

    for d in sorted(candidates):
        info = candidates[d]
        expected_staff = resolve_staff_name(d, cfg)
        existing = alba_map.get(d, [])

        if not existing:
            summary = _build_summary(expected_staff, info["guests"], info["nights"])
            to_create.append((d, info, summary))
            continue

        # 이미 알바 이벤트가 있으면, 요일 룰과 다르면 제목만 갱신
        # 같은 날짜에 여러 알바 이벤트가 있는 비정상 케이스는 모두 검사
        for ev in existing:
            if ev["staff"] != expected_staff:
                new_summary = f"{expected_staff} / {ev['rest']}"
                to_update.append((ev, ev["summary"], new_summary, d))
            else:
                skipped_ok += 1

    print(f"=== 생성 대상: {len(to_create)}건 ===")
    for d, info, summary in to_create:
        print(f"  {d} ({d.strftime('%a')}) → {summary}    [source: {info['source_summary']}]")

    print(f"\n=== 제목 갱신 대상 (요일 불일치): {len(to_update)}건 ===")
    for ev, old, new, d in to_update:
        print(f"  {d} ({d.strftime('%a')}): {old}  →  {new}")

    print(f"\n=== 이미 정상: {skipped_ok}건 ===")

    if not args.apply:
        print("\n[DRYRUN] 적용하려면 --apply 추가")
        return 0

    print("\n=== 적용 중 ===")
    staff_cal_id = _resolve_google_calendar_id(svc, staff_cal)
    created = updated = failed = 0

    for d, info, summary in to_create:
        try:
            eid = _create_google_event(svc, staff_cal, summary, d, d + timedelta(days=1))
            if eid:
                created += 1
                print(f"  + 생성: {d} {summary} (id={eid})")
            else:
                failed += 1
                print(f"  ! 생성 실패: {d} {summary}")
        except Exception as e:
            failed += 1
            print(f"  ! 생성 예외: {d} {summary} — {e}")

    for ev, old, new, d in to_update:
        try:
            full = svc.events().get(calendarId=staff_cal_id, eventId=ev["event_id"]).execute()
            full["summary"] = new
            svc.events().update(
                calendarId=staff_cal_id, eventId=ev["event_id"], body=full
            ).execute()
            updated += 1
            print(f"  ~ 갱신: {d} {old}  →  {new}")
        except Exception as e:
            failed += 1
            print(f"  ! 갱신 예외: {d} {old} — {e}")

    print(f"\n완료: 생성 {created}, 갱신 {updated}, 실패 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Discord 봇 — 슬래시 커맨드 기반 승인/조회 인터페이스.

역할:
- 카카오 알림 이중화는 notifier.py의 `_send_discord_webhook()`이 이미 처리.
  (웹훅은 별도 봇 로그인 불필요)
- 이 봇은 양방향 인터랙션 전용: 사용자가 슬래시 커맨드로 매핑 승인 등 실행.

커맨드:
  /ping                    - 봇 응답 확인
  /status                  - CDP Chrome + 파이프라인 + DB 현황
  /list                    - 대기 중 매핑 승인 목록
  /approve <id>            - id 항목 승인 (matching table 반영)
  /reject <id>             - id 항목 거부 (대기열에서 제거)
  /pending_clear           - 모든 대기 건 삭제 (주의)

대기열 포맷: data/pending_approvals.json
  {"next_id": N, "items": [
    {"id": 1, "type": "mapping_add|mapping_update|mapping_disable",
     "memo_item": "...", "current_url": "...", "suggested_url": "...",
     "reason": "...", "created_at": "..."},
    ...
  ]}

런처: scripts/discord_bot_start.py
"""
import json
import logging
import os
import socket
import sqlite3
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAPPING_PATH = PROJECT_ROOT / "data" / "product_mapping.json"
PENDING_PATH = PROJECT_ROOT / "data" / "pending_approvals.json"
DB_PATH = PROJECT_ROOT / "db" / "reservations.db"


# =============================================================
# pending_approvals.json 헬퍼
# =============================================================

def _load_pending() -> dict:
    if PENDING_PATH.exists():
        try:
            return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[Discord] pending_approvals.json 파싱 실패")
    return {"next_id": 1, "items": []}


def _save_pending(data: dict) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _apply_approval(item: dict) -> tuple[bool, str]:
    """승인된 항목을 product_mapping.json에 반영. (성공여부, 메시지) 반환."""
    if not MAPPING_PATH.exists():
        return False, "product_mapping.json 없음"

    try:
        mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"매핑 파싱 실패: {e}"

    action = item.get("type")
    memo_item = item.get("memo_item", "")
    suggested_url = item.get("suggested_url", "")

    # 백업 생성 (1회당)
    backup_path = MAPPING_PATH.with_suffix(".json.bak")
    try:
        backup_path.write_text(MAPPING_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        logger.warning("[Discord] 매핑 백업 실패 (계속 진행)", exc_info=True)

    if action == "mapping_add":
        # 신규 매핑 추가
        if memo_item in mapping:
            # 이미 키가 있으면 url만 갱신 + 별칭 보존
            mapping[memo_item]["url"] = suggested_url
            mapping[memo_item]["최근주문일"] = datetime.now().strftime("%Y-%m-%d")
            msg = f"'{memo_item}' URL 갱신"
        else:
            mapping[memo_item] = {
                "url": suggested_url,
                "상품명": memo_item,
                "기본수량": 1,
                "최대가격": int(item.get("suggested_max_price", 0) or 0),
                "최근주문일": datetime.now().strftime("%Y-%m-%d"),
                "자동주문_허용": True,
                "별칭": item.get("aliases", []),
                "카테고리": "기타",
                "분류": "기타",
            }
            msg = f"'{memo_item}' 신규 매핑 추가"

    elif action == "mapping_update":
        if memo_item not in mapping:
            return False, f"매핑에 '{memo_item}' 없음 (update 불가)"
        mapping[memo_item]["url"] = suggested_url
        mapping[memo_item]["최근주문일"] = datetime.now().strftime("%Y-%m-%d")
        aliases = item.get("aliases", [])
        if aliases:
            existing = set(mapping[memo_item].get("별칭", []) or [])
            for a in aliases:
                if a and a not in existing:
                    existing.add(a)
            mapping[memo_item]["별칭"] = sorted(existing)
        msg = f"'{memo_item}' URL 갱신"

    elif action == "mapping_disable":
        if memo_item not in mapping:
            return False, f"매핑에 '{memo_item}' 없음"
        mapping[memo_item]["자동주문_허용"] = False
        msg = f"'{memo_item}' 자동주문 비활성화"

    else:
        return False, f"알 수 없는 type: {action}"

    try:
        MAPPING_PATH.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        return False, f"매핑 저장 실패: {e}"

    return True, msg


def add_pending(item_type: str, memo_item: str,
                suggested_url: str = "", current_url: str = "",
                reason: str = "", aliases: list | None = None,
                suggested_max_price: int = 0) -> int:
    """Logic ①②③가 호출해서 승인 대기열에 항목 추가. id 반환."""
    data = _load_pending()
    next_id = int(data.get("next_id", 1))
    entry = {
        "id": next_id,
        "type": item_type,
        "memo_item": memo_item,
        "current_url": current_url,
        "suggested_url": suggested_url,
        "suggested_max_price": suggested_max_price,
        "reason": reason,
        "aliases": aliases or [],
        "created_at": datetime.now().isoformat(),
    }
    data["items"].append(entry)
    data["next_id"] = next_id + 1
    _save_pending(data)
    return next_id


# =============================================================
# 상태 조회 헬퍼
# =============================================================

def _is_cdp_port_open() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 9222))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _last_pipeline_success() -> str:
    path = PROJECT_ROOT / "data" / "last_success.txt"
    if not path.exists():
        return "기록 없음"
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return "읽기 실패"


def _today_stock_summary() -> str:
    if not DB_PATH.exists():
        return "DB 없음"
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM stock_orders "
            "WHERE date(detected_at) = date('now','localtime') GROUP BY status"
        ).fetchall()
        conn.close()
        if not rows:
            return "오늘 처리 0건"
        return ", ".join(f"{r['status']}={r['n']}" for r in rows)
    except Exception as e:
        return f"조회 실패: {e}"


# =============================================================
# 봇 클래스
# =============================================================

class ReservationBot(discord.Client):
    def __init__(self, guild_id: str | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._guild_id = guild_id

    async def setup_hook(self):
        _register_commands(self.tree)
        if self._guild_id:
            guild = discord.Object(id=int(self._guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("[Discord] 길드 %s에 커맨드 즉시 동기화", self._guild_id)
        else:
            await self.tree.sync()
            logger.info("[Discord] 커맨드 글로벌 동기화 (전파 최대 1시간)")

    async def on_ready(self):
        logger.info("[Discord] 봇 로그인 완료: %s (id=%s)", self.user, self.user.id)


# =============================================================
# 슬래시 커맨드
# =============================================================

def _register_commands(tree: app_commands.CommandTree) -> None:

    @tree.command(name="ping", description="봇 응답 확인")
    async def ping(interaction: discord.Interaction):
        await interaction.response.send_message("pong — 봇 정상 작동 중")

    @tree.command(name="status", description="CDP Chrome + 파이프라인 현황")
    async def status(interaction: discord.Interaction):
        cdp = "ON" if _is_cdp_port_open() else "OFF"
        last = _last_pipeline_success()
        today = _today_stock_summary()
        pending = len(_load_pending().get("items", []))
        msg = (
            f"**봇 상태**\n"
            f"- CDP Chrome(port 9222): `{cdp}`\n"
            f"- 마지막 파이프라인 성공: `{last}`\n"
            f"- 오늘 stock_orders: `{today}`\n"
            f"- 대기 중 승인: `{pending}건`"
        )
        await interaction.response.send_message(msg)

    @tree.command(name="list", description="대기 중인 매핑 승인 목록")
    async def list_pending(interaction: discord.Interaction):
        data = _load_pending()
        items = data.get("items", [])
        if not items:
            await interaction.response.send_message("대기 중 승인 건 없음.")
            return
        lines = [f"**대기 중 {len(items)}건**"]
        for it in items[:15]:
            label = {
                "mapping_add": "신규",
                "mapping_update": "URL 교체",
                "mapping_disable": "비활성화",
            }.get(it["type"], it["type"])
            line = f"  `#{it['id']}` [{label}] **{it['memo_item']}**"
            if it.get("suggested_url"):
                line += f" → {it['suggested_url'][:50]}"
            if it.get("reason"):
                line += f"  _({it['reason'][:60]})_"
            lines.append(line)
        if len(items) > 15:
            lines.append(f"...외 {len(items) - 15}건")
        lines.append("\n승인: `/approve <id>` · 거부: `/reject <id>`")
        await interaction.response.send_message("\n".join(lines))

    @tree.command(name="approve", description="대기 중 매핑을 승인하여 매핑표 반영")
    @app_commands.describe(approval_id="승인할 항목 ID (`/list`로 확인)")
    async def approve(interaction: discord.Interaction, approval_id: int):
        data = _load_pending()
        target = next((it for it in data["items"] if it["id"] == approval_id), None)
        if not target:
            await interaction.response.send_message(f"id={approval_id} 항목 없음")
            return
        ok, msg = _apply_approval(target)
        if ok:
            data["items"] = [it for it in data["items"] if it["id"] != approval_id]
            _save_pending(data)
            await interaction.response.send_message(f"승인 완료 — {msg}")
        else:
            await interaction.response.send_message(f"승인 실패: {msg}")

    @tree.command(name="reject", description="대기 중 매핑을 거부하여 삭제")
    @app_commands.describe(approval_id="거부할 항목 ID")
    async def reject(interaction: discord.Interaction, approval_id: int):
        data = _load_pending()
        before = len(data["items"])
        data["items"] = [it for it in data["items"] if it["id"] != approval_id]
        if len(data["items"]) == before:
            await interaction.response.send_message(f"id={approval_id} 항목 없음")
            return
        _save_pending(data)
        await interaction.response.send_message(f"거부 처리 — id={approval_id} 삭제")

    @tree.command(name="pending_clear", description="모든 대기 건 일괄 삭제 (주의)")
    async def pending_clear(interaction: discord.Interaction):
        data = _load_pending()
        n = len(data.get("items", []))
        data["items"] = []
        _save_pending(data)
        await interaction.response.send_message(f"대기 건 {n}개 모두 삭제됨")


# =============================================================
# 진입점
# =============================================================

def run():
    """봇 시작. DISCORD_BOT_TOKEN 없으면 에러.

    DISCORD_GUILD_ID 있으면 해당 길드 즉시 동기화, 없으면 글로벌(전파 최대 1시간).
    """
    from dotenv import load_dotenv
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN 미설정 — .env에 추가하세요")
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip() or None
    bot = ReservationBot(guild_id=guild_id)
    bot.run(token)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )
    run()

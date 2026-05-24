"""
Discord 봇 — 버튼 기반 승인 + 슬래시 커맨드 백업 인터페이스.

역할:
- 알림 채널은 notifier.py의 `_send_discord_webhook()`이 처리.
- 매핑 승인은 봇이 채널에 발송한 메시지의 [✅ 매핑 승인] / [❌ 매핑 거절]
  버튼으로 처리 (송금 자동화 봇과 동일 패턴, custom_id 영구 View).
- 슬래시 커맨드는 봇 다운/장애 시 백업 경로로 유지.

커맨드:
  /ping                    - 봇 응답 확인
  /status                  - CDP Chrome + 파이프라인 + DB 현황
  /list                    - 대기 중 매핑 승인 목록 (참고용 — 평소엔 버튼 사용)
  /approve <id>            - 백업: id 항목 승인
  /reject <id>             - 백업: id 항목 거부
  /pending_clear           - 모든 대기 건 삭제 (주의)
  /enable <name>           - 비활성 매핑 재활성화

대기열 포맷: data/pending_approvals.json
  {"next_id": N, "items": [
    {"id": 1, "type": "mapping_add|mapping_update|mapping_disable",
     "memo_item": "...", "current_url": "...", "suggested_url": "...",
     "reason": "...", "created_at": "...",
     "discord_message_id": <int|null>},  # 봇이 발송한 메시지 ID (중복 발송 방지)
    ...
  ]}

런처: scripts/discord_bot_start.py
"""
import asyncio
import json
import logging
import os
import socket
import sqlite3
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands, ui

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10  # 새 pending 항목 발견 시 발송 주기

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

    # product_mapping.json 은 raw 포맷: {canonical: body, "_skip_items": {...}}
    # alias_index/skip_index 는 load_mapping() 가 파생 생성하므로 파일에 저장하지 않음.

    if action == "mapping_add":
        if memo_item in mapping:
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
        suggested_qty = item.get("suggested_quantity")
        changes: list[str] = []
        if suggested_url:
            mapping[memo_item]["url"] = suggested_url
            changes.append("URL")
        if suggested_qty is not None:
            try:
                qty_int = int(suggested_qty)
            except (TypeError, ValueError):
                qty_int = None
            if qty_int and qty_int > 0:
                mapping[memo_item]["기본수량"] = qty_int
                changes.append(f"기본수량={qty_int}")
        if not changes:
            return False, f"'{memo_item}' update 항목 없음 (suggested_url/suggested_quantity 둘 다 비어있음)"
        mapping[memo_item]["최근주문일"] = datetime.now().strftime("%Y-%m-%d")
        aliases = item.get("aliases", [])
        if aliases:
            existing = set(mapping[memo_item].get("별칭", []) or [])
            for a in aliases:
                if a and a not in existing:
                    existing.add(a)
            mapping[memo_item]["별칭"] = sorted(existing)
        msg = f"'{memo_item}' {' + '.join(changes)} 갱신"

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
                suggested_max_price: int = 0,
                current_quantity: int | None = None,
                suggested_quantity: int | None = None) -> int:
    """Logic ①②③가 호출해서 승인 대기열에 항목 추가. id 반환.

    discord_message_id 는 None 으로 둠 — 봇 폴링 task 가 발견하면 채널에
    버튼 메시지를 발송하고 그 message_id 를 채워 넣는다 (중복 발송 방지).

    mapping_update 의 경우 suggested_url / suggested_quantity 중 비어있는 필드는
    "변경 없음" 으로 해석되어 _apply_approval 에서 무시된다.
    """
    data = _load_pending()
    next_id = int(data.get("next_id", 1))
    entry = {
        "id": next_id,
        "type": item_type,
        "memo_item": memo_item,
        "current_url": current_url,
        "suggested_url": suggested_url,
        "current_quantity": current_quantity,
        "suggested_quantity": suggested_quantity,
        "suggested_max_price": suggested_max_price,
        "reason": reason,
        "aliases": aliases or [],
        "created_at": datetime.now().isoformat(),
        "discord_message_id": None,
    }
    data["items"].append(entry)
    data["next_id"] = next_id + 1
    _save_pending(data)
    return next_id


# =============================================================
# 버튼 View — 매핑 승인 / 거절 (송금 봇 PaymentActionView 와 동일 패턴)
# =============================================================

_TYPE_LABEL = {
    "mapping_add": "신규 매핑 추가",
    "mapping_update": "매핑 갱신 (URL/수량)",
    "mapping_disable": "자동주문 품목 제외",
}

# 버튼 라벨 (승인/거절) — type 별.
# "이번에만 주문 안함" 거절은 mapping_disable 에만 의미 있음 (제외 결정 보류 = 다음 3일 검사 시
# 다시 알림). 다른 type 의 거절은 단순히 "이 추천을 무시"라서 짧게 표기.
_BUTTON_LABELS = {
    "mapping_add":     ("✅ 승인 (자동주문 등록)",     "❌ 거절"),
    "mapping_update":  ("✅ 승인 (매핑 갱신)",         "❌ 거절"),
    "mapping_disable": ("✅ 승인 (자동주문 품목 제외)", "❌ 거절 (이번에만 주문 안함)"),
}
_BUTTON_LABELS_DEFAULT = ("✅ 승인", "❌ 거절")


def _format_approval_message(item: dict) -> str:
    """버튼 메시지 본문. 처리 후엔 끝에 결과 suffix 가 붙어 본문 자체로 이력화."""
    label = _TYPE_LABEL.get(item["type"], item["type"])
    lines = [f"🔔 **매핑 승인 요청 #{item['id']}** · _{label}_"]
    lines.append(f"품목: **{item['memo_item']}**")
    if item.get("current_url"):
        lines.append(f"현재 URL: <{item['current_url']}>")
    if item.get("suggested_url"):
        lines.append(f"제안 URL: <{item['suggested_url']}>")
    cur_qty = item.get("current_quantity")
    sug_qty = item.get("suggested_quantity")
    if cur_qty is not None or sug_qty is not None:
        cur_s = "?" if cur_qty is None else str(cur_qty)
        sug_s = "?" if sug_qty is None else str(sug_qty)
        lines.append(f"기본수량: {cur_s} → **{sug_s}**")
    if item.get("aliases"):
        lines.append(f"별칭: {', '.join(item['aliases'][:5])}")
    if item.get("reason"):
        lines.append(f"사유: _{item['reason']}_")
    return "\n".join(lines)


class MappingActionView(ui.View):
    """승인 / 거절 영구 View. timeout=None 이라 봇 재시작 후에도 custom_id 매칭
    으로 클릭이 계속 동작 (단, setup_hook 에서 add_view 재등록 필요).

    버튼 라벨은 item_type 별로 다름 (_BUTTON_LABELS 참고).
    custom_id 는 'mapping_approve:{id}' / 'mapping_reject:{id}' — 라벨과 무관하게 영구.
    """

    def __init__(self, approval_id: int, item_type: str = "mapping_disable"):
        super().__init__(timeout=None)
        self.approval_id = approval_id
        self.item_type = item_type
        approve_label, reject_label = _BUTTON_LABELS.get(item_type, _BUTTON_LABELS_DEFAULT)
        self.add_item(_make_mapping_button(
            approval_id, approve_label, discord.ButtonStyle.success, "approve"))
        self.add_item(_make_mapping_button(
            approval_id, reject_label, discord.ButtonStyle.danger, "reject"))


def _make_mapping_button(approval_id: int, label: str,
                          style: discord.ButtonStyle, action: str) -> ui.Button:
    btn = ui.Button(
        label=label,
        style=style,
        custom_id=f"mapping_{action}:{approval_id}",
    )

    async def _callback(interaction: discord.Interaction):
        await _handle_mapping_action(interaction, approval_id, action)

    btn.callback = _callback
    return btn


async def _handle_mapping_action(interaction: discord.Interaction,
                                  approval_id: int, action: str):
    bot = interaction.client  # type: ignore[assignment]

    # 권한 체크 — DISCORD_AUTHORIZED_USER_ID 비어있으면 누구나 가능
    if hasattr(bot, "is_authorized_user") and not bot.is_authorized_user(interaction.user.id):
        await interaction.response.send_message(
            "❌ 권한 없음 — 등록된 관리자만 누를 수 있습니다.",
            ephemeral=True,
        )
        return

    data = _load_pending()
    target = next((it for it in data["items"] if it["id"] == approval_id), None)

    user_label = interaction.user.display_name
    base_content = interaction.message.content or ""

    if target is None:
        # 이미 처리된 항목 — 메시지만 비활성화 처리
        await interaction.response.edit_message(
            content=base_content + f"\n\n⚠️ 이미 처리되었거나 항목 없음 (id={approval_id})",
            view=ui.View(timeout=None),
        )
        return

    if action == "approve":
        ok, msg = _apply_approval(target)
        if ok:
            data["items"] = [it for it in data["items"] if it["id"] != approval_id]
            _save_pending(data)
            suffix = f"\n\n✅ **승인 완료** — {msg} ({user_label})"
        else:
            # 실패는 큐에서 제거하지 않음 — 사유 표시만, 버튼은 비활성화하지 않음
            await interaction.response.send_message(
                f"❌ 승인 실패: {msg}",
                ephemeral=True,
            )
            return
    else:  # reject
        data["items"] = [it for it in data["items"] if it["id"] != approval_id]
        _save_pending(data)
        suffix = f"\n\n🚫 **거절** — id={approval_id} 삭제됨 ({user_label})"

    logger.info("[Discord] 매핑 #%s → %s (by %s)", approval_id, action, interaction.user)
    await interaction.response.edit_message(
        content=base_content + suffix,
        view=ui.View(timeout=None),
    )


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
    def __init__(self, guild_id: str | None = None,
                 channel_id: str | None = None,
                 authorized_user_ids: set[int] | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._guild_id = guild_id
        self._channel_id = int(channel_id) if channel_id else None
        self._authorized_user_ids = authorized_user_ids or set()
        self._approval_channel: discord.TextChannel | None = None

    def is_authorized_user(self, user_id: int) -> bool:
        # 권한자 미설정 = 누구나 (개발/단일사용자 환경 호환)
        if not self._authorized_user_ids:
            return True
        return int(user_id) in self._authorized_user_ids

    async def setup_hook(self):
        _register_commands(self.tree)

        # 영구 View 재등록 — 봇 재시작 후에도 기존 메시지 버튼이 동작
        # (custom_id 가 'mapping_approve:N' / 'mapping_reject:N' 인 모든 항목 대응)
        data = _load_pending()
        for item in data.get("items", []):
            self.add_view(MappingActionView(
                int(item["id"]), item.get("type", "mapping_disable")))
        if data.get("items"):
            logger.info("[Discord] 매핑 승인 영구 View 재등록: %d건", len(data["items"]))

        if self._guild_id:
            guild = discord.Object(id=int(self._guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("[Discord] 길드 %s에 커맨드 즉시 동기화", self._guild_id)
        else:
            await self.tree.sync()
            logger.info("[Discord] 커맨드 글로벌 동기화 (전파 최대 1시간)")

        # 새 pending 항목 발견 시 봇이 채널에 메시지 발송하는 백그라운드 task
        if self._channel_id:
            self.loop.create_task(self._approval_poll_loop())

    async def on_ready(self):
        logger.info("[Discord] 봇 로그인 완료: %s (id=%s)", self.user, self.user.id)
        if self._channel_id and self._approval_channel is None:
            await self._setup_approval_channel()

    async def _setup_approval_channel(self):
        """채널 캐시에서 못 찾으면 fetch 까지 시도. 실패 시 폴링은 동작 안 함."""
        ch = self.get_channel(self._channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(self._channel_id)
            except Exception as e:
                logger.error("[Discord] 채널 fetch 실패 (id=%s): %s", self._channel_id, e)
                return
        self._approval_channel = ch  # type: ignore[assignment]
        logger.info("[Discord] 매핑 승인 채널: #%s", getattr(ch, "name", "?"))

    async def _approval_poll_loop(self):
        """`pending_approvals.json` 에서 discord_message_id 미설정 항목을 발견하면
        채널에 버튼 메시지를 발송하고 message_id 를 채워 넣는다 (idempotent).

        product_matcher.py 의 add_pending() 은 동기 코드이므로, 봇 프로세스에선
        이 폴링으로 비동기 발송을 트리거한다.
        """
        await self.wait_until_ready()
        if self._approval_channel is None:
            await self._setup_approval_channel()
        while not self.is_closed():
            try:
                await self._dispatch_pending_messages()
            except Exception:
                logger.exception("[Discord] approval poll 예외")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _dispatch_pending_messages(self):
        if self._approval_channel is None:
            return
        data = _load_pending()
        changed = False
        for item in data.get("items", []):
            if item.get("discord_message_id"):
                continue  # 이미 발송됨
            content = _format_approval_message(item)
            view = MappingActionView(
                int(item["id"]), item.get("type", "mapping_disable"))
            try:
                msg = await self._approval_channel.send(content=content, view=view)
            except Exception:
                logger.exception("[Discord] 매핑 메시지 발송 실패 — id=%s", item.get("id"))
                continue
            item["discord_message_id"] = msg.id
            changed = True
            logger.info("[Discord] 매핑 승인 메시지 발송: #%s → message=%s",
                        item.get("id"), msg.id)
        if changed:
            _save_pending(data)


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
        lines.append("\n채널의 매핑 메시지에서 [✅ 매핑 승인] / [❌ 매핑 거절] 버튼으로 처리하세요.")
        lines.append("백업: `/approve <id>` · `/reject <id>`")
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

    @tree.command(name="enable", description="비활성화된 매핑을 다시 활성화")
    @app_commands.describe(item_name="활성화할 품목 이름 (매핑 키)")
    async def enable(interaction: discord.Interaction, item_name: str):
        if not MAPPING_PATH.exists():
            await interaction.response.send_message("product_mapping.json 없음")
            return
        try:
            mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            await interaction.response.send_message(f"매핑 로드 실패: {e}")
            return
        if item_name not in mapping or not isinstance(mapping.get(item_name), dict):
            await interaction.response.send_message(f"매핑에 '{item_name}' 없음")
            return
        prev = mapping[item_name].get("자동주문_허용", True)
        mapping[item_name]["자동주문_허용"] = True
        try:
            # 백업
            backup = MAPPING_PATH.with_suffix(".json.bak")
            backup.write_text(MAPPING_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            MAPPING_PATH.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            await interaction.response.send_message(
                f"'{item_name}' 자동주문 활성화 완료 (이전: {prev})"
            )
        except Exception as e:
            await interaction.response.send_message(f"저장 실패: {e}")


# =============================================================
# 진입점
# =============================================================

def run():
    """봇 시작. DISCORD_BOT_TOKEN 없으면 에러.

    환경변수:
      DISCORD_BOT_TOKEN           (필수) 봇 토큰
      DISCORD_CHANNEL_ID          (권장) 매핑 승인 메시지 발송 채널 ID — 미설정 시 버튼 기능 OFF
      DISCORD_AUTHORIZED_USER_ID  (선택) 콤마 구분 사용자 ID 목록 — 미설정 시 누구나 버튼 가능
      DISCORD_GUILD_ID            (선택) 길드 즉시 동기화. 없으면 글로벌(전파 최대 1시간)
    """
    from dotenv import load_dotenv
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN 미설정 — .env에 추가하세요")
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip() or None
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", "").strip() or None

    raw_users = os.environ.get("DISCORD_AUTHORIZED_USER_ID", "").strip()
    authorized_user_ids: set[int] = set()
    if raw_users:
        for piece in raw_users.split(","):
            piece = piece.strip()
            if piece.isdigit():
                authorized_user_ids.add(int(piece))

    bot = ReservationBot(
        guild_id=guild_id,
        channel_id=channel_id,
        authorized_user_ids=authorized_user_ids,
    )
    bot.run(token)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )
    run()

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    import google.generativeai as genai
except ImportError:  # pragma: no cover - optional dep
    genai = None

GEMINI_PRO_MODEL = "gemini-2.5-pro"
GEMINI_FLASH_MODEL = "gemini-flash-lite-latest"
PRO_RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60
SCAN_COOLDOWN_SECONDS = 7 * 24 * 3600
REPORT_TTL_SECONDS = 3 * 24 * 3600
MAX_REPORT_CHARS = 1800


class GeminiKeyPool:
    def __init__(self, key_file: Path):
        self.key_file = key_file
        self._keys = []
        self._index = 0
        self._lock = asyncio.Lock()
        self.reload()

    def reload(self):
        keys = []
        if self.key_file.exists():
            with self.key_file.open("r", encoding="utf-8") as handle:
                for line in handle:
                    token = line.strip()
                    if not token or token.startswith("#"):
                        continue
                    keys.append(token)
        self._keys = keys
        self._index = 0

    async def acquire_key(self) -> str:
        async with self._lock:
            if not self._keys:
                self.reload()
            if not self._keys:
                raise RuntimeError(f"Gemini key file {self.key_file} is empty。")
            key = self._keys[self._index % len(self._keys)]
            self._index = (self._index + 1) % len(self._keys)
            return key

    def __len__(self):
        return len(self._keys)


class SlidingRateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self.events = deque()
        self.lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self.lock:
                now = time.time()
                while self.events and now - self.events[0] >= self.window:
                    self.events.popleft()
                if len(self.events) < self.limit:
                    self.events.append(now)
                    return
                wait_seconds = RATE_WINDOW_SECONDS
            await asyncio.sleep(wait_seconds)


class GeminiClient:
    def __init__(self, key_pool: GeminiKeyPool, pro_rate_limiter: SlidingRateLimiter):
        self.key_pool = key_pool
        self.pro_rate_limiter = pro_rate_limiter

    async def generate(self, model: str, prompt: str, generation_config: Optional[Dict[str, Any]] = None):
        if genai is None:
            raise RuntimeError("google-generativeai 未安裝。請在 requirements.txt 中加入並安裝。")
        if model == GEMINI_PRO_MODEL and self.pro_rate_limiter:
            await self.pro_rate_limiter.acquire()
        attempts = max(1, len(self.key_pool))
        last_error = None
        for _ in range(attempts):
            key = await self.key_pool.acquire_key()
            try:
                return await asyncio.to_thread(
                    self._invoke_model,
                    key,
                    model,
                    prompt,
                    generation_config or {},
                )
            except Exception as exc:  # pragma: no cover - remote API errors
                last_error = exc
                await asyncio.sleep(1)
        raise RuntimeError(f"Gemini 請求失敗：{last_error}") from last_error

    @staticmethod
    def _invoke_model(api_key: str, model: str, prompt: str, generation_config: Dict[str, Any]):
        genai.configure(api_key=api_key)
        model_instance = genai.GenerativeModel(model)
        response = model_instance.generate_content(
            prompt,
            generation_config=generation_config,
        )
        return response


class GeminiAIExpansion(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.shared_api = getattr(bot, "shared_api", {})
        self.ai_storage = Path(self.shared_api.get("ai_analyse_dir", "AI_Analyse_Bot"))
        self.ai_storage.mkdir(exist_ok=True)
        self.report_dir = self.ai_storage
        self.scan_usage_file = self.ai_storage / "scan_usage.json"
        self.usage_lock = asyncio.Lock()
        self.key_pool = GeminiKeyPool(Path(__file__).with_name("Gemini_keys.txt"))
        self.rate_limiter = SlidingRateLimiter(PRO_RATE_LIMIT, RATE_WINDOW_SECONDS)
        self.client = GeminiClient(self.key_pool, self.rate_limiter)
        self.version = self.shared_api.get("version", "v2.0")
        self.send_log = self.shared_api.get("send_log")
        self.bot.tree.add_command(self.security_scan)
        self.bot.tree.add_command(self.report)
        if len(self.key_pool) == 0:
            print("[Gemini Expansion] 警告：Gemini_keys.txt 為空，請填入 API Key 以啟用此擴充功能。")

    def cog_unload(self):
        self.bot.tree.remove_command(self.security_scan.name, type=self.security_scan.type)
        self.bot.tree.remove_command(self.report.name, type=self.report.type)

    # --------- Slash Commands ---------

    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.command(name="gemini-security-scan", description="使用 Gemini 2.5 Pro 對伺服器進行深度安全掃描")
    async def security_scan(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        can_run, block_msg = await self._can_run_scan(interaction.guild_id, interaction.user.id)
        if not can_run:
            await interaction.followup.send(block_msg, ephemeral=True)
            return
        try:
            context = await self._collect_guild_context(interaction.guild)
            prompt = self._build_security_prompt(context)
            response = await self.client.generate(GEMINI_PRO_MODEL, prompt)
            report_text = self._normalize_response_text(response)
            report_text = self._truncate(report_text)
            embed = discord.Embed(
                title="Gemini 伺服器安全報告",
                description=report_text,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"AntiNuke360 {self.version} | Gemini 擴充 v1.0")
            await interaction.followup.send(embed=embed, ephemeral=True)
            await self._update_scan_usage(interaction.guild_id, interaction.user.id)
        except Exception as exc:
            await interaction.followup.send(f"Gemini 掃描失敗：{exc}", ephemeral=True)

    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.command(name="gemini-bot-report", description="檢視或重新產生特定機器人的 AI 分析報告")
    @app_commands.describe(bot_id="機器人 ID", refresh="是否強制重新產生報告")
    async def report(self, interaction: discord.Interaction, bot_id: str, refresh: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = interaction.guild.get_member(int(bot_id))
        except Exception:
            member = None
        if member is None or not member.bot:
            await interaction.followup.send("找不到該機器人，請確認它在此伺服器內。", ephemeral=True)
            return
        try:
            report = await self._get_or_create_bot_report(member, force_refresh=refresh)
            embed = discord.Embed(
                title=f"Bot 安全報告 - {member.display_name}",
                description=report.get("summary", "無資料"),
                color=discord.Color.orange() if report.get("risk_level") != "low" else discord.Color.green(),
            )
            embed.add_field(name="風險等級", value=report.get("risk_level", "unknown"), inline=False)
            if report.get("suspicious_signals"):
                embed.add_field(name="可疑徵象", value="\n".join(report["suspicious_signals"][:5]), inline=False)
            if report.get("recommendations"):
                embed.add_field(name="建議措施", value="\n".join(report["recommendations"][:5]), inline=False)
            source = report.get("source", "cache")
            embed.set_footer(text=f"AntiNuke360 {self.version} | 來源: {source}")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"取得報告時發生錯誤：{exc}", ephemeral=True)

    # --------- Listeners ---------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild is None or not member.bot:
            return
        try:
            report = await self._get_or_create_bot_report(member)
            risk_level = (report.get("risk_level") or "unknown").lower()
            if risk_level in {"medium", "high", "critical"}:
                await self._revoke_bot_permissions(member)
                await self._notify_security_team(member, report)
        except Exception as exc:
            print(f"[Gemini Expansion] 分析新機器人失敗: {exc}")

    # --------- Internal helpers ---------

    async def _can_run_scan(self, guild_id: int, user_id: int):
        async with self.usage_lock:
            data = self._load_usage()
            now = time.time()
            guild_key = f"guild:{guild_id}"
            user_key = f"user:{user_id}"
            guild_remaining = self._remaining(data.get(guild_key, 0), now)
            user_remaining = self._remaining(data.get(user_key, 0), now)
            if guild_remaining > 0:
                return False, self._cooldown_message("伺服器", guild_remaining)
            if user_remaining > 0:
                return False, self._cooldown_message("帳號", user_remaining)
            return True, ""

    async def _update_scan_usage(self, guild_id: int, user_id: int):
        async with self.usage_lock:
            data = self._load_usage()
            now = time.time()
            data[f"guild:{guild_id}"] = now
            data[f"user:{user_id}"] = now
            self._save_usage(data)

    def _remaining(self, last_ts: float, now: float) -> float:
        if not last_ts:
            return 0
        elapsed = now - last_ts
        remaining = SCAN_COOLDOWN_SECONDS - elapsed
        return max(0, remaining)

    def _cooldown_message(self, scope: str, remaining: float) -> str:
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return f"⚠️ 這個{scope}在 7 天內已經使用過掃描。請於 {hours} 小時 {minutes} 分鐘後再試。"

    def _load_usage(self) -> Dict[str, float]:
        if not self.scan_usage_file.exists():
            return {}
        try:
            with self.scan_usage_file.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save_usage(self, data: Dict[str, float]):
        with self.scan_usage_file.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)

    async def _collect_guild_context(self, guild: discord.Guild) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "guild": {
                "id": guild.id,
                "name": guild.name,
                "member_count": guild.member_count,
                "owner": f"{guild.owner} ({guild.owner_id})" if guild.owner else None,
                "created_at": guild.created_at.isoformat() if guild.created_at else None,
            },
            "bot_overview": [],
            "recent_audit": [],
        }
        for member in guild.members:
            if not member.bot:
                continue
            perms = member.guild_permissions
            context["bot_overview"].append({
                "id": member.id,
                "tag": str(member),
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "roles": [role.name for role in member.roles if role.name],
                "administrator": perms.administrator,
                "manage_guild": perms.manage_guild,
                "manage_roles": perms.manage_roles,
                "kick_members": perms.kick_members,
                "ban_members": perms.ban_members,
            })
        try:
            async for entry in guild.audit_logs(limit=75):
                context["recent_audit"].append({
                    "action": str(entry.action),
                    "actor": f"{entry.user} ({entry.user.id})" if entry.user else "Unknown",
                    "target": f"{entry.target} ({getattr(entry.target, 'id', 'N/A')})" if entry.target else "Unknown",
                    "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    "reason": entry.reason,
                })
        except discord.Forbidden:
            context["recent_audit"].append({"error": "無法讀取審核日誌 (缺少權限)"})
        return context

    def _build_security_prompt(self, context: Dict[str, Any]) -> str:
        payload = json.dumps(context, ensure_ascii=False)
        return (
            "你是資安顧問，需基於 Discord 伺服器資料產生安全審查建議。\n"
            "請詳細分析每條審核日誌與所有機器人的權限，揪出炸群或偽裝成防炸群的可疑 bot。\n"
            "輸出內容需包含：\n"
            "1. 伺服器風險摘要\n"
            "2. 可疑機器人清單（若無請註明）\n"
            "3. 伺服器防護建議與立即行動清單\n"
            "4. 需要注意的審核日誌事件\n"
            "請使用繁體中文，總字數不超過 1700 字元。以下是 JSON 資料：\n"
            f"```json\n{payload}\n```"
        )

    def _truncate(self, text: str) -> str:
        if len(text) <= MAX_REPORT_CHARS:
            return text
        return text[: MAX_REPORT_CHARS - 3] + "..."

    def _normalize_response_text(self, response: Any) -> str:
        if response is None:
            return "(Gemini 沒有返回內容)"
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        parts = []
        for cand in getattr(response, "candidates", []) or []:
            part_text = getattr(cand, "content", None)
            if part_text:
                parts.append(str(part_text))
        return ("\n\n".join(parts)).strip() or "(Gemini 沒有返回內容)"

    def _bot_report_path(self, guild_id: int, bot_id: int) -> Path:
        return self.report_dir / f"{guild_id}_{bot_id}.json"

    def _load_cached_report(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return None
        timestamp = data.get("timestamp", 0)
        if (time.time() - timestamp) > REPORT_TTL_SECONDS:
            return None
        data["source"] = "cache"
        return data

    def _save_bot_report(self, path: Path, payload: Dict[str, Any]):
        payload = {**payload, "timestamp": time.time()}
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    async def _get_or_create_bot_report(self, member: discord.Member, force_refresh: bool = False) -> Dict[str, Any]:
        path = self._bot_report_path(member.guild.id, member.id)
        if not force_refresh:
            cached = self._load_cached_report(path)
            if cached:
                return cached
        context = await self._collect_bot_context(member)
        prompt = self._build_bot_prompt(context)
        response = await self.client.generate(
            GEMINI_FLASH_MODEL,
            prompt,
            generation_config={"response_mime_type": "application/json"},
        )
        report = self._parse_bot_response(response)
        report["source"] = "live"
        self._save_bot_report(path, report)
        return report

    async def _collect_bot_context(self, member: discord.Member) -> Dict[str, Any]:
        inviter = None
        try:
            async for entry in member.guild.audit_logs(limit=25, action=discord.AuditLogAction.bot_add):
                if entry.target and entry.target.id == member.id:
                    inviter = {
                        "id": entry.user.id if entry.user else None,
                        "tag": str(entry.user) if entry.user else None,
                        "created_at": entry.created_at.isoformat() if entry.created_at else None,
                    }
                    break
        except discord.Forbidden:
            inviter = None
        perms = member.guild_permissions
        return {
            "bot": {
                "id": member.id,
                "tag": str(member),
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            },
            "inviter": inviter,
            "roles": [
                {
                    "name": role.name,
                    "permissions": role.permissions.value,
                    "position": role.position,
                }
                for role in member.roles
            ],
            "perms": {
                "administrator": perms.administrator,
                "manage_roles": perms.manage_roles,
                "manage_guild": perms.manage_guild,
                "kick_members": perms.kick_members,
                "ban_members": perms.ban_members,
                "manage_channels": perms.manage_channels,
            },
        }

    def _build_bot_prompt(self, context: Dict[str, Any]) -> str:
        payload = json.dumps(context, ensure_ascii=False)
        return (
            "請以 JSON 格式回覆對 Discord Bot 的多因素安全評估。\n"
            "輸出欄位：risk_level (low/medium/high/critical)、summary、suspicious_signals (list)、recommendations (list)。\n"
            "若需要立即移除請在 summary 中明確指出。請使用繁體中文。以下為輸入：\n"
            f"```json\n{payload}\n```"
        )

    def _parse_bot_response(self, response: Any) -> Dict[str, Any]:
        text = self._normalize_response_text(response)
        try:
            data = json.loads(text)
        except Exception:
            data = {
                "risk_level": "unknown",
                "summary": text,
                "suspicious_signals": [],
                "recommendations": [],
            }
        data.setdefault("risk_level", "unknown")
        data.setdefault("summary", text)
        data.setdefault("suspicious_signals", [])
        data.setdefault("recommendations", [])
        return data

    async def _revoke_bot_permissions(self, member: discord.Member):
        guild = member.guild
        manageable = []
        me = guild.me
        if not me or not me.guild_permissions.manage_roles:
            return
        for role in member.roles:
            if role == guild.default_role:
                continue
            if me.top_role and role.position >= me.top_role.position:
                continue
            manageable.append(role)
        if manageable:
            try:
                await member.remove_roles(*manageable, reason="Gemini AI 判定為可疑 bot，撤除權限")
            except discord.Forbidden:
                pass

    async def _notify_security_team(self, member: discord.Member, report: Dict[str, Any]):
        if not callable(self.send_log):
            return
        embed = discord.Embed(
            title="⚠️ 可疑機器人警告",
            description=report.get("summary", "Gemini 無回應"),
            color=discord.Color.red(),
        )
        embed.add_field(name="風險等級", value=report.get("risk_level", "unknown"), inline=False)
        suspicious = report.get("suspicious_signals") or []
        if suspicious:
            embed.add_field(name="可疑徵象", value="\n".join(suspicious[:5]), inline=False)
        recs = report.get("recommendations") or []
        if recs:
            embed.add_field(name="建議措施", value="\n".join(recs[:5]), inline=False)
        embed.set_footer(text=f"AntiNuke360 {self.version} | Gemini 擴充 v1.0")
        await self.send_log(member.guild, embed=embed)


def setup(bot: commands.Bot):
    bot.add_cog(GeminiAIExpansion(bot))

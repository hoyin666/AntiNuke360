import os
import time
import asyncio
import json
from collections import defaultdict, deque
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEVELOPER_ID = 800536911378251787
BLACKLIST_FILE = "bot_blacklist.json"
WHITELIST_FILE = "bot_whitelist.json"
SERVER_WHITELIST_FILE = "server_whitelist.json"
GUILDS_FILE = "guilds_data.json"
SNAPSHOT_DIR = Path("snapshots")
SNAPSHOT_TTL_SECONDS = 72 * 3600  # 72 hours
VERSION = "v1.1.1"

SNAPSHOT_DIR.mkdir(exist_ok=True)

user_actions = defaultdict(lambda: defaultdict(lambda: defaultdict(deque)))
whitelisted_users = defaultdict(set)
server_whitelisted_bots = defaultdict(set)
banned_in_session = defaultdict(set)
notified_bans = defaultdict(set)

# 權限錯誤監控
permission_errors = defaultdict(deque)

# 防止短時間內重複詢問還原
restore_prompted = defaultdict(lambda: 0)

# 固定防護參數
PROTECTION_CONFIG = {
    "max_actions": 7,
    "window_seconds": 10,
    "enabled": True
}

# 自訂狀態文字
STATUS_MESSAGES = [
    "炸？AntiNuke360讓你沒地方炸！",
    "別炸了，AntiNuke360在盯著你",
    "我早知道找我，怎麼了？想我嗎？",
    "咖啡......加冰還是加糖？",
    "聽說有人想炸服?來啊,我等你",
    "沒有廣告,沒有彈窗,只有保護",
    "你的核彈按鈕呢？已經被我禁用了。",
    "不會偷偷裝全家桶的AntiNuke360",
    "黑名單正在更新...有人要上榜嗎？",
    "0.01%失敗率？那不是我的問題吧（大概）",
    "FBI Warning（誤）",
    "珍珠奶茶好喝欸",
    "晚安......不，我不睡覺",
    "我有一份黑名單，你想上嗎？",
    "這......巧克力太甜了...",
    "那是......什麼感覺？",
    "FBI Open Door（誤）",
    "老利（跑錯台了）",
    "鋒利度測試（跑錯台了）",
    "我不會炸群，因為我不是TSBOOM！",
    "中國的會爆炸，AntiNuke360的會防炸",
    "你好 我吃一點ww",
    "english or spanish",
    "sorry, I am gay"
    ]

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_blacklist(data):
    with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[BLACKLIST] 已儲存黑名單到 {BLACKLIST_FILE}")

def load_whitelist():
    if os.path.exists(WHITELIST_FILE):
        try:
            with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_whitelist(data):
    with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[WHITELIST] 已儲存白名單到 {WHITELIST_FILE}")

def load_server_whitelist():
    if os.path.exists(SERVER_WHITELIST_FILE):
        try:
            with open(SERVER_WHITELIST_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for guild_id, bot_list in data.items():
                    server_whitelisted_bots[int(guild_id)] = set(bot_list)
                return data
        except Exception:
            return {}
    return {}

def save_server_whitelist():
    data = {}
    for guild_id, bot_set in server_whitelisted_bots.items():
        data[str(guild_id)] = list(bot_set)
    with open(SERVER_WHITELIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[SERVER_WHITELIST] 已儲存伺服器白名單到 {SERVER_WHITELIST_FILE}")

def load_guilds_data():
    if os.path.exists(GUILDS_FILE):
        try:
            with open(GUILDS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_guilds_data(data):
    with open(GUILDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_to_guilds_data(guild_id):
    data = load_guilds_data()
    guild_id_str = str(guild_id)
    if guild_id_str not in data:
        data[guild_id_str] = {
            "joined_at": time.time(),
            "welcome_channel_id": None
        }
        save_guilds_data(data)

def remove_from_guilds_data(guild_id):
    data = load_guilds_data()
    guild_id_str = str(guild_id)
    if guild_id_str in data:
        del data[guild_id_str]
        save_guilds_data(data)

bot_blacklist = load_blacklist()
bot_whitelist = load_whitelist()
load_server_whitelist()

class AntiNukeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.moderation = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.status_index = 0
        self.last_status_update = 0

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            print(f"已同步 {len(synced)} 個斜線指令。")
        except Exception as e:
            print(f"同步斜線指令失敗: {e}")

bot = AntiNukeBot()

@bot.event
async def on_ready():
    print("=" * 60)
    print(f"[READY] Bot 已登入: {bot.user} ({VERSION})")
    print(f"[READY] 全域黑名單中有 {len(bot_blacklist)} 個機器人")
    print(f"[READY] 全域白名單中有 {len(bot_whitelist)} 個機器人")
    print(f"[READY] 正在 {len(bot.guilds)} 個伺服器中")
    print(f"[READY] 自訂狀態文字已啟用 ({len(STATUS_MESSAGES)} 個)")
    print(f"[READY] 快照資料夾: {SNAPSHOT_DIR.resolve()}，TTL: {SNAPSHOT_TTL_SECONDS} 秒")
    print("=" * 60)
    
    if not bot.change_status_loop.is_running():
        bot.change_status_loop.start()
        print("[STATUS] 已啟動狀態文字循環")

@tasks.loop(seconds=10)
async def change_status_loop():
    try:
        if len(STATUS_MESSAGES) == 0:
            return
        
        status_message = STATUS_MESSAGES[bot.status_index]
        status_obj = discord.CustomActivity(name=status_message)
        
        task = bot.change_presence(activity=status_obj, status=discord.Status.online)
        await asyncio.shield(task)
        
        bot.status_index = (bot.status_index + 1) % len(STATUS_MESSAGES)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[STATUS ERROR] 更新自訂狀態失敗: {e}")

bot.change_status_loop = change_status_loop

# Snapshot utilities
def snapshot_path(guild_id: int) -> Path:
    return SNAPSHOT_DIR / f"{guild_id}.json"

def save_snapshot_file(guild_id: int, data: dict):
    path = snapshot_path(guild_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[SNAPSHOT] 已儲存伺服器 {guild_id} 快照到 {path}")

def load_snapshot_file(guild_id: int):
    path = snapshot_path(guild_id)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[SNAPSHOT ERROR] 讀取快照失敗: {e}")
        return None

def snapshot_is_valid(snapshot: dict) -> bool:
    if not snapshot:
        return False
    ts = snapshot.get("timestamp", 0)
    return (time.time() - ts) <= SNAPSHOT_TTL_SECONDS

def snapshot_time_remaining(snapshot: dict) -> int:
    if not snapshot:
        return 0
    expires_at = snapshot.get("timestamp", 0) + SNAPSHOT_TTL_SECONDS
    return max(0, int(expires_at - time.time()))

async def create_snapshot(guild: discord.Guild):
    try:
        print(f"[SNAPSHOT] 建立快照: {guild.name} ({guild.id})")
        data = {"timestamp": time.time(), "roles": [], "categories": [], "channels": []}
        
        # Roles (exclude @everyone)
        roles = [r for r in guild.roles if r != guild.default_role]
        for r in roles:
            data["roles"].append({
                "name": r.name,
                "permissions": r.permissions.value,
                "color": r.color.value if r.color else 0,
                "hoist": r.hoist,
                "mentionable": r.mentionable,
                "position": r.position
            })
        
        # Categories
        categories = sorted(guild.categories, key=lambda c: c.position)
        for c in categories:
            overwrites = []
            for target, ow in c.overwrites.items():
                entry = {}
                if isinstance(target, discord.Role):
                    entry["type"] = "role"
                    entry["role_name"] = target.name
                elif isinstance(target, discord.Member):
                    entry["type"] = "member"
                    entry["member_id"] = target.id
                else:
                    continue
                try:
                    allow = int(ow.pair()[0].value) if hasattr(ow, "pair") else int(ow.read_permissions().value)
                except Exception:
                    allow = 0
                try:
                    deny = int(ow.pair()[1].value) if hasattr(ow, "pair") else 0
                except Exception:
                    deny = 0
                entry["allow"] = allow
                entry["deny"] = deny
                overwrites.append(entry)
            data["categories"].append({
                "name": c.name,
                "position": c.position,
                "overwrites": overwrites
            })
        
        # Channels (text & voice)
        channels = sorted(guild.channels, key=lambda ch: getattr(ch, "position", 0))
        for ch in channels:
            ch_type = "text" if isinstance(ch, discord.TextChannel) else ("voice" if isinstance(ch, discord.VoiceChannel) else "other")
            parent_name = ch.category.name if ch.category else None
            overwrites = []
            for target, ow in ch.overwrites.items():
                entry = {}
                if isinstance(target, discord.Role):
                    entry["type"] = "role"
                    entry["role_name"] = target.name
                elif isinstance(target, discord.Member):
                    entry["type"] = "member"
                    entry["member_id"] = target.id
                else:
                    continue
                try:
                    allow = int(ow.pair()[0].value) if hasattr(ow, "pair") else int(ow.read_permissions().value)
                except Exception:
                    allow = 0
                try:
                    deny = int(ow.pair()[1].value) if hasattr(ow, "pair") else 0
                except Exception:
                    deny = 0
                entry["allow"] = allow
                entry["deny"] = deny
                overwrites.append(entry)
            ch_info = {
                "name": ch.name,
                "type": ch_type,
                "position": getattr(ch, "position", 0),
                "parent": parent_name,
                "overwrites": overwrites
            }
            if isinstance(ch, discord.TextChannel):
                ch_info.update({
                    "topic": ch.topic,
                    "nsfw": ch.nsfw,
                    "slowmode": ch.slowmode_delay if hasattr(ch, "slowmode_delay") else getattr(ch, "slowmode", 0)
                })
            if isinstance(ch, discord.VoiceChannel):
                ch_info.update({
                    "bitrate": ch.bitrate,
                    "user_limit": ch.user_limit
                })
            data["channels"].append(ch_info)
        
        save_snapshot_file(guild.id, data)
        return True
    except Exception as e:
        print(f"[SNAPSHOT ERROR] 建立快照失敗: {e}")
        return False

async def perform_restore(guild: discord.Guild, ctx_sender=None):
    snapshot = load_snapshot_file(guild.id)
    if not snapshot or not snapshot_is_valid(snapshot):
        return False, "沒有有效的快照可用。"
    
    me = guild.me
    if not me:
        return False, "無法取得 Bot 的成員資料。"
    if not (me.guild_permissions.manage_roles and me.guild_permissions.manage_channels):
        return False, "權限不足：需要 Manage Roles 與 Manage Channels 權限來還原快照。"
    
    try:
        print(f"[RESTORE] 開始清除現有頻道與身分組（若 Bot 有權限）: {guild.name}")
        for ch in list(guild.channels):
            try:
                if ch.permissions_for(me).manage_channels:
                    await ch.delete(reason="AntiNuke360: 還原前清除現有頻道")
                    await asyncio.sleep(0.15)
                else:
                    print(f"[RESTORE] 無法刪除頻道 (權限不足): {ch.name}")
            except discord.Forbidden:
                print(f"[RESTORE] 刪除頻道權限不足: {ch.name}")
            except Exception as e:
                print(f"[RESTORE] 刪除頻道失敗 {ch.name}: {e}")
        
        bot_top_pos = me.top_role.position if me.top_role else -1
        for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            if role == guild.default_role:
                continue
            if role.position >= bot_top_pos:
                print(f"[RESTORE] 跳過刪除身分組 (位置高於或等於 Bot): {role.name}")
                continue
            try:
                await role.delete(reason="AntiNuke360: 還原前清除身分組")
                await asyncio.sleep(0.15)
            except discord.Forbidden:
                print(f"[RESTORE] 刪除身分組權限不足: {role.name}")
            except Exception as e:
                print(f"[RESTORE] 刪除身分組失敗 {role.name}: {e}")
        
        role_map = {}
        roles_data = sorted(snapshot.get("roles", []), key=lambda r: r.get("position", 0))
        created_roles = []
        for rdata in roles_data:
            name = rdata.get("name", "unnamed")
            perms = discord.Permissions(rdata.get("permissions", 0))
            color_val = rdata.get("color", 0)
            hoist = rdata.get("hoist", False)
            mentionable = rdata.get("mentionable", False)
            existing = discord.utils.get(guild.roles, name=name)
            if existing:
                role_map[name] = existing
                continue
            try:
                new_role = await guild.create_role(
                    name=name,
                    permissions=perms,
                    colour=discord.Colour(color_val) if color_val else discord.Colour.default(),
                    hoist=hoist,
                    mentionable=mentionable,
                    reason="AntiNuke360: 還原快照"
                )
                role_map[name] = new_role
                created_roles.append((new_role, rdata.get("position", 0)))
                await asyncio.sleep(0.15)
            except discord.Forbidden:
                print(f"[RESTORE] 權限不足，無法建立身分組: {name}")
            except Exception as e:
                print(f"[RESTORE] 建立身分組失敗 {name}: {e}")
        
        try:
            pos_map = {}
            for name, role in role_map.items():
                rp = next((r.get("position", 0) for r in roles_data if r.get("name") == name), role.position)
                pos_map[role] = rp
            if pos_map:
                try:
                    await guild.edit_role_positions({r: p for r, p in pos_map.items()})
                except AttributeError:
                    print("[RESTORE] guild.edit_role_positions 不可用，跳過批次設定順位")
                except discord.Forbidden as e:
                    print(f"[RESTORE] 調整角色順位失敗 (權限): {e}")
                except Exception as e:
                    print(f"[RESTORE] 調整角色順位失敗: {e}")
        except Exception as e:
            print(f"[RESTORE] 準備角色順位資料時發生錯誤: {e}")
        
        category_map = {}
        for cdata in sorted(snapshot.get("categories", []), key=lambda c: c.get("position", 0)):
            name = cdata.get("name", "category")
            existing = discord.utils.get(guild.categories, name=name)
            if existing:
                category_map[name] = existing
                continue
            overwrites = {}
            for ow in cdata.get("overwrites", []):
                if ow.get("type") == "role":
                    role_obj = role_map.get(ow.get("role_name"))
                    if role_obj:
                        allow = discord.Permissions(ow.get("allow", 0))
                        deny = discord.Permissions(ow.get("deny", 0))
                        overwrites[role_obj] = discord.PermissionOverwrite(allow=allow, deny=deny)
                elif ow.get("type") == "member":
                    member = guild.get_member(ow.get("member_id"))
                    if member:
                        allow = discord.Permissions(ow.get("allow", 0))
                        deny = discord.Permissions(ow.get("deny", 0))
                        overwrites[member] = discord.PermissionOverwrite(allow=allow, deny=deny)
            try:
                cat = await guild.create_category(name, overwrites=overwrites, reason="AntiNuke360: 還原快照")
                category_map[name] = cat
                await asyncio.sleep(0.12)
            except discord.Forbidden:
                print(f"[RESTORE] 權限不足，無法建立分類: {name}")
            except Exception as e:
                print(f"[RESTORE] 建立分類失敗 {name}: {e}")
        
        created_channels = []
        for chdata in sorted(snapshot.get("channels", []), key=lambda c: c.get("position", 0)):
            name = chdata.get("name", "channel")
            ch_type = chdata.get("type", "text")
            parent_name = chdata.get("parent")
            parent = category_map.get(parent_name) if parent_name else None
            overwrites = {}
            for ow in chdata.get("overwrites", []):
                if ow.get("type") == "role":
                    role_obj = role_map.get(ow.get("role_name"))
                    if role_obj:
                        allow = discord.Permissions(ow.get("allow", 0))
                        deny = discord.Permissions(ow.get("deny", 0))
                        overwrites[role_obj] = discord.PermissionOverwrite(allow=allow, deny=deny)
                elif ow.get("type") == "member":
                    member = guild.get_member(ow.get("member_id"))
                    if member:
                        allow = discord.Permissions(ow.get("allow", 0))
                        deny = discord.Permissions(ow.get("deny", 0))
                        overwrites[member] = discord.PermissionOverwrite(allow=allow, deny=deny)
            if ch_type == "text":
                topic = chdata.get("topic")
                nsfw = chdata.get("nsfw", False)
                slowmode = chdata.get("slowmode", 0)
                try:
                    ch = await guild.create_text_channel(name, category=parent, topic=topic, nsfw=nsfw, overwrites=overwrites, reason="AntiNuke360: 還原快照")
                    try:
                        await ch.edit(slowmode_delay=slowmode)
                    except Exception:
                        pass
                    created_channels.append((ch, chdata.get("position", 0)))
                    await asyncio.sleep(0.12)
                except discord.Forbidden:
                    print(f"[RESTORE] 權限不足，無法建立文字頻道: {name}")
                except Exception as e:
                    print(f"[RESTORE] 建立文字頻道失敗 {name}: {e}")
            elif ch_type == "voice":
                bitrate = chdata.get("bitrate", None)
                user_limit = chdata.get("user_limit", None)
                try:
                    ch = await guild.create_voice_channel(name, category=parent, bitrate=bitrate, user_limit=user_limit, overwrites=overwrites, reason="AntiNuke360: 還原快照")
                    created_channels.append((ch, chdata.get("position", 0)))
                    await asyncio.sleep(0.12)
                except discord.Forbidden:
                    print(f"[RESTORE] 權限不足，無法建立語音頻道: {name}")
                except Exception as e:
                    print(f"[RESTORE] 建立語音頻道失敗 {name}: {e}")
            else:
                continue
        
        try:
            for ch, pos in created_channels:
                try:
                    await ch.edit(position=pos)
                    await asyncio.sleep(0.08)
                except Exception:
                    pass
        except Exception as e:
            print(f"[RESTORE] 調整頻道順位失敗: {e}")
        
        return True, f"已嘗試還原伺服器結構。建立身分組: {len(role_map)}，建立/更新頻道: {len(created_channels)}"
    except discord.Forbidden as e:
        print(f"[RESTORE ERROR] 還原失敗: {e}")
        return False, f"還原失敗: 權限不足 ({e})"
    except Exception as e:
        print(f"[RESTORE ERROR] 還原失敗: {e}")
        return False, f"還原過程中發生錯誤: {e}"

async def prompt_restore_on_suspect(guild: discord.Guild):
    now = time.time()
    if now - restore_prompted[guild.id] < 600:
        return
    restore_prompted[guild.id] = now
    
    snapshot = load_snapshot_file(guild.id)
    if not snapshot or not snapshot_is_valid(snapshot):
        return
    
    remaining = snapshot_time_remaining(snapshot)
    owner = guild.owner
    message_text = (
        f"AntiNuke360 偵測到伺服器可能遭受大規模破壞攻擊。\n"
        f"我們偵測到一個快照可用，剩餘有效時間: {remaining//3600} 小時 {(remaining%3600)//60} 分鐘。\n"
        "回覆 `Y` 以自動還原伺服器結構（會先嘗試刪除可刪除的身分組與頻道），或回覆 `N` 以略過。\n"
        "您也可以稍後使用斜線指令 `/restore-snapshot` 手動還原。"
    )
    sent_location = None
    try:
        if owner:
            dm = await owner.create_dm()
            try:
                await dm.send(message_text)
                sent_location = ("dm", owner.id)
            except Exception:
                sent_location = None
    except Exception:
        sent_location = None
    
    if not sent_location:
        data = load_guilds_data()
        welcome_ch_id = data.get(str(guild.id), {}).get("welcome_channel_id")
        target_ch = None
        if welcome_ch_id:
            target_ch = guild.get_channel(welcome_ch_id)
        if not target_ch:
            target_ch = guild.system_channel
        if not target_ch:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target_ch = ch
                    break
        if target_ch:
            try:
                await target_ch.send(message_text)
                sent_location = ("channel", target_ch.id)
            except Exception:
                sent_location = None
    
    if not sent_location:
        print(f"[PROMPT] 無法通知伺服器擁有者或任何頻道來詢問還原: {guild.name}")
        return
    
    def check(m: discord.Message):
        try:
            if sent_location[0] == "dm":
                return m.author.id == owner.id and isinstance(m.channel, discord.DMChannel) and m.content.strip().upper() in ("Y", "N")
            else:
                return m.author.id == owner.id and m.channel.id == sent_location[1] and m.content.strip().upper() in ("Y", "N")
        except Exception:
            return False
    
    try:
        resp = await bot.wait_for("message", timeout=300.0, check=check)
        if resp.content.strip().upper() == "Y":
            ok, msg = await perform_restore(guild)
            notify = f"還原結果: {'成功' if ok else '失敗'}。{msg}"
            try:
                if sent_location[0] == "dm":
                    await resp.channel.send(notify)
                else:
                    ch = guild.get_channel(sent_location[1])
                    if ch:
                        await ch.send(notify)
            except Exception:
                pass
        else:
            notify = (
                "已選擇不還原。\n"
                "您可以使用斜線指令 `/restore-snapshot` 來手動還原。\n"
                f"目前快照剩餘有效時間: {remaining//3600} 小時 {(remaining%3600)//60} 分鐘。"
            )
            try:
                if sent_location[0] == "dm":
                    await resp.channel.send(notify)
                else:
                    ch = guild.get_channel(sent_location[1])
                    if ch:
                        await ch.send(notify)
            except Exception:
                pass
    except asyncio.TimeoutError:
        notify = (
            "未在 5 分鐘內收到回覆，已取消自動還原操作。\n"
            "如需還原，請使用斜線指令 `/restore-snapshot`。\n"
            f"目前快照剩餘有效時間: {remaining//3600} 小時 {(remaining%3600)//60} 分鐘。"
        )
        try:
            if sent_location and sent_location[0] == "dm" and owner:
                dm = await owner.create_dm()
                await dm.send(notify)
            elif sent_location:
                ch = guild.get_channel(sent_location[1])
                if ch:
                    await ch.send(notify)
        except Exception:
            pass

async def scan_and_ban_blacklist(guild):
    print(f"[SCAN] 開始掃描伺服器 {guild.name} 中的黑名單成員")
    banned_count = 0
    scan_count = 0
    try:
        async for member in guild.fetch_members(limit=None):
            scan_count += 1
            user_id_str = str(member.id)
            if user_id_str in bot_blacklist:
                try:
                    if member.id not in banned_in_session[guild.id]:
                        blacklist_info = bot_blacklist[user_id_str]
                        ban_reason = blacklist_info.get('reason', '黑名單機器人')
                        await guild.ban(member, reason=f"AntiNuke360: {ban_reason}")
                        banned_in_session[guild.id].add(member.id)
                        banned_count += 1
                        print(f"[SCAN] 已停權黑名單成員: {member} (ID: {member.id})")
                except Exception as e:
                    print(f"[SCAN ERROR] 無法停權 {member}: {e}")
    except Exception as e:
        print(f"[SCAN ERROR] 掃描伺服器失敗: {e}")
    print(f"[SCAN] 掃描完成 - 掃描 {scan_count} 人，停權 {banned_count} 人")
    return scan_count, banned_count

async def check_permission_errors(guild):
    gid = guild.id
    now = time.time()
    while permission_errors[gid] and now - permission_errors[gid][0] > 60:
        permission_errors[gid].popleft()
    if len(permission_errors[gid]) >= 10:
        print(f"[PERMISSION] 伺服器 {guild.name} 1 分鐘內出現 10 次權限錯誤，準備離開")
        try:
            embed = discord.Embed(title="身份組權限設錯警告", color=discord.Color.red())
            embed.description = f"AntiNuke360 在伺服器 '{guild.name}' 中 1 分鐘內遇到 10 次權限不足錯誤 (403 Forbidden)。\n\n請確保 Bot 的身份組具有以下權限：\n- 封禁成員 (Ban Members)\n- 踢出成員 (Kick Members)\n- 管理頻道 (Manage Channels)\n- 管理角色 (Manage Roles)\n\n將身份組移至頻道權限或伺服器權限中的其他管理員角色之上。\n\n修復後，本 Bot 將自動重新加入伺服器。"
            embed.set_footer(text="AntiNuke360 v1.1.1")
            try:
                await guild.owner.send(embed=embed)
                print(f"[PERMISSION] 已向伺服器所有者發送通知")
            except Exception as e:
                print(f"[PERMISSION ERROR] 無法發送 DM: {e}")
        except Exception as e:
            print(f"[PERMISSION ERROR] 構建嵌入訊息失敗: {e}")
        try:
            await guild.leave()
            print(f"[PERMISSION] 已自動離開伺服器: {guild.name}")
        except Exception as e:
            print(f"[PERMISSION ERROR] 無法離開伺服器: {e}")
        permission_errors[gid].clear()

async def track_action(guild, user, action_type):
    if guild is None or user is None:
        return False
    if user.id == guild.owner_id:
        return False
    if user.id in whitelisted_users[guild.id]:
        return False
    if user.id in server_whitelisted_bots[guild.id]:
        return False
    now = time.time()
    actions = user_actions[guild.id][user.id][action_type]
    actions.append(now)
    window = PROTECTION_CONFIG["window_seconds"]
    while actions and now - actions[0] > window:
        actions.popleft()
    current_count = len(actions)
    max_count = PROTECTION_CONFIG["max_actions"]
    if current_count > max_count:
        return True
    return False

async def take_action(guild, user, reason):
    global bot_blacklist, notified_bans
    gid = guild.id
    uid = user.id

    if uid in banned_in_session[gid]:
        return

    print(f"[ACTION] 開始處理 {user} (ID: {uid})")
    try:
        await guild.ban(user, reason=f"AntiNuke360: {reason}")
        banned_in_session[gid].add(uid)
        print(f"[BAN] 成功封鎖 {user}")

        if user.bot:
            user_id_str = str(uid)
            if user_id_str not in bot_blacklist:
                bot_blacklist[user_id_str] = {
                    "name": str(user),
                    "reason": reason,
                    "timestamp": time.time(),
                    "guilds_detected": [gid]
                }
            else:
                if gid not in bot_blacklist[user_id_str]["guilds_detected"]:
                    bot_blacklist[user_id_str]["guilds_detected"].append(gid)
            save_blacklist(bot_blacklist)
            print(f"[BLACKLIST] 已將 {user} 加入全域黑名單")

            # 在所有伺服器中掃描並停權
            await scan_blacklist_all_guilds()

        if uid not in notified_bans[gid] and guild.owner:
            notified_bans[gid].add(uid)
            embed = discord.Embed(title="[AntiNuke360 警報]", color=discord.Color.red())
            embed.description = f"使用者 `{user}` (ID: `{uid}`) 已在伺服器 `{guild.name}` 被自動封鎖。\n\n原因: {reason}"
            embed.add_field(name="伺服器", value=guild.name, inline=True)
            embed.add_field(name="伺服器 ID", value=str(gid), inline=True)
            embed.set_footer(text="AntiNuke360")
            try:
                await guild.owner.send(embed=embed)
            except Exception:
                pass
    except discord.Forbidden as e:
        print(f"[BAN ERROR] 權限不足: {e}")
        permission_errors[gid].append(time.time())
        await check_permission_errors(guild)
    except Exception as e:
        print(f"[BAN ERROR] 封鎖失敗: {e}")

async def scan_blacklist_all_guilds():
    print("[SCAN] 開始在所有伺服器中掃描黑名單成員")
    total_scanned = 0
    total_banned = 0
    for guild in bot.guilds:
        try:
            scan_count, banned_count = await scan_and_ban_blacklist(guild)
            total_scanned += scan_count
            total_banned += banned_count
        except Exception as e:
            print(f"[SCAN ERROR] 無法掃描伺服器 {guild.name}: {e}")
    print(f"[SCAN] 全部伺服器掃描完成 - 共掃描 {total_scanned} 人，停權 {total_banned} 人")

async def send_welcome_message(guild):
    try:
        if not guild.me.guild_permissions.manage_channels:
            print(f"[WELCOME] 無法創建頻道: 權限不足")
            return
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False),
            guild.me: discord.PermissionOverwrite(send_messages=True)
        }
        
        channel = await guild.create_text_channel(
            "antinuke360-welcome",
            overwrites=overwrites,
            reason="AntiNuke360 自動設置"
        )
        
        data = load_guilds_data()
        if str(guild.id) not in data:
            data[str(guild.id)] = {"joined_at": time.time(), "welcome_channel_id": channel.id}
        else:
            data[str(guild.id)]["welcome_channel_id"] = channel.id
        save_guilds_data(data)
        
        embed = discord.Embed(
            title="歡迎使用 AntiNuke360",
            description="感謝你將 AntiNuke360 加入此伺服器！",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="功能介紹",
            value="""AntiNuke360 是一個強大的伺服器防護機器人，提供以下功能：

自動 Nuke 攻擊防護
- 偵測大量刪除頻道
- 偵測大量發送訊息
- 偵測大量建立 Webhook
- 偵測大量踢出成員
- 偵測大量建立角色

全域黑名單系統
- 自動識別已知的惡意機器人
- 在試圖加入時立即封鎖
- 支援手動掃描並停權黑名單成員

本地白名單系統
- 每個伺服器獨立管理
- 信任的機器人不會被影響

固定防護參數
- 最優的靈敏度設置
- 無法調整(確保一致性)""",
            inline=False
        )
        
        embed.add_field(
            name="使用指南",
            value="""管理員指令:
/status - 查看防護狀態
/add-server-white [ID] - 加入本伺服器白名單
/remove-server-white [ID] - 從本伺服器白名單移除
/server-whitelist - 查看本伺服器白名單
/scan-blacklist - 掃描並停權伺服器中的黑名單成員

開發者指令:
/add-black [ID] [原因] - 加入全域黑名單
/remove-black [ID] - 移除全域黑名單
/add-white [ID] [原因] - 加入全域白名單
/remove-white [ID] - 移除全域白名單
/blacklist - 查看全域黑名單
/whitelist-list - 查看全域白名單
/scan-all-guilds - 在所有伺服器掃描並停權黑名單成員

還原快照:
/restore-snapshot - 還原伺服器快照 (管理員)""",
            inline=False
        )
        
        embed.add_field(
            name="防護參數 (固定)",
            value=f"""最大動作次數: {PROTECTION_CONFIG['max_actions']}
時間窗口: {PROTECTION_CONFIG['window_seconds']} 秒
狀態: 啟用

參數已優化，無法調整""",
            inline=False
        )
        
        embed.add_field(
            name="遇到問題？",
            value="如有任何問題或建議，請聯繫伺服器管理員或機器人開發者。",
            inline=False
        )
        
        embed.set_footer(text="AntiNuke360 v1.1.1 | 伺服器防護專家")
        
        await channel.send(embed=embed)
        print(f"[WELCOME] 已在伺服器 {guild.name} 創建歡迎頻道")
        
    except Exception as e:
        print(f"[WELCOME ERROR] 創建歡迎訊息失敗: {e}")

@bot.event
async def on_guild_join(guild):
    print(f"[JOIN] 已加入新伺服器: {guild.name} (ID: {guild.id})")
    add_to_guilds_data(guild.id)
    await send_welcome_message(guild)

@bot.event
async def on_guild_remove(guild):
    print(f"[LEAVE] 已從伺服器移除: {guild.name} (ID: {guild.id})")
    remove_from_guilds_data(guild.id)
    if guild.id in server_whitelisted_bots:
        del server_whitelisted_bots[guild.id]
        save_server_whitelist()
    if guild.id in permission_errors:
        del permission_errors[guild.id]

@bot.event
async def on_member_join(member):
    guild = member.guild
    user_id_str = str(member.id)
    
    # 如果是機器人，建立或覆寫快照
    if member.bot:
        try:
            await create_snapshot(guild)
        except Exception as e:
            print(f"[SNAPSHOT ERROR] 建立快照時發生錯誤: {e}")
    
    if user_id_str in bot_blacklist:
        print(f"[JOIN] {member} (黑名單機器人) 試圖加入伺服器 {guild.name}，立即封鎖")
        try:
            blacklist_info = bot_blacklist[user_id_str]
            ban_reason = blacklist_info.get('reason', '在其他伺服器進行 Nuke 攻擊')
            await guild.ban(member, reason=f"AntiNuke360: 黑名單機器人 - {ban_reason}")
            print(f"[BAN] 已封鎖黑名單機器人 {member}")
            
            if user_id_str not in notified_bans[guild.id]:
                notified_bans[guild.id].add(member.id)
                embed = discord.Embed(title="[AntiNuke360 警報]", color=discord.Color.red())
                embed.description = f"黑名單機器人 `{member}` (ID: `{member.id}`) 試圖加入伺服器被自動封鎖。"
                embed.add_field(name="被列入黑名單的原因", value=ban_reason, inline=False)
                embed.add_field(name="伺服器", value=guild.name, inline=True)
                embed.set_footer(text="AntiNuke360")
                try:
                    await guild.owner.send(embed=embed)
                except Exception:
                    pass
                
                try:
                    await member.send(embed=embed)
                except Exception:
                    pass
        except Exception as e:
            print(f"[BAN ERROR] 無法封鎖 {member}: {e}")
    elif user_id_str in bot_whitelist:
        print(f"[JOIN] {member} (全域白名單機器人) 加入伺服器 {guild.name}，允許")
    elif member.id in server_whitelisted_bots[guild.id]:
        print(f"[JOIN] {member} (伺服器白名單機器人) 加入伺服器 {guild.name}，允許")

@bot.event
async def on_webhook_update(channel):
    guild = channel.guild
    print(f"[EVENT] {guild.name} 中偵測到 Webhook 操作")
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.webhook_create):
            actor = entry.user
            actor_id_str = str(actor.id)
            
            if actor_id_str in bot_blacklist:
                await take_action(guild, actor, "黑名單機器人")
                break
            
            if actor_id_str in bot_whitelist or actor.id in server_whitelisted_bots[guild.id]:
                break
            
            if await track_action(guild, actor, "webhook_create"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "短時間內大量建立 Webhook")
            break
    except Exception:
        pass

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    guild = message.guild
    user_id_str = str(message.author.id)
    
    if user_id_str in bot_blacklist or user_id_str in bot_whitelist:
        return
    
    if message.author.id in server_whitelisted_bots[guild.id]:
        return
    
    if await track_action(guild, message.author, "message_send"):
        asyncio.create_task(prompt_restore_on_suspect(guild))
        await take_action(guild, message.author, "短時間內大量發送訊息")
    
    await bot.process_commands(message)

@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild
    print(f"[EVENT] {guild.name} 中創建了頻道: {channel.name}")
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
            actor = entry.user
            actor_id_str = str(actor.id)
            
            if actor_id_str in bot_blacklist:
                await take_action(guild, actor, "黑名單機器人")
                break
            
            if actor_id_str in bot_whitelist or actor.id in server_whitelisted_bots[guild.id]:
                break
            
            if await track_action(guild, actor, "channel_create"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "短時間內大量建立頻道")
            break
    except Exception:
        pass

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete):
            actor = entry.user
            actor_id_str = str(actor.id)
            
            if actor_id_str in bot_blacklist:
                await take_action(guild, actor, "黑名單機器人")
                continue
            
            if actor_id_str in bot_whitelist or actor.id in server_whitelisted_bots[guild.id]:
                continue
            
            if await track_action(guild, actor, "channel_delete"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "短時間內大量刪除頻道")
            break
    except Exception:
        pass

@bot.event
async def on_member_remove(member):
    guild = member.guild
    await asyncio.sleep(2)
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
            if entry.target.id == member.id:
                actor = entry.user
                actor_id_str = str(actor.id)
                
                if actor_id_str in bot_blacklist:
                    await take_action(guild, actor, "黑名單機器人")
                    break
                
                if actor_id_str in bot_whitelist or actor.id in server_whitelisted_bots[guild.id]:
                    break
                
                if await track_action(guild, actor, "member_kick"):
                    asyncio.create_task(prompt_restore_on_suspect(guild))
                    await take_action(guild, actor, "短時間內大量踢出成員")
                break
    except Exception:
        pass

@bot.event
async def on_member_ban(guild, user):
    try:
        user_id_str = str(user.id)
        if user_id_str in bot_blacklist:
            return
        
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                actor = entry.user
                actor_id_str = str(actor.id)
                
                if actor_id_str in bot_blacklist:
                    await take_action(guild, actor, "黑名單機器人")
                    break
                
                if actor_id_str in bot_whitelist or actor.id in server_whitelisted_bots[guild.id]:
                    break
                
                if await track_action(guild, actor, "member_ban"):
                    asyncio.create_task(prompt_restore_on_suspect(guild))
                    await take_action(guild, actor, "短時間內大量封鎖成員")
                break
    except Exception:
        pass

@bot.event
async def on_guild_role_create(role):
    guild = role.guild
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_create):
            actor = entry.user
            actor_id_str = str(actor.id)
            
            if actor_id_str in bot_blacklist:
                await take_action(guild, actor, "黑名單機器人")
                break
            
            if actor_id_str in bot_whitelist or actor.id in server_whitelisted_bots[guild.id]:
                break
            
            if await track_action(guild, actor, "role_create"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "短時間內大量建立角色")
            break
    except Exception:
        pass

# Slash commands

@bot.tree.command(name="status", description="檢查 AntiNuke360 狀態")
async def status(interaction: discord.Interaction):
    embed = discord.Embed(title="AntiNuke360 狀態", color=discord.Color.green())
    embed.description = "AntiNuke360 運行狀態:"
    embed.add_field(name="系統", value="啟用", inline=False)
    embed.add_field(name="最大動作次數", value=str(PROTECTION_CONFIG["max_actions"]), inline=False)
    embed.add_field(name="偵測時間窗 (秒)", value=str(PROTECTION_CONFIG["window_seconds"]), inline=False)
    embed.add_field(name="全域黑名單機器人", value=str(len(bot_blacklist)), inline=False)
    embed.add_field(name="全域白名單機器人", value=str(len(bot_whitelist)), inline=False)
    embed.add_field(name="本伺服器白名單機器人", value=str(len(server_whitelisted_bots[interaction.guild.id])), inline=False)
    has_snapshot = snapshot_is_valid(load_snapshot_file(interaction.guild.id))
    embed.add_field(name="伺服器快照", value=f"{'有有效快照' if has_snapshot else '無有效快照'}", inline=False)
    embed.add_field(name="自訂狀態文字", value=f"已啟用 ({len(STATUS_MESSAGES)} 個，每 10 秒輪流)", inline=False)
    embed.set_footer(text=f"AntiNuke360 {VERSION} | 防護參數已固定")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="scan-blacklist", description="掃描並停權伺服器中的黑名單成員 (管理員)")
async def scan_blacklist(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        scan_count, banned_count = await scan_and_ban_blacklist(interaction.guild)
        embed = discord.Embed(title="黑名單掃描完成", color=discord.Color.green())
        embed.description = f"已掃描伺服器中的成員並停權黑名單帳號"
        embed.add_field(name="掃描人數", value=str(scan_count), inline=True)
        embed.add_field(name="停權人數", value=str(banned_count), inline=True)
        embed.add_field(name="伺服器", value=interaction.guild.name, inline=False)
        embed.set_footer(text="AntiNuke360 v1.1.1")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        embed = discord.Embed(title="掃描失敗", color=discord.Color.red())
        embed.description = f"掃描伺服器時出錯: {str(e)}"
        embed.set_footer(text="AntiNuke360 v1.1.1")
        await interaction.followup.send(embed=embed)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="add-server-white", description="將機器人加入本伺服器白名單 (管理員)")
@app_commands.describe(bot_id="機器人 ID")
async def add_server_white(interaction: discord.Interaction, bot_id: str):
    try:
        bot_id_int = int(bot_id)
    except Exception:
        await interaction.response.send_message("無效的機器人 ID", ephemeral=True)
        return
    gid = interaction.guild.id
    if bot_id_int in server_whitelisted_bots[gid]:
        await interaction.response.send_message(f"該機器人已在本伺服器白名單中", ephemeral=True)
        return
    server_whitelisted_bots[gid].add(bot_id_int)
    save_server_whitelist()
    embed = discord.Embed(title="已加入本伺服器白名單", color=discord.Color.green())
    embed.description = f"機器人 ID: `{bot_id}` 已加入本伺服器白名單"
    embed.add_field(name="伺服器", value=interaction.guild.name, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="remove-server-white", description="從本伺服器白名單移除機器人 (管理員)")
@app_commands.describe(bot_id="機器人 ID")
async def remove_server_white(interaction: discord.Interaction, bot_id: str):
    try:
        bot_id_int = int(bot_id)
    except Exception:
        await interaction.response.send_message("無效的機器人 ID", ephemeral=True)
        return
    gid = interaction.guild.id
    if bot_id_int not in server_whitelisted_bots[gid]:
        await interaction.response.send_message(f"該機器人不在本伺服器白名單中", ephemeral=True)
        return
    server_whitelisted_bots[gid].discard(bot_id_int)
    save_server_whitelist()
    embed = discord.Embed(title="已從本伺服器白名單移除", color=discord.Color.red())
    embed.description = f"機器人 ID: `{bot_id}` 已從本伺服器白名單移除"
    embed.add_field(name="伺服器", value=interaction.guild.name, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="server-whitelist", description="查看本伺服器白名單 (管理員)")
async def server_whitelist(interaction: discord.Interaction):
    gid = interaction.guild.id
    bots = server_whitelisted_bots[gid]
    if not bots:
        await interaction.response.send_message("本伺服器白名單為空", ephemeral=True)
        return
    lines = [f"{i+1}. `{bot_id}`" for i, bot_id in enumerate(sorted(bots)[:50])]
    embed = discord.Embed(title=f"本伺服器白名單 ({len(bots)})", color=discord.Color.blue())
    embed.description = "\n".join(lines)
    if len(bots) > 50:
        embed.add_field(name="提示", value=f"還有 {len(bots) - 50} 個機器人未顯示", inline=False)
    embed.add_field(name="伺服器", value=interaction.guild.name, inline=True)
    embed.set_footer(text="AntiNuke360 v1.1.1")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="add-black", description="將機器人加入全域黑名單 (開發者)")
@app_commands.describe(bot_id="機器人 ID", reason="原因")
async def add_black(interaction: discord.Interaction, bot_id: str, reason: str = ""):
    global bot_blacklist
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if bot_id in bot_blacklist:
        await interaction.response.send_message(f"該機器人已在黑名單中", ephemeral=True)
        return
    bot_blacklist[bot_id] = {"name": bot_id, "reason": reason, "timestamp": time.time(), "guilds_detected": []}
    save_blacklist(bot_blacklist)
    await interaction.response.defer()
    embed = discord.Embed(title="已加入黑名單", color=discord.Color.red())
    embed.description = f"機器人 ID: `{bot_id}` 已加入全域黑名單"
    embed.add_field(name="原因", value=reason if reason else "無", inline=False)
    embed.set_footer(text="AntiNuke360 v1.1.1")
    await interaction.followup.send(embed=embed)
    await scan_blacklist_all_guilds()

@bot.tree.command(name="remove-black", description="從全域黑名單移除機器人 (開發者)")
@app_commands.describe(bot_id="機器人 ID")
async def remove_black(interaction: discord.Interaction, bot_id: str):
    global bot_blacklist
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if bot_id not in bot_blacklist:
        await interaction.response.send_message(f"該機器人不在黑名單中", ephemeral=True)
        return
    del bot_blacklist[bot_id]
    save_blacklist(bot_blacklist)
    embed = discord.Embed(title="已從黑名單移除", color=discord.Color.green())
    embed.description = f"機器人 ID: `{bot_id}` 已從全域黑名單移除"
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="add-white", description="將機器人加入全域白名單 (開發者)")
@app_commands.describe(bot_id="機器人 ID", reason="原因")
async def add_white(interaction: discord.Interaction, bot_id: str, reason: str = ""):
    global bot_whitelist
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if bot_id in bot_whitelist:
        await interaction.response.send_message(f"該機器人已在白名單中", ephemeral=True)
        return
    bot_whitelist[bot_id] = {"name": bot_id, "reason": reason, "timestamp": time.time()}
    save_whitelist(bot_whitelist)
    embed = discord.Embed(title="已加入白名單", color=discord.Color.green())
    embed.description = f"機器人 ID: `{bot_id}` 已加入全域白名單"
    embed.add_field(name="原因", value=reason if reason else "無", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="remove-white", description="從全域白名單移除機器人 (開發者)")
@app_commands.describe(bot_id="機器人 ID")
async def remove_white(interaction: discord.Interaction, bot_id: str):
    global bot_whitelist
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if bot_id not in bot_whitelist:
        await interaction.response.send_message(f"該機器人不在白名單中", ephemeral=True)
        return
    del bot_whitelist[bot_id]
    save_whitelist(bot_whitelist)
    embed = discord.Embed(title="已從白名單移除", color=discord.Color.red())
    embed.description = f"機器人 ID: `{bot_id}` 已從全域白名單移除"
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="blacklist", description="查看全域黑名單 (開發者)")
async def blacklist(interaction: discord.Interaction):
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if not bot_blacklist:
        await interaction.response.send_message("黑名單為空", ephemeral=True)
        return
    lines = []
    for bot_id, info in bot_blacklist.items():
        lines.append(f"ID: `{bot_id}` | 名稱: {info.get('name', '未知')} | 原因: {info.get('reason', '無')}")
    embed = discord.Embed(title=f"全域黑名單 ({len(bot_blacklist)})", color=discord.Color.red())
    embed.description = "\n".join(lines[:10])
    if len(lines) > 10:
        embed.add_field(name="提示", value=f"還有 {len(lines) - 10} 個機器人未顯示", inline=False)
    embed.set_footer(text="AntiNuke360 v1.1.1")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="whitelist-list", description="查看全域白名單 (開發者)")
async def whitelist_list(interaction: discord.Interaction):
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if not bot_whitelist:
        await interaction.response.send_message("白名單為空", ephemeral=True)
        return
    lines = []
    for bot_id, info in bot_whitelist.items():
        lines.append(f"ID: `{bot_id}` | 名稱: {info.get('name', '未知')} | 原因: {info.get('reason', '無')}")
    embed = discord.Embed(title=f"全域白名單 ({len(bot_whitelist)})", color=discord.Color.green())
    embed.description = "\n".join(lines[:10])
    if len(lines) > 10:
        embed.add_field(name="提示", value=f"還有 {len(lines) - 10} 個機器人未顯示", inline=False)
    embed.set_footer(text="AntiNuke360 v1.1.1")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="scan-all-guilds", description="在所有伺服器掃描並停權黑名單成員 (開發者)")
async def scan_all_guilds(interaction: discord.Interaction):
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        await scan_blacklist_all_guilds()
        embed = discord.Embed(title="全域黑名單掃描完成", color=discord.Color.green())
        embed.description = "已在所有伺服器中掃描並停權黑名單成員"
        embed.set_footer(text="AntiNuke360 v1.1.1")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        embed = discord.Embed(title="全域掃描失敗", color=discord.Color.red())
        embed.description = f"掃描時出錯: {str(e)}"
        embed.set_footer(text="AntiNuke360 v1.1.1")
        await interaction.followup.send(embed=embed)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="restore-snapshot", description="還原本伺服器的備份快照 (管理員)")
async def restore_snapshot_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    snapshot = load_snapshot_file(guild.id)
    if not snapshot or not snapshot_is_valid(snapshot):
        await interaction.followup.send("伺服器沒有有效的快照可供還原或已過期。", ephemeral=True)
        return
    remaining = snapshot_time_remaining(snapshot)
    await interaction.followup.send(f"開始還原快照 (剩餘有效時間: {remaining//3600} 小時 {(remaining%3600)//60} 分鐘)。這可能需要一段時間且會先嘗試刪除可刪除的現有頻道與身分組...", ephemeral=True)
    ok, msg = await perform_restore(guild, ctx_sender=interaction.user)
    if ok:
        await interaction.followup.send(f"還原完成: {msg}", ephemeral=True)
    else:
        await interaction.followup.send(f"還原失敗: {msg}", ephemeral=True)

@bot.tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("無權限", ephemeral=True)

if __name__ == "__main__":
    if not TOKEN:
        print("錯誤: 找不到 DISCORD_TOKEN")
    else:
        print(f"啟動 AntiNuke360 {VERSION}...")
        bot.run(TOKEN)

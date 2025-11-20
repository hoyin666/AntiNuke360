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

# 新增：MySQL
import mysql.connector
from mysql.connector import Error

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEVELOPER_ID = 800536911378251787

# 這三個 JSON 不再使用，但保留常數名稱以免其他地方硬編碼
BLACKLIST_FILE = "bot_blacklist.json"
WHITELIST_FILE = "bot_whitelist.json"
SERVER_WHITELIST_FILE = "server_whitelist.json"
GUILDS_FILE = "guilds_data.json"

SNAPSHOT_DIR = Path("snapshots")
SNAPSHOT_TTL_SECONDS = 72 * 3600  # 72 hours
VERSION = "v1.3.0"  # 版本號

SNAPSHOT_DIR.mkdir(exist_ok=True)

user_actions = defaultdict(lambda: defaultdict(lambda: defaultdict(deque)))
whitelisted_users = defaultdict(set)
# server_whitelists structure (in-memory):
# guild_id -> {
#   "anti_kick": set(ids),
#   "temporary": {id: expiry_ts},
#   "permanent": set(ids),
#   "log_channel": channel_id or None
# }
server_whitelists = defaultdict(lambda: {"anti_kick": set(), "temporary": {}, "permanent": set(), "log_channel": None})
banned_in_session = defaultdict(set)
notified_bans = defaultdict(set)

# 權限錯誤監控
permission_errors = defaultdict(deque)

# 防止短時間內重複詢問還原
restore_prompted = defaultdict(lambda: 0)

# 反被盜帳設定
anti_hijack_settings = defaultdict(lambda: {"enabled": True})

# 反被盜帳偵測用：guild_id -> user_id -> content -> deque[(timestamp, channel_id)]
hijack_tracker = defaultdict(lambda: defaultdict(lambda: defaultdict(deque)))

# 固定防護參數
PROTECTION_CONFIG = {
    "max_actions": 7,
    "window_seconds": 10,
    "enabled": True
}

# 臨時白名單容許值（針對敏感操作）
TEMP_WHITELIST_MAX = 15
TEMP_WHITELIST_WINDOW = 15  # seconds
TEMP_WHITELIST_TTL = 3600  # 1 hour

# 敏感操作清單
SENSITIVE_ACTIONS = {
    "channel_create",
    "channel_delete",
    "member_kick",
    "member_ban",
    "role_create",
    "webhook_create"
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
    "sorry, I am gay",
    "洋蔥女裝：來都來了",
    "你們都是佬🛐"
]

# ========== MySQL 連線 & SQL 存取函式 ==========

MYSQL_HOST = os.getenv("MYSQL_HOST", "c6f22e13-cd22-42c9-b4e9-6f5055d1aebd")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("MYSQL_DB", "")


def get_db_connection():
    return mysql.connector.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        connection_timeout=10,
    )


def ensure_snapshots_table():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                guild_id BIGINT PRIMARY KEY,
                snapshot_json LONGTEXT NOT NULL,
                updated_at DOUBLE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """
        )
        conn.commit()
        cursor.close()
        conn.close()
        print("[DB] 已確認 snapshots 資料表存在。")
    except Error as e:
        print(f"[DB ERROR] 建立/確認 snapshots 表失敗: {e}")


def load_blacklist():
    """從 MySQL 載入全域黑名單到記憶體 dict，結構維持與舊 JSON 一樣。"""
    data = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT bot_id, name, reason, timestamp, guilds_detected FROM bot_blacklist")
        for row in cursor.fetchall():
            bot_id = str(row["bot_id"])
            guilds = []
            if row["guilds_detected"]:
                try:
                    guilds = json.loads(row["guilds_detected"])
                except Exception:
                    guilds = []
            data[bot_id] = {
                "name": row.get("name") or bot_id,
                "reason": row.get("reason") or "",
                "timestamp": float(row["timestamp"]) if row["timestamp"] is not None else 0,
                "guilds_detected": guilds,
            }
        cursor.close()
        conn.close()
        print(f"[DB] 從 MySQL 載入黑名單 {len(data)} 筆")
    except Error as e:
        print(f"[DB ERROR] 載入黑名單失敗: {e}")
    return data


def save_blacklist(data):
    """將記憶體中的黑名單 dict 寫回 MySQL。"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bot_blacklist")
        insert_sql = """
            INSERT INTO bot_blacklist (bot_id, name, reason, timestamp, guilds_detected)
            VALUES (%s, %s, %s, %s, %s)
        """
        rows = 0
        for bot_id_str, info in data.items():
            try:
                bot_id = int(bot_id_str)
            except ValueError:
                continue
            name = info.get("name", bot_id_str)
            reason = info.get("reason", "")
            ts = info.get("timestamp", None)
            ts_val = float(ts) if ts is not None else None
            guilds = info.get("guilds_detected", [])
            guilds_str = json.dumps(guilds, ensure_ascii=False)
            cursor.execute(insert_sql, (bot_id, name, reason, ts_val, guilds_str))
            rows += 1
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[DB] 已儲存黑名單 {rows} 筆到 MySQL")
    except Error as e:
        print(f"[DB ERROR] 儲存黑名單失敗: {e}")


def load_whitelist():
    """從 MySQL 載入全域白名單到記憶體 dict。"""
    data = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT bot_id, name, reason, timestamp FROM bot_whitelist")
        for row in cursor.fetchall():
            bot_id = str(row["bot_id"])
            data[bot_id] = {
                "name": row.get("name") or bot_id,
                "reason": row.get("reason") or "",
                "timestamp": float(row["timestamp"]) if row["timestamp"] is not None else 0,
            }
        cursor.close()
        conn.close()
        print(f"[DB] 從 MySQL 載入白名單 {len(data)} 筆")
    except Error as e:
        print(f"[DB ERROR] 載入白名單失敗: {e}")
    return data


def save_whitelist(data):
    """將全域白名單 dict 寫回 MySQL。"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bot_whitelist")
        insert_sql = """
            INSERT INTO bot_whitelist (bot_id, name, reason, timestamp)
            VALUES (%s, %s, %s, %s)
        """
        rows = 0
        for bot_id_str, info in data.items():
            try:
                bot_id = int(bot_id_str)
            except ValueError:
                continue
            name = info.get("name", bot_id_str)
            reason = info.get("reason") or ""
            ts = info.get("timestamp", None)
            ts_val = float(ts) if ts is not None else None
            cursor.execute(insert_sql, (bot_id, name, reason, ts_val))
            rows += 1
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[DB] 已儲存白名單 {rows} 筆到 MySQL")
    except Error as e:
        print(f"[DB ERROR] 儲存白名單失敗: {e}")


def load_server_whitelist():
    """
    從 MySQL 載入 server_whitelist 表，填滿 in-memory 的 server_whitelists 結構。
    結構同原本 JSON 轉換後的記憶體格式。
    """
    global server_whitelists
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT guild_id, anti_kick_user_id, temp_user_id, temp_expiry, perm_user_id, log_channel_id
            FROM server_whitelist
            """
        )
        server_whitelists = defaultdict(lambda: {"anti_kick": set(), "temporary": {}, "permanent": set(), "log_channel": None})
        for row in cursor.fetchall():
            gid = int(row["guild_id"])
            anti = server_whitelists[gid]["anti_kick"]
            temp = server_whitelists[gid]["temporary"]
            perm = server_whitelists[gid]["permanent"]

            if row["anti_kick_user_id"] is not None:
                anti.add(int(row["anti_kick_user_id"]))
            if row["temp_user_id"] is not None:
                uid = int(row["temp_user_id"])
                expiry = float(row["temp_expiry"]) if row["temp_expiry"] is not None else time.time()
                temp[uid] = expiry
            if row["perm_user_id"] is not None:
                perm.add(int(row["perm_user_id"]))
            if row["log_channel_id"] is not None:
                server_whitelists[gid]["log_channel"] = int(row["log_channel_id"])

        cursor.close()
        conn.close()
        print(f"[DB] 從 MySQL 載入 server_whitelist，guild 數量: {len(server_whitelists)}")
    except Error as e:
        print(f"[DB ERROR] 載入 server_whitelist 失敗: {e}")
        return {}


def save_server_whitelist():
    """
    將 in-memory 的 server_whitelists 寫回 MySQL。
    邏輯：清空表，再依照記憶體重建所有列。
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM server_whitelist")
        insert_sql = """
            INSERT INTO server_whitelist
            (guild_id, anti_kick_user_id, temp_user_id, temp_expiry, perm_user_id, log_channel_id)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        rows = 0
        for gid, v in server_whitelists.items():
            anti = v.get("anti_kick", set()) or set()
            perm = v.get("permanent", set()) or set()
            temporary = v.get("temporary", {}) or {}
            log_ch = v.get("log_channel", None)
            log_ch_id = int(log_ch) if log_ch is not None else None

            for uid in anti:
                cursor.execute(insert_sql, (gid, uid, None, None, None, log_ch_id))
                rows += 1
            for uid in perm:
                cursor.execute(insert_sql, (gid, None, None, None, uid, log_ch_id))
                rows += 1
            for uid, expiry in temporary.items():
                cursor.execute(insert_sql, (gid, None, uid, float(expiry), None, log_ch_id))
                rows += 1
            if not anti and not perm and not temporary and log_ch_id is not None:
                cursor.execute(insert_sql, (gid, None, None, None, None, log_ch_id))
                rows += 1

        conn.commit()
        cursor.close()
        conn.close()
        print(f"[DB] 已儲存 server_whitelist {rows} 列到 MySQL")
    except Error as e:
        print(f"[DB ERROR] 儲存 server_whitelist 失敗: {e}")


def load_guilds_data():
    """
    從 MySQL 載入 guilds_data，回傳 dict 結構與原 JSON 相同：
    {
      "guild_id_str": {
        "joined_at": float,
        "welcome_channel_id": int or None
      }
    }
    """
    data = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT guild_id, joined_at, welcome_channel_id FROM guilds_data")
        for row in cursor.fetchall():
            gid_str = str(row["guild_id"])
            joined_at = float(row["joined_at"]) if row["joined_at"] is not None else time.time()
            welcome = row["welcome_channel_id"]
            welcome_id = int(welcome) if welcome is not None else None
            data[gid_str] = {
                "joined_at": joined_at,
                "welcome_channel_id": welcome_id
            }
        cursor.close()
        conn.close()
        print(f"[DB] 從 MySQL 載入 guilds_data {len(data)} 筆")
    except Error as e:
        print(f"[DB ERROR] 載入 guilds_data 失敗: {e}")
    return data


def save_guilds_data(data):
    """
    將 guilds_data dict 寫回 MySQL。
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM guilds_data")
        insert_sql = """
            INSERT INTO guilds_data (guild_id, joined_at, welcome_channel_id)
            VALUES (%s, %s, %s)
        """
        rows = 0
        for gid_str, info in data.items():
            try:
                gid = int(gid_str)
            except ValueError:
                continue
            joined_at = float(info.get("joined_at", time.time()))
            welcome = info.get("welcome_channel_id", None)
            welcome_id = int(welcome) if welcome is not None else None
            cursor.execute(insert_sql, (gid, joined_at, welcome_id))
            rows += 1
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[DB] 已儲存 guilds_data {rows} 筆到 MySQL")
    except Error as e:
        print(f"[DB ERROR] 儲存 guilds_data 失敗: {e}")


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


# 啟動時從 DB 載入黑白名單 & server_whitelist，並確認 snapshots 表
bot_blacklist = load_blacklist()
bot_whitelist = load_whitelist()
load_server_whitelist()
ensure_snapshots_table()

class AntiNukeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.moderation = True
        intents.message_content = True
        intents.presences = True
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
    print(f"[READY] 快照 TTL: {SNAPSHOT_TTL_SECONDS} 秒（存於 MySQL）")
    print("=" * 60)
    
    if not bot.change_status_loop.is_running():
        bot.change_status_loop.start()
        print("[STATUS] 已啟動狀態文字循環")
    if not check_admin_permission_loop.is_running():
        check_admin_permission_loop.start()
        print("[PERMISSION CHECK] 已啟動每小時 Administrator 權限檢查循環")

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

# 每小時檢查是否具有 Administrator 權限
@tasks.loop(hours=1)
async def check_admin_permission_loop():
    try:
        for guild in bot.guilds:
            try:
                me = guild.me
                if not me or not me.guild_permissions.administrator:
                    print(f"[PERMISSION CHECK LOOP] 伺服器 {guild.name} 缺少 Administrator 權限，通知並離開")

                    recipients = []
                    owner = guild.owner
                    if owner:
                        recipients.append(owner)

                    admins = [m for m in guild.members if m.guild_permissions.administrator and not m.bot]

                    status_priority = {"online": 0, "idle": 1, "dnd": 2, "offline": 3, None: 3}
                    def admin_sort_key(m):
                        st = getattr(m, "status", None)
                        pr = status_priority.get(str(st), 3)
                        joined = m.joined_at.timestamp() if m.joined_at else 0
                        return (pr, -joined)

                    admins_sorted = sorted(admins, key=admin_sort_key)

                    for a in admins_sorted:
                        if a not in recipients:
                            recipients.append(a)
                        if len(recipients) >= 6:
                            break

                    text = (
                        f"您好，這裡是 **AntiNuke360 {VERSION}**。\n\n"
                        "機器人需要 **Administrator** 權限才能正常運作，包含偵測與阻止 nuke 攻擊、封鎖黑名單機器人，"
                        "以及在伺服器遭受破壞時進行自動還原等功能。\n\n"
                        "目前我在此伺服器中沒有 **Administrator** 權限，因此將自動離開。\n"
                        "請在重新邀請本機器人時，勾選 **Administrator** 權限。\n\n"
                        "若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。"
                    )

                    for r in recipients:
                        try:
                            dm = await r.create_dm()
                            await dm.send(text)
                        except Exception:
                            continue

                    try:
                        await guild.leave()
                        print(f"[PERMISSION CHECK LOOP] 已因缺少 Administrator 權限離開伺服器: {guild.name}")
                    except Exception as e:
                        print(f"[PERMISSION CHECK LOOP ERROR] 無法離開伺服器 {guild.name}: {e}")
            except Exception as e:
                print(f"[PERMISSION CHECK LOOP ERROR] 在伺服器 {guild.name} 檢查 Administrator 權限時發生錯誤: {e}")
    except Exception as e:
        print(f"[PERMISSION CHECK LOOP ERROR] 每小時檢查循環發生錯誤: {e}")

# ========== Snapshot utilities：用 MySQL 儲存 ==========

def snapshot_path(guild_id: int) -> Path:
    return SNAPSHOT_DIR / f"{guild_id}.json"

def save_snapshot_file(guild_id: int, data: dict):
    """
    將 snapshot 以 JSON 字串存入 MySQL 的 snapshots 表。
    結構與原 JSON 檔內容相同，只是儲存位置改為 DB。
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        snapshot_json = json.dumps(data, ensure_ascii=False)
        now_ts = time.time()
        cursor.execute(
            """
            INSERT INTO snapshots (guild_id, snapshot_json, updated_at)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                snapshot_json = VALUES(snapshot_json),
                updated_at = VALUES(updated_at)
            """,
            (guild_id, snapshot_json, now_ts),
        )
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[SNAPSHOT] 已將伺服器 {guild_id} 快照儲存至 MySQL snapshots 表")
    except Error as e:
        print(f"[SNAPSHOT ERROR] 儲存快照至 MySQL 失敗: {e}")

def load_snapshot_file(guild_id: int):
    """
    從 MySQL snapshots 表讀取 snapshot JSON，回傳 dict。
    若不存在則回傳 None。
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT snapshot_json FROM snapshots WHERE guild_id = %s", (guild_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            return None
        try:
            data = json.loads(row["snapshot_json"])
            return data
        except Exception as e:
            print(f"[SNAPSHOT ERROR] 解析 MySQL 中快照 JSON 失敗: {e}")
            return None
    except Error as e:
        print(f"[SNAPSHOT ERROR] 從 MySQL 讀取快照失敗: {e}")
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
            else:
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
            else:
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
        f"AntiNuke360 偵測到你的伺服器可能遭受大規模破壞攻擊。\n"
        f"AntiNuke360 偵測到一個快照可用，剩餘有效時間: {remaining//3600} 小時 {(remaining%3600)//60} 分鐘。\n"
        "回覆 `Y` 以自動還原伺服器結構（會先嘗試刪除可刪除的身分組與頻道），或回覆 `N` 以略過。\n"
        "您也可以稍後使用斜線指令 `/restore-snapshot` 手動還原。"
    )
    sent_location = None
    try:
        if owner:
            dm = await owner.create_dm()
            try:
                await dm.send(message_text + "\n\n若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。")
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
                    await resp.channel.send(notify + "\n\n若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。")
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
                    await resp.channel.send(notify + "\n\n若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。")
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
                await dm.send(notify + "\n\n若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。")
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
                    anti_kick = server_whitelists[guild.id]["anti_kick"]
                    if member.id in anti_kick:
                        print(f"[SCAN] {member} 在伺服器防踢白名單中，跳過停權")
                        try:
                            embed = discord.Embed(title="[AntiNuke360 記錄 - 防踢白名單生效]", color=discord.Color.orange())
                            embed.description = (
                                f"被列入全域黑名單的使用者/機器人 `{member}` (ID: `{member.id}`) 在伺服器 `{guild.name}` 中被跳過停權，"
                                "因為其已被加入本伺服器的防踢白名單。\n\n"
                                "若您要讓黑名單用戶在此伺服器中不被自動停權，可使用 `/add-server-anti-kick` 將目標 ID 加入防踢白名單。"
                            )
                            embed.set_footer(text="AntiNuke360 v1.3.0")
                            await send_log(guild, embed=embed)
                        except Exception:
                            pass
                        continue

                    if member.id not in banned_in_session[guild.id]:
                        blacklist_info = bot_blacklist[user_id_str]
                        ban_reason = blacklist_info.get('reason', '黑名單機器人')
                        await guild.ban(member, reason=f"AntiNuke360: {ban_reason}")
                        banned_in_session[guild.id].add(member.id)
                        banned_count += 1
                        print(f"[SCAN] 已停權黑名單成員: {member} (ID: {member.id})")

                        try:
                            embed = discord.Embed(title="[AntiNuke360 黑名單停權]", color=discord.Color.red())
                            embed.description = (
                                f"使用者/機器人 `{member}` (ID: `{member.id}`) 已因黑名單紀錄在伺服器 `{guild.name}` 被自動停權。\n\n"
                                f"黑名單原因: {ban_reason}\n\n"
                                "如果您確定此帳號在本伺服器是安全的、並希望未來不要再被自動停權，\n"
                                "伺服器擁有者可以使用 `/add-server-anti-kick` 指令將其加入本伺服器的防踢白名單。"
                            )
                            embed.set_footer(text="AntiNuke360 v1.3.0")
                            await send_log(guild, embed=embed)
                        except Exception:
                            pass
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
            embed.description = f"""AntiNuke360 在伺服器 '{guild.name}' 中 1 分鐘內遇到 10 次權限不足錯誤 (403 Forbidden)。

請確保 Bot 的身份組具有以下權限：
- 封禁成員 (Ban Members)
- 踢出成員 (Kick Members)
- 管理頻道 (Manage Channels)
- 管理身分組 (Manage Roles)
- 檢視審核日誌 (View Audit Log)

權限不足會導致無法正常防護伺服器，Bot 將自動離開此伺服器。"""
            embed.set_footer(text="AntiNuke360 v1.3.0")
            try:
                await send_log(guild, embed=embed)
                print(f"[PERMISSION] 已向伺服器所有者/記錄頻道發送通知")
            except Exception as e:
                print(f"[PERMISSION ERROR] 無法發送通知: {e}")
        except Exception as e:
            print(f"[PERMISSION ERROR] 構建嵌入訊息失敗: {e}")
        try:
            await guild.leave()
            print(f"[PERMISSION] 已自動離開伺服器: {guild.name}")
        except Exception as e:
            print(f"[PERMISSION ERROR] 無法離開伺服器: {e}")
        permission_errors[gid].clear()

# Helper functions for server whitelist checks and management
def purge_expired_temporary(guild_id: int):
    now = time.time()
    temp = server_whitelists[guild_id]["temporary"]
    remove = [uid for uid, expiry in temp.items() if expiry <= now]
    for uid in remove:
        del temp[uid]

def is_permanent_whitelisted(guild_id: int, user_id: int) -> bool:
    return user_id in server_whitelists[guild_id]["permanent"]

def is_temporary_whitelisted(guild_id: int, user_id: int) -> bool:
    purge_expired_temporary(guild_id)
    return user_id in server_whitelists[guild_id]["temporary"]

def is_anti_kick_whitelisted(guild_id: int, user_id: int) -> bool:
    return user_id in server_whitelists[guild_id]["anti_kick"]

def add_temporary_whitelist(guild_id: int, user_id: int):
    server_whitelists[guild_id]["temporary"][user_id] = time.time() + TEMP_WHITELIST_TTL
    save_server_whitelist()

def remove_temporary_whitelist(guild_id: int, user_id: int):
    temp = server_whitelists[guild_id]["temporary"]
    if user_id in temp:
        del temp[user_id]
        save_server_whitelist()

def add_permanent_whitelist(guild_id: int, user_id: int):
    server_whitelists[guild_id]["permanent"].add(user_id)
    save_server_whitelist()

def remove_permanent_whitelist(guild_id: int, user_id: int):
    server_whitelists[guild_id]["permanent"].discard(user_id)
    save_server_whitelist()

def add_anti_kick_whitelist(guild_id: int, user_id: int):
    server_whitelists[guild_id]["anti_kick"].add(user_id)
    save_server_whitelist()

def remove_anti_kick_whitelist(guild_id: int, user_id: int):
    server_whitelists[guild_id]["anti_kick"].discard(user_id)
    save_server_whitelist()

def set_log_channel_for_guild(guild_id: int, channel_id: int):
    server_whitelists[guild_id]["log_channel"] = channel_id
    save_server_whitelist()

def get_log_channel_for_guild(guild_id: int):
    return server_whitelists[guild_id].get("log_channel")

async def send_log(guild: discord.Guild, content: str = None, embed: discord.Embed = None):
    log_ch_id = get_log_channel_for_guild(guild.id)
    sent = False
    if log_ch_id:
        ch = guild.get_channel(log_ch_id)
        if ch and isinstance(ch, discord.TextChannel):
            try:
                if ch.permissions_for(guild.me).send_messages:
                    await ch.send(content=content, embed=embed)
                    sent = True
                    return True
            except Exception:
                sent = False
    owner = guild.owner
    recipients = []
    if owner:
        recipients.append(owner)
    admins = [m for m in guild.members if (m.guild_permissions.administrator or m.guild_permissions.manage_guild) and not m.bot]
    status_priority = {"online": 0, "idle": 1, "dnd": 2, "offline": 3, None: 3}
    def admin_sort_key(m):
        st = getattr(m, "status", None)
        pr = status_priority.get(str(st), 3)
        joined = m.joined_at.timestamp() if m.joined_at else 0
        return (pr, -joined)
    admins_sorted = sorted(admins, key=admin_sort_key)
    for a in admins_sorted:
        if a not in recipients:
            recipients.append(a)
        if len(recipients) >= 6:
            break
    for r in recipients:
        try:
            dm = await r.create_dm()
            if embed is not None:
                if embed.footer and embed.footer.text:
                    footer_text = embed.footer.text + " | 若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。"
                else:
                    footer_text = "若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。"
                embed.set_footer(text=footer_text)
            else:
                if content is None:
                    content = ""
                suffix = "\n\n若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。"
                content = (content or "") + suffix
            await dm.send(content=content, embed=embed)
            sent = True
        except Exception:
            continue
    return sent

async def track_action(guild, user, action_type):
    if guild is None or user is None:
        return False
    if user.id == guild.owner_id:
        return False
    if is_permanent_whitelisted(guild.id, user.id):
        return False
    purge_expired_temporary(guild.id)
    if user.id in whitelisted_users[guild.id]:
        return False
    if str(user.id) in bot_whitelist:
        return False

    now = time.time()
    if action_type in SENSITIVE_ACTIONS and is_temporary_whitelisted(guild.id, user.id):
        max_count = TEMP_WHITELIST_MAX
        window = TEMP_WHITELIST_WINDOW
    else:
        max_count = PROTECTION_CONFIG["max_actions"]
        window = PROTECTION_CONFIG["window_seconds"]

    actions = user_actions[guild.id][user.id][action_type]
    actions.append(now)
    while actions and now - actions[0] > window:
        actions.popleft()
    current_count = len(actions)
    if current_count > max_count:
        return True
    return False

async def take_action(guild, user, reason):
    global bot_blacklist, notified_bans
    gid = guild.id
    uid = user.id

    if uid in banned_in_session[guild.id]:
        return

    print(f"[ACTION] 開始處理 {user} (ID: {uid})")
    try:
        await guild.ban(user, reason=f"AntiNuke360: {reason}")
        banned_in_session[guild.id].add(uid)
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
            await scan_blacklist_all_guilds()

        if uid not in notified_bans[gid] and guild.owner:
            notified_bans[gid].add(uid)
            embed = discord.Embed(title="[AntiNuke360 警報]", color=discord.Color.red())
            embed.description = (
                f"使用者 `{user}` (ID: `{uid}`) 已在伺服器 `{guild.name}` 被自動封鎖。\n\n"
                f"原因: {reason}\n\n"
                "若此帳號在本伺服器是被允許的，伺服器擁有者可以使用 `/add-server-anti-kick` 指令\n"
                "將其加入本伺服器的防踢白名單，以避免未來再度因黑名單或異常行為被自動封鎖。"
            )
            embed.add_field(name="伺服器", value=guild.name, inline=True)
            embed.add_field(name="伺服器 ID", value=str(gid), inline=True)
            embed.set_footer(text="AntiNuke360 v1.3.0")
            try:
                await send_log(guild, embed=embed)
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

本地白名單系統 (新增)
- 分為：防踢白名單 / 臨時白名單 / 永久白名單
- 防踢白名單：允許被列入全域黑名單的帳號/機器人加入此伺服器（僅限伺服器擁有者管理）
- 臨時白名單：在 1 小時內對敏感操作放寬至 15 次 / 15 秒（管理員可增刪）
- 永久白名單：對敏感操作完全免疫，無時間限制（僅限伺服器擁有者管理）

固定防護參數
- 最優的靈敏度設置
- 無法調整(確保一致性)

進階保護 (v1.2.3)
- 黑名單訊息即時屏蔽（非防踢白名單）
- 反外部應用程式刷屏（5 秒內 3 則相同訊息，支援禁言設定）
- 反被盜帳（5 秒內在不同頻道發送 3 次相同訊息，DM 邀請 + 踢出/只刪訊息）""",
            inline=False
        )
        
        embed.add_field(
            name="使用指南",
            value="""管理員指令:
/status - 查看防護狀態
/add-server-temp [ID] - 將成員或機器人加入本伺服器臨時白名單 (管理員，可移除)
/remove-server-temp [ID]
/set-log-channel [#channel] - 指定記錄頻道 (管理員)
/toggle-anti-hijack [on/off] - 開啟或關閉反被盜帳功能 (管理員)

伺服器擁有者指令:
/add-server-anti-kick [ID] - 防踢白名單 (僅擁有者)
/remove-server-anti-kick [ID]
/add-server-perm [ID] - 永久白名單 (僅擁有者)
/remove-server-perm [ID]

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
        
        embed.set_footer(text="AntiNuke360 v1.3.0 | 伺服器防護專家（Snapshot 已存於 MySQL）")
        
        await channel.send(embed=embed)
        print(f"[WELCOME] 已在伺服器 {guild.name} 創建歡迎頻道")
        
    except Exception as e:
        print(f"[WELCOME ERROR] 創建歡迎訊息失敗: {e}")

@bot.event
async def on_guild_join(guild):
    print(f"[JOIN] 已加入新伺服器: {guild.name} (ID: {guild.id})")
    add_to_guilds_data(guild.id)
    if guild.id not in server_whitelists:
        server_whitelists[guild.id] = {"anti_kick": set(), "temporary": {}, "permanent": set(), "log_channel": None}
        save_server_whitelist()
    await send_welcome_message(guild)

    async def delayed_admin_check(g: discord.Guild):
        try:
            await asyncio.sleep(600)
            if g not in bot.guilds:
                return
            me = g.me
            if not me or not me.guild_permissions.administrator:
                print(f"[PERMISSION CHECK] 在伺服器 {g.name} 中 10 分鐘後仍沒有 Administrator 權限，將通知並自動離開")

                recipients = []
                owner = g.owner
                if owner:
                    recipients.append(owner)

                admins = [m for m in g.members if m.guild_permissions.administrator and not m.bot]

                status_priority = {"online": 0, "idle": 1, "dnd": 2, "offline": 3, None: 3}
                def admin_sort_key(m):
                    st = getattr(m, "status", None)
                    pr = status_priority.get(str(st), 3)
                    joined = m.joined_at.timestamp() if m.joined_at else 0
                    return (pr, -joined)

                admins_sorted = sorted(admins, key=admin_sort_key)

                for a in admins_sorted:
                    if a not in recipients:
                        recipients.append(a)
                    if len(recipients) >= 6:
                        break

                text = (
                    f"您好，這裡是 **AntiNuke360 {VERSION}**。\n\n"
                    "機器人需要 **Administrator** 權限才能正常運作，包含偵測與阻止 nuke 攻擊、封鎖黑名單機器人，"
                    "以及在伺服器遭受破壞時進行自動還原等功能。\n\n"
                    "目前我在此伺服器中沒有 **Administrator** 權限，因此將自動離開。\n"
                    "請在重新邀請本機器人時，勾選 **Administrator** 權限。\n\n"
                    "若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。"
                )

                for r in recipients:
                    try:
                        dm = await r.create_dm()
                        await dm.send(text)
                    except Exception:
                        continue

                try:
                    await g.leave()
                    print(f"[PERMISSION CHECK] 已因缺少 Administrator 權限離開伺服器: {g.name}")
                except Exception as e:
                    print(f"[PERMISSION CHECK ERROR] 無法離開伺服器 {g.name}: {e}")
        except Exception as e:
            print(f"[PERMISSION CHECK ERROR] 在 on_guild_join 延遲檢查 Administrator 權限時發生錯誤: {e}")

    asyncio.create_task(delayed_admin_check(guild))

    async def retry_welcome_channel(g: discord.Guild):
        try:
            while True:
                if g not in bot.guilds:
                    print(f"[WELCOME RETRY] Bot 已不在伺服器 {g.name} 中，停止重試創建歡迎頻道")
                    return

                data = load_guilds_data()
                info = data.get(str(g.id), {})
                welcome_id = info.get("welcome_channel_id")
                has_welcome = False
                if welcome_id:
                    ch = g.get_channel(welcome_id)
                    if isinstance(ch, discord.TextChannel):
                        has_welcome = True

                if has_welcome:
                    print(f"[WELCOME RETRY] 已確認伺服器 {g.name} 擁有歡迎頻道，停止重試")
                    return

                print(f"[WELCOME RETRY] 伺服器 {g.name} 尚未成功建立歡迎頻道，嘗試重新建立...")
                await send_welcome_message(g)

                data = load_guilds_data()
                info = data.get(str(g.id), {})
                welcome_id = info.get("welcome_channel_id")
                has_welcome = False
                if welcome_id:
                    ch = g.get_channel(welcome_id)
                    if isinstance(ch, discord.TextChannel):
                        has_welcome = True

                if has_welcome:
                    print(f"[WELCOME RETRY] 已在伺服器 {g.name} 成功建立歡迎頻道 (重試)")
                    return

                await asyncio.sleep(60)
        except Exception as e:
            print(f"[WELCOME RETRY ERROR] 在重試建立歡迎頻道時發生錯誤 (伺服器: {g.name}): {e}")

    asyncio.create_task(retry_welcome_channel(guild))

@bot.event
async def on_guild_remove(guild):
    print(f"[LEAVE] 已從伺服器移除: {guild.name} (ID: {guild.id})")
    remove_from_guilds_data(guild.id)
    if guild.id in server_whitelists:
        del server_whitelists[guild.id]
        save_server_whitelist()
    if guild.id in permission_errors:
        del permission_errors[guild.id]

@bot.event
async def on_member_join(member):
    guild = member.guild
    user_id_str = str(member.id)
    
    if member.bot:
        try:
            await create_snapshot(guild)
        except Exception as e:
            print(f"[SNAPSHOT ERROR] 建立快照時發生錯誤: {e}")
    
    if user_id_str in bot_blacklist:
        if is_anti_kick_whitelisted(guild.id, member.id):
            print(f"[JOIN] {member} (全域黑名單但在伺服器防踢白名單) 加入伺服器 {guild.name}，允許")
            embed = discord.Embed(title="[AntiNuke360 記錄]", color=discord.Color.orange())
            embed.description = (
                f"被列入全域黑名單的使用者/機器人 `{member}` (ID: `{member.id}`) 被允許加入此伺服器，"
                "因為其在本伺服器的防踢白名單中。\n\n"
                "若您要讓特定黑名單用戶在本伺服器中不被自動停權，可以使用 `/add-server-anti-kick` 將其加入防踢白名單。"
            )
            embed.add_field(name="伺服器", value=guild.name, inline=True)
            embed.set_footer(text="AntiNuke360 v1.3.0")
            try:
                await send_log(guild, embed=embed)
            except Exception:
                pass
            return
        print(f"[JOIN] {member} (黑名單機器人) 試圖加入伺服器 {guild.name}，立即封鎖")
        try:
            blacklist_info = bot_blacklist[user_id_str]
            ban_reason = blacklist_info.get('reason', '在其他伺服器進行 Nuke 攻擊')
            await guild.ban(member, reason=f"AntiNuke360: 黑名單機器人 - {ban_reason}")
            print(f"[BAN] 已封鎖黑名單機器人 {member}")
            
            if user_id_str not in notified_bans[guild.id]:
                notified_bans[guild.id].add(member.id)
                embed = discord.Embed(title="[AntiNuke360 警報]", color=discord.Color.red())
                embed.description = (
                    f"黑名單機器人 `{member}` (ID: `{member.id}`) 試圖加入伺服器被自動封鎖。\n\n"
                    f"被列入黑名單的原因: {ban_reason}\n\n"
                    "如果您確定此機器人在本伺服器是被允許的，伺服器擁有者可以使用 `/add-server-anti-kick`，\n"
                    "將其加入本伺服器的防踢白名單，以避免未來再度被自動封鎖。"
                )
                embed.add_field(name="伺服器", value=guild.name, inline=True)
                embed.set_footer(text="AntiNuke360 v1.3.0")
                try:
                    await send_log(guild, embed=embed)
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
    elif is_permanent_whitelisted(guild.id, member.id):
        print(f"[JOIN] {member} (本伺服器永久白名單) 加入伺服器 {guild.name}，允許")

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
            
            if actor_id_str in bot_whitelist or is_permanent_whitelisted(guild.id, actor.id):
                break
            
            if await track_action(guild, actor, "webhook_create"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "行為異常：短時間內大量建立 Webhook")
            break
    except Exception:
        pass

async def handle_anti_hijack(message: discord.Message):
    guild = message.guild
    user = message.author
    gid = guild.id
    uid = user.id
    content = message.content

    if not anti_hijack_settings[gid]["enabled"]:
        return

    if is_permanent_whitelisted(gid, uid):
        mode = "whitelisted"
    else:
        mode = "normal"

    if not content:
        return

    dq = hijack_tracker[gid][uid][content]
    now = time.time()
    dq.append((now, message.channel.id))
    filtered = [(ts, cid) for (ts, cid) in dq if now - ts <= 5]
    hijack_tracker[gid][uid][content] = deque(filtered)
    channels = {cid for _, cid in filtered}

    if len(filtered) >= 3 and len(channels) >= 3:
        try:
            await message.delete()
        except Exception:
            pass

        mutual_guilds = [g for g in bot.guilds if g.get_member(uid)]

        invite_links = []
        for g in mutual_guilds:
            target_channel = g.system_channel
            if not target_channel:
                for ch in g.text_channels:
                    if ch.permissions_for(g.me).create_instant_invite:
                        target_channel = ch
                        break
            if not target_channel:
                continue
            try:
                invite = await target_channel.create_invite(max_age=7 * 24 * 3600, max_uses=1, reason="AntiNuke360: 被盜帳回復用邀請")
                invite_links.append((g.name, str(invite)))
            except Exception as e:
                print(f"[ANTI HIJACK] 無法在伺服器 {g.name} 建立邀請: {e}")
                continue

        dm_text_lines = [
            "您好，這裡是 AntiNuke360。",
            "",
            "我們偵測到您的帳號在短時間內於多個頻道發送相同訊息，疑似 **被盜帳號或被利用發送詐騙訊息**。",
            "為了保護伺服器安全，您的帳號已被從相關伺服器中踢出或暫時限制。",
        ]
        if invite_links:
            dm_text_lines.append("")
            dm_text_lines.append("以下是您曾加入、並安裝 AntiNuke360 的伺服器 7 天一次性邀請連結：")
            for name, link in invite_links:
                dm_text_lines.append(f"- {name}: {link}")
            dm_text_lines.append("")
            dm_text_lines.append("請在完成安全檢查、更改密碼與二階段驗證後，再透過上述連結重新加入伺服器。")
        else:
            dm_text_lines.append("")
            dm_text_lines.append("目前無法自動為您建立回到各伺服器的邀請連結，請自行聯繫伺服器管理員協助。")

        try:
            dm = await user.create_dm()
            dm_text_lines.append("")
            dm_text_lines.append("若您是在私訊中看到此訊息，代表部份伺服器尚未設定 AntiNuke360 的日誌頻道。")
            await dm.send("\n".join(dm_text_lines))
        except Exception as e:
            print(f"[ANTI HIJACK] 無法 DM 使用者 {user}: {e}")

        embed = discord.Embed(title="[AntiNuke360 - 反被盜帳偵測]", color=discord.Color.red())
        embed.description = (
            f"使用者 `{user}` (ID: `{uid}`) 在 5 秒內於多個頻道發送相同訊息，疑似被盜帳號或發送詐騙訊息。\n\n"
            f"本頻道: {message.channel.mention}\n"
            f"訊息內容: ```{content[:1500]}```"
        )
        embed.set_footer(text="AntiNuke360 v1.3.0")
        try:
            await send_log(guild, embed=embed)
        except Exception:
            pass

        if mode == "whitelisted":
            print(f"[ANTI HIJACK] {user} 為永久白名單，僅刪除訊息與通知。")
            return

        for g in mutual_guilds:
            member = g.get_member(uid)
            if not member:
                continue
            try:
                await g.kick(member, reason="AntiNuke360: 疑似被盜帳號 / 詐騙訊息")
                print(f"[ANTI HIJACK] 已從伺服器 {g.name} 踢出 {member}")
            except Exception as e:
                print(f"[ANTI HIJACK] 無法從伺服器 {g.name} 踢出 {member}: {e}")

@bot.event
async def on_message(message):
    if not message.guild:
        return

    guild = message.guild
    user = message.author
    gid = guild.id
    uid = user.id
    user_id_str = str(uid)

    if user_id_str in bot_blacklist and not is_anti_kick_whitelisted(gid, uid):
        try:
            await message.delete()
            print(f"[BLACKLIST MSG] 已刪除黑名單成員 {user} 的訊息")
        except Exception as e:
            print(f"[BLACKLIST MSG] 刪除黑名單訊息失敗: {e}")
        return

    if user.bot:
        return

    await handle_anti_hijack(message)

    if user_id_str in bot_blacklist or user_id_str in bot_whitelist:
        return

    if is_permanent_whitelisted(guild.id, user.id):
        return

    if await track_action(guild, user, "message_send"):
        asyncio.create_task(prompt_restore_on_suspect(guild))
        await take_action(guild, user, "行為異常短時間內大量發送訊息")
    
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
            
            if actor_id_str in bot_whitelist or is_permanent_whitelisted(guild.id, actor.id):
                break
            
            if await track_action(guild, actor, "channel_create"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "行為異常：短時間內大量建立頻道")
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
            
            if actor_id_str in bot_whitelist or is_permanent_whitelisted(guild.id, actor.id):
                continue
            
            if await track_action(guild, actor, "channel_delete"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "行為異常：短時間內大量刪除頻道")
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
                
                if actor_id_str in bot_whitelist or is_permanent_whitelisted(guild.id, actor.id):
                    break
                
                if await track_action(guild, actor, "member_kick"):
                    asyncio.create_task(prompt_restore_on_suspect(guild))
                    await take_action(guild, actor, "行為異常：短時間內大量踢出成員")
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
                
                if actor_id_str in bot_whitelist or is_permanent_whitelisted(guild.id, actor.id):
                    break
                
                if await track_action(guild, actor, "member_ban"):
                    asyncio.create_task(prompt_restore_on_suspect(guild))
                    await take_action(guild, actor, "行為異常：短時間內大量停權成員")
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
            
            if actor_id_str in bot_whitelist or is_permanent_whitelisted(guild.id, actor.id):
                break
            
            if await track_action(guild, actor, "role_create"):
                asyncio.create_task(prompt_restore_on_suspect(guild))
                await take_action(guild, actor, "行為異常：短時間內大量建立身分組")
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
    gid = interaction.guild.id
    anti_count = len(server_whitelists[gid]["anti_kick"]) if gid in server_whitelists else 0
    temp_count = len([k for k, v in server_whitelists[gid]["temporary"].items() if v > time.time()]) if gid in server_whitelists else 0
    perm_count = len(server_whitelists[gid]["permanent"]) if gid in server_whitelists else 0
    embed.add_field(name="伺服器防踢白名單人數", value=str(anti_count), inline=False)
    embed.add_field(name="伺服器臨時白名單人數", value=str(temp_count), inline=False)
    embed.add_field(name="伺服器永久白名單人數", value=str(perm_count), inline=False)
    has_snapshot = snapshot_is_valid(load_snapshot_file(interaction.guild.id))
    embed.add_field(name="伺服器快照", value=f"{'有有效快照' if has_snapshot else '無有效快照'}", inline=False)
    hij_settings = anti_hijack_settings[gid]
    embed.add_field(name="反被盜帳", value="啟用" if hij_settings["enabled"] else "停用", inline=False)
    embed.add_field(name="自訂狀態文字", value=f"已啟用 ({len(STATUS_MESSAGES)} 個，每 10 秒輪流)", inline=False)
    embed.set_footer(text=f"AntiNuke360 {VERSION} | 防護參數已固定 & Snapshot in MySQL")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="scan-blacklist", description="掃描並停權伺服器中的黑名單成員 (管理員)")
async def scan_blacklist_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        scan_count, banned_count = await scan_and_ban_blacklist(interaction.guild)
        embed = discord.Embed(title="黑名單掃描完成", color=discord.Color.green())
        embed.description = (
            "已掃描伺服器中的成員並停權黑名單帳號。\n\n"
            "若有特定黑名單帳號在本伺服器是被允許的，伺服器擁有者可以使用 `/add-server-anti-kick` 將其加入防踢白名單，"
            "以避免未來再次被自動停權。"
        )
        embed.add_field(name="掃描人數", value=str(scan_count), inline=True)
        embed.add_field(name="停權人數", value=str(banned_count), inline=True)
        embed.add_field(name="伺服器", value=interaction.guild.name, inline=False)
        embed.set_footer(text="AntiNuke360 v1.3.0")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        embed = discord.Embed(title="掃描失敗", color=discord.Color.red())
        embed.description = f"掃描伺服器時出錯: {str(e)}"
        embed.set_footer(text="AntiNuke360 v1.3.0")
        await interaction.followup.send(embed=embed)

# 臨時白名單 - 管理員可增刪
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="add-server-temp", description="將成員或機器人加入本伺服器臨時白名單 (管理員)")
@app_commands.describe(entity_id="成員或機器人 ID")
async def add_server_temp(interaction: discord.Interaction, entity_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        eid = int(entity_id)
    except Exception:
        await interaction.followup.send("無效的 ID", ephemeral=True)
        return
    add_temporary_whitelist(interaction.guild.id, eid)
    await interaction.followup.send(f"已將 `{entity_id}` 加入本伺服器臨時白名單 (1 小時)", ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="remove-server-temp", description="從本伺服器臨時白名單移除成員或機器人 (管理員)")
@app_commands.describe(entity_id="成員或機器人 ID")
async def remove_server_temp(interaction: discord.Interaction, entity_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        eid = int(entity_id)
    except Exception:
        await interaction.followup.send("無效的 ID", ephemeral=True)
        return
    remove_temporary_whitelist(interaction.guild.id, eid)
    await interaction.followup.send(f"已從本伺服器臨時白名單移除 `{entity_id}`", ephemeral=True)

# 防踢白名單 - 只有伺服器擁有者可以設定
@bot.tree.command(name="add-server-anti-kick", description="將成員或機器人加入本伺服器防踢白名單 (僅擁有者)")
@app_commands.describe(entity_id="成員或機器人 ID")
async def add_server_anti_kick(interaction: discord.Interaction, entity_id: str):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("只有伺服器擁有者可使用此指令", ephemeral=True)
        return
    try:
        eid = int(entity_id)
    except Exception:
        await interaction.response.send_message("無效的 ID", ephemeral=True)
        return
    add_anti_kick_whitelist(interaction.guild.id, eid)
    await interaction.response.send_message(f"已將 `{entity_id}` 加入本伺服器防踢白名單", ephemeral=True)

@bot.tree.command(name="remove-server-anti-kick", description="從本伺服器防踢白名單移除成員或機器人 (僅擁有者)")
@app_commands.describe(entity_id="成員或機器人 ID")
async def remove_server_anti_kick(interaction: discord.Interaction, entity_id: str):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("只有伺服器擁有者可使用此指令", ephemeral=True)
        return
    try:
        eid = int(entity_id)
    except Exception:
        await interaction.response.send_message("無效的 ID", ephemeral=True)
        return
    remove_anti_kick_whitelist(interaction.guild.id, eid)
    await interaction.response.send_message(f"已從本伺服器防踢白名單移除 `{entity_id}`", ephemeral=True)

# 永久白名單 - 只有伺服器擁有者可以設定
@bot.tree.command(name="add-server-perm", description="將成員或機器人加入本伺服器永久白名單 (僅擁有者)")
@app_commands.describe(entity_id="成員或機器人 ID")
async def add_server_perm(interaction: discord.Interaction, entity_id: str):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("只有伺服器擁有者可使用此指令", ephemeral=True)
        return
    try:
        eid = int(entity_id)
    except Exception:
        await interaction.response.send_message("無效的 ID", ephemeral=True)
        return
    add_permanent_whitelist(interaction.guild.id, eid)
    await interaction.response.send_message(f"已將 `{entity_id}` 加入本伺服器永久白名單", ephemeral=True)

@bot.tree.command(name="remove-server-perm", description="從本伺服器永久白名單移除成員或機器人 (僅擁有者)")
@app_commands.describe(entity_id="成員或機器人 ID")
async def remove_server_perm(interaction: discord.Interaction, entity_id: str):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("只有伺服器擁有者可使用此指令", ephemeral=True)
        return
    try:
        eid = int(entity_id)
    except Exception:
        await interaction.response.send_message("無效的 ID", ephemeral=True)
        return
    remove_permanent_whitelist(interaction.guild.id, eid)
    await interaction.response.send_message(f"已從本伺服器永久白名單移除 `{entity_id}`", ephemeral=True)

@bot.tree.command(name="server-whitelist", description="查看本伺服器白名單 (管理員)")
async def server_whitelist_cmd(interaction: discord.Interaction):
    gid = interaction.guild.id
    anti = server_whitelists[gid]["anti_kick"]
    temp = server_whitelists[gid]["temporary"]
    perm = server_whitelists[gid]["permanent"]
    purge_expired_temporary(gid)
    if not anti and not temp and not perm:
        await interaction.response.send_message("本伺服器白名單為空", ephemeral=True)
        return
    lines = []
    if anti:
        lines.append("防踢白名單:")
        for i, bid in enumerate(sorted(anti)):
            lines.append(f"  {i+1}. `{bid}`")
    if temp:
        lines.append("臨時白名單 (剩餘秒數):")
        now = time.time()
        for i, (bid, expiry) in enumerate(sorted(temp.items(), key=lambda x: x[1])):
            rem = int(expiry - now)
            lines.append(f"  {i+1}. `{bid}` - {rem} 秒")
    if perm:
        lines.append("永久白名單:")
        for i, bid in enumerate(sorted(perm)):
            lines.append(f"  {i+1}. `{bid}`")
    embed = discord.Embed(title="本伺服器白名單狀態", color=discord.Color.blue())
    embed.description = "\n".join(lines[:30])
    embed.set_footer(text="AntiNuke360 v1.3.0")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set-log-channel", description="設定本伺服器的記錄頻道 (管理員)")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="記錄頻道（提及頻道或 ID）")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if channel is None:
        set_log_channel_for_guild(interaction.guild.id, None)
        await interaction.response.send_message("已清除記錄頻道設定，未來會私訊伺服器擁有者與管理員。", ephemeral=True)
        return
    set_log_channel_for_guild(interaction.guild.id, channel.id)
    await interaction.response.send_message(f"已將 {channel.mention} 設為記錄頻道。", ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="toggle-anti-hijack", description="開啟或關閉反被盜帳功能 (管理員)")
@app_commands.describe(mode="輸入 on 或 off")
async def toggle_anti_hijack(interaction: discord.Interaction, mode: str):
    gid = interaction.guild.id
    mode_lower = mode.lower()
    if mode_lower not in ("on", "off", "true", "false", "enable", "disable"):
        await interaction.response.send_message("請輸入 `on` 或 `off`。", ephemeral=True)
        return
    enabled = mode_lower in ("on", "true", "enable")
    anti_hijack_settings[gid]["enabled"] = enabled
    await interaction.response.send_message(f"反被盜帳功能已{'啟用' if enabled else '關閉'}。", ephemeral=True)

@bot.tree.command(name="add-black", description="將機器人加入全域黑名單 (開發者)")
@app_commands.describe(bot_id="機器人 ID", reason="原因")
async def add_black(interaction: discord.Interaction, bot_id: str, reason: str = ""):
    global bot_blacklist
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    if bot_id in bot_blacklist:
        await interaction.response.send_message("該機器人已在黑名單中", ephemeral=True)
        return
    bot_blacklist[bot_id] = {"name": bot_id, "reason": reason, "timestamp": time.time(), "guilds_detected": []}
    save_blacklist(bot_blacklist)
    await interaction.response.defer()
    embed = discord.Embed(title="已加入黑名單", color=discord.Color.red())
    embed.description = (
        f"機器人 ID: `{bot_id}` 已加入全域黑名單。\n\n"
        "如需在特定伺服器允許此機器人，伺服器擁有者可以使用 `/add-server-anti-kick` 將其加入防踢白名單，"
        "以避免未來被自動停權。"
    )
    embed.add_field(name="原因", value=reason if reason else "無", inline=False)
    embed.set_footer(text="AntiNuke360 v1.3.0")
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
        await interaction.response.send_message("該機器人不在黑名單中", ephemeral=True)
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
        await interaction.response.send_message("該機器人已在白名單中", ephemeral=True)
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
        await interaction.response.send_message("該機器人不在白名單中", ephemeral=True)
        return
    del bot_whitelist[bot_id]
    save_whitelist(bot_whitelist)
    embed = discord.Embed(title="已從白名單移除", color=discord.Color.red())
    embed.description = f"機器人 ID: `{bot_id}` 已從全域白名單移除"
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="blacklist", description="查看全域黑名單 (開發者)")
async def blacklist_cmd(interaction: discord.Interaction):
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
    embed.set_footer(text="AntiNuke360 v1.3.0")
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
    embed.set_footer(text="AntiNuke360 v1.3.0")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="scan-all-guilds", description="在所有伺服器掃描並停權黑名單成員 (開發者)")
async def scan_all_guilds_cmd(interaction: discord.Interaction):
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        await scan_blacklist_all_guilds()
        embed = discord.Embed(title="全域黑名單掃描完成", color=discord.Color.green())
        embed.description = (
            "已在所有伺服器中掃描並停權黑名單成員。\n\n"
            "若您希望在特定伺服器中允許某些黑名單帳號，"
            "可於該伺服器使用 `/add-server-anti-kick` 將其加入防踢白名單，以避免未來的自動停權。"
        )
        embed.set_footer(text="AntiNuke360 v1.3.0")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        embed = discord.Embed(title="全域掃描失敗", color=discord.Color.red())
        embed.description = f"掃描時出錯: {str(e)}"
        embed.set_footer(text="AntiNuke360 v1.3.0")
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
    await interaction.followup.send(
        f"開始還原快照 (剩餘有效時間: {remaining//3600} 小時 {(remaining%3600)//60} 分鐘)。"
        "這可能需要一段時間且會先嘗試刪除可刪除的現有頻道與身分組。",
        ephemeral=True
    )
    ok, msg = await perform_restore(guild, ctx_sender=interaction.user)
    if ok:
        await interaction.followup.send(f"還原完成: {msg}", ephemeral=True)
    else:
        await interaction.followup.send(f"還原失敗: {msg}", ephemeral=True)

# === 新增：查詢某 ID 是否在黑名單 / 白名單的指令 ===

@bot.tree.command(name="check-black", description="查詢某個 ID 是否在全域黑名單或白名單 (管理員)")
@app_commands.describe(entity_id="使用者或機器人 ID（純數字）")
async def check_black(interaction: discord.Interaction, entity_id: str):
    await interaction.response.defer(ephemeral=True)
    target_id = entity_id.strip()
    info_black = bot_blacklist.get(target_id)
    info_white = bot_whitelist.get(target_id)

    if not info_black and not info_white:
        embed = discord.Embed(title="查詢結果", color=discord.Color.green())
        embed.description = f"ID `{target_id}` 不在全域黑名單，也不在全域白名單。"
        embed.set_footer(text="AntiNuke360 v1.3.0")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(title="查詢結果", color=discord.Color.orange())
    lines = []
    if info_black:
        lines.append("**黑名單**：")
        lines.append(f"- 名稱：`{info_black.get('name', target_id)}`")
        lines.append(f"- 原因：{info_black.get('reason', '無')}")
        ts = info_black.get("timestamp")
        if ts:
            lines.append(f"- 加入時間 (timestamp)：`{ts}`")
        guilds = info_black.get("guilds_detected", [])
        if guilds:
            lines.append(f"- 偵測伺服器 ID 列表：`{', '.join(str(x) for x in guilds)}`")
        lines.append("")

    if info_white:
        lines.append("**白名單**：")
        lines.append(f"- 名稱：`{info_white.get('name', target_id)}`")
        lines.append(f"- 原因：{info_white.get('reason', '無')}")
        tsw = info_white.get("timestamp")
        if tsw:
            lines.append(f"- 加入時間 (timestamp)：`{tsw}`")

    embed.description = "\n".join(lines)
    embed.set_footer(text="AntiNuke360 v1.3.0")
    await interaction.followup.send(embed=embed, ephemeral=True)

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

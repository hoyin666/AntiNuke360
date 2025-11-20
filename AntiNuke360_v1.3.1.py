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
VERSION = "v1.3.1"  # 版本號

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

# 全服公告常數與排程
ANNOUNCEMENT_WAIT_TIMEOUT = 12 * 3600  # 12 小時等待最新管理員上線
ANNOUNCEMENT_CHECK_INTERVAL = 60  # 每次檢查間隔（秒）
pending_announcement_tasks = set()  # 儲存等待 DM 的 asyncio task

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

def build_announcement_embed(message: str, sender_display: str) -> discord.Embed:
    embed = discord.Embed(
        title="[AntiNuke360 全服公告]",
        description=message,
        color=discord.Color.gold()
    )
    embed.add_field(name="發布者", value=sender_display, inline=False)
    embed.set_footer(text=f"AntiNuke360 {VERSION}")
    return embed

def get_admin_candidates(guild: discord.Guild):
    candidates = []
    seen = set()
    owner = guild.owner
    if owner and not owner.bot:
        candidates.append(owner)
        seen.add(owner.id)
    for member in guild.members:
        if member.bot or member.id in seen:
            continue
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild:
            candidates.append(member)
            seen.add(member.id)
    return candidates

def member_is_online(member: discord.Member) -> bool:
    status = getattr(member, "status", discord.Status.offline)
    return status not in (discord.Status.offline, discord.Status.invisible, None)

async def try_send_announcement_to_log(guild: discord.Guild, message: str, sender_display: str) -> bool:
    log_ch_id = get_log_channel_for_guild(guild.id)
    if not log_ch_id:
        return False
    channel = guild.get_channel(log_ch_id)
    if not isinstance(channel, discord.TextChannel):
        return False
    if not channel.permissions_for(guild.me).send_messages:
        return False
    try:
        await channel.send(embed=build_announcement_embed(message, sender_display))
        print(f"[ANNOUNCE] 已在伺服器 {guild.name} 的日誌頻道發布公告")
        return True
    except Exception as e:
        print(f"[ANNOUNCE ERROR] 無法在伺服器 {guild.name} 的日誌頻道發送公告: {e}")
        return False

async def dm_guild_member(member: discord.Member, message: str, sender_display: str) -> bool:
    try:
        dm = await member.create_dm()
        await dm.send(embed=build_announcement_embed(message, sender_display))
        print(f"[ANNOUNCE] 已私訊管理員 {member} 發送公告")
        return True
    except Exception as e:
        print(f"[ANNOUNCE ERROR] 無法私訊管理員 {member}: {e}")
        return False

def schedule_admin_wait_task(guild_id: int, message: str, sender_display: str):
    task = asyncio.create_task(wait_for_admin_and_dm(guild_id, message, sender_display))
    pending_announcement_tasks.add(task)
    task.add_done_callback(lambda t: pending_announcement_tasks.discard(t))

async def wait_for_admin_and_dm(guild_id: int, message: str, sender_display: str):
    deadline = time.time() + ANNOUNCEMENT_WAIT_TIMEOUT
    while time.time() < deadline:
        guild = bot.get_guild(guild_id)
        if not guild:
            return
        admins = get_admin_candidates(guild)
        if not admins:
            return
        for admin in admins:
            if member_is_online(admin):
                if await dm_guild_member(admin, message, sender_display):
                    return
        await asyncio.sleep(ANNOUNCEMENT_CHECK_INTERVAL)
    print(f"[ANNOUNCE] 伺服器 {guild_id} 在 12 小時內沒有管理員上線，已取消公告")

async def dispatch_global_announcement(guild: discord.Guild, message: str, sender_display: str) -> str:
    if await try_send_announcement_to_log(guild, message, sender_display):
        return "log"
    admins = get_admin_candidates(guild)
    if not admins:
        print(f"[ANNOUNCE] 伺服器 {guild.name} 無可聯絡管理員，跳過")
        return "no_admin"
    online_admins = [m for m in admins if member_is_online(m)]
    for admin in online_admins:
        if await dm_guild_member(admin, message, sender_display):
            return "dm"
    schedule_admin_wait_task(guild.id, message, sender_display)
    print(f"[ANNOUNCE] 伺服器 {guild.name} 無上線管理員，已排程等待")
    return "scheduled"

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

@bot.tree.command(name="announce-all", description="向所有伺服器發送全服公告 (開發者)")
@app_commands.describe(message="公告內容")
async def announce_all(interaction: discord.Interaction, message: str):
    if interaction.user.id != DEVELOPER_ID:
        await interaction.response.send_message("只有開發者可以使用此指令", ephemeral=True)
        return
    content = message.strip()
    if not content:
        await interaction.response.send_message("公告內容不得為空。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    sender_display = f"{interaction.user} (ID: {interaction.user.id})"
    stats = {"log": 0, "dm": 0, "scheduled": 0, "no_admin": 0, "error": 0}
    for guild in bot.guilds:
        try:
            result = await dispatch_global_announcement(guild, content, sender_display)
            if result in stats:
                stats[result] += 1
            else:
                stats["error"] += 1
        except Exception as e:
            print(f"[ANNOUNCE ERROR] 無法處理伺服器 {guild.name}: {e}")
            stats["error"] += 1
    summary = (
        "全服公告已處理。\n"
        f"- 日誌頻道送達：{stats['log']}\n"
        f"- 線上管理員私訊：{stats['dm']}\n"
        f"- 等待管理員上線：{stats['scheduled']}\n"
        f"- 無可聯絡管理員：{stats['no_admin']}\n"
        f"- 發送失敗：{stats['error']}"
    )
    await interaction.followup.send(summary, ephemeral=True)

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

# AntiNuke360

完全開源、社群驅動、100%透明。

## 功能概述

AntiNuke360 是一個強大的 Discord 伺服器防護機器人，專門針對 Nuke 攻擊進行防禦。它提供多層防護機制，包括實時攻擊偵測、自動封鎖惡意用戶、全域黑名單系統、伺服器快照自動還原、反被盜帳偵測功能，以及基於 MySQL 的資料持久化。

---

## 核心功能

### Gemini AI 深度安檢（v2.0 新增）

- 透過 `Gemini 2.5 Pro (gemini-2.5-pro)` 為伺服器提供深度掃描，逐條檢視審核日誌、逐一檢查 bot 權限並輸出建議。每個伺服器與每個發動帳號每 7 天僅能啟動一次掃描，避免濫用。
- 每分鐘最多 10 次 Gemini 2.5 Pro 請求，如達上限會自動延遲 60 秒後再試，確保批量 key 穩定運作。
- 新增的 Bot 會自動使用 `Gemini Flash-Lite Latest` 進行多因素審查，結果快取至 `AI_Analyse_Bot/` 目錄，效期 3 天，重複查詢直接讀取快取避免重複扣額。
- 若判定為可疑 bot，系統會立即在日誌頻道提醒（若已設定），同時自動移除該 bot 的角色與權限。
- 所有 Gemini 報告都儲存在 `AI_Analyse_Bot/` 內，管理員可使用 `/gemini-bot-report` 指令重看或強制刷新。

### 自動反 Nuke 防護

機器人持續監控以下行為，並在短時間內檢測到異常操作時自動採取行動：

- 大量刪除頻道 (偵測時間窗: 10 秒)
- 大量建立頻道
- 大量發送訊息
- 大量建立 Webhook
- 大量踢出成員
- 大量封鎖成員
- 大量建立角色

防護參數已優化並固定為：

- 最大動作次數: 7 次
- 時間窗口: 10 秒
- 狀態: 啟用 (不可調整)

---

### 伺服器快照系統（Snapshot 存於 MySQL）

機器人自動為每個伺服器建立結構快照，可在遭受攻擊後快速還原：

- 自動建立伺服器架構快照 (**72 小時有效期**)
- 保存所有角色、分類、頻道、權限設定
- 攻擊發生時自動詢問是否還原
- 完全恢復伺服器結構，防止永久破壞

快照資料儲存在 MySQL 的 `snapshots` 資料表中，而不是本機檔案：

```sql
CREATE TABLE IF NOT EXISTS snapshots (
    guild_id BIGINT PRIMARY KEY,
    snapshot_json LONGTEXT NOT NULL,
    updated_at DOUBLE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

### 一鍵還原功能

在伺服器遭受核打擊攻擊時：

- 自動偵測攻擊行為
- 立即詢問伺服器擁有者是否還原
- 或使用 `/restore-snapshot` 手動還原
- 完全重建角色、分類、頻道、權限
- 快照在 72 小時內持續有效

---

### 全域黑名單系統

機器人維護一個全域黑名單，自動識別並追蹤已知的惡意機器人：

- 自動識別在其他伺服器進行攻擊的惡意機器人
- 機器人試圖加入伺服器時立即封鎖
- 支援手動掃描功能，識別並停權已在伺服器中的黑名單成員
- 黑名單信息包括機器人 ID、名稱、原因和被檢測的伺服器列表
- 使用 MySQL `bot_blacklist` 資料表持久化儲存

---

### 全域白名單系統

開發者可以維護全域白名單，允許特定的機器人在所有伺服器中豁免防護：

- 開發者可新增/移除全域白名單成員
- 白名單機器人在所有伺服器中自動豁免
- 包含白名單原因和時間戳
- 使用 MySQL `bot_whitelist` 資料表持久化儲存

---

### 本地伺服器白名單系統（三層）

伺服器可以管理不同層級的白名單，區分管理員權限和擁有者權限：

- **防踢白名單** (伺服器擁有者管理)  
  允許全域黑名單帳號加入但記錄監控，可避免特定黑名單帳號在本伺服器被自動停權。

- **臨時白名單** (管理員管理)  
  1 小時自動過期，針對敏感操作寬鬆至 **15 次/15 秒**。

- **永久白名單** (伺服器擁有者管理)  
  對所有敏感操作完全豁免。

所有白名單資料儲存在 MySQL `server_whitelist` 資料表中，所有操作都記錄到指定的記錄頻道或發送給伺服器管理員。

---

### 稽核日誌監控

機器人監控伺服器的稽審日誌，追蹤所有模組化操作：

- 監控頻道建立/刪除事件
- 監控成員踢出/封鎖事件
- 監控角色建立事件
- 監控 Webhook 建立事件
- 同時檢查執行者是否在黑名單或白名單中

---

### 反被盜帳偵測 (v1.2.2 新增)

自動偵測並防禦被盜帳號發動的詐騙攻擊：

- 監控 **5 秒內在不同頻道發送相同訊息** 的異常行為
- 自動識別被盜帳號特徵（跨頻道重複訊息）
- 自動刪除疑似詐騙訊息
- 自動踢出被盜帳號，發送通知和恢復邀請
- 自動 DM 被害人 7 天一次性邀請連結用於帳號恢復
- 永久白名單成員僅刪除訊息，不會被踢出

---

### 黑名單訊息屏蔽 (v1.2.2 新增)

全域黑名單成員無法發送訊息：

- 黑名單成員的所有訊息自動刪除
- 防踢白名單成員的訊息同樣被屏蔽（除非在永久白名單）
- 訊息刪除自動記錄

---

### Administrator 權限檢查 (v1.2.1 新增)

- 加入伺服器後自動檢查是否具有 Administrator 權限
- 每小時定期檢查一次 Administrator 權限
- 若無權限，通知擁有者和管理員後自動離開
- 確保機器人持續具備完整的防護能力

---

### 延遲初始化 (v1.2.3 新增)

改進權限檢查時機，提升管理員使用體驗：

- 機器人加入伺服器時記錄時間戳
- **延遲 10 分鐘後才檢查權限**（而非立即檢查）
- 給予管理員充足時間配置身分組和權限

---

### 權限錯誤監控

機器人自動偵測和回應權限不足的情況：

- 追蹤 **1 分鐘內的權限錯誤次數**
- 當權限錯誤達到 10 次時，向伺服器所有者發送通知
- 自動離開缺乏必要權限的伺服器
- 通知訊息包含需要的權限列表和修復建議

---

### 成員加入掃描

當新成員加入伺服器時：

- 檢查是否在全域黑名單中
- 檢查是否在伺服器白名單中
- 檢查是否在全域白名單中
- 黑名單成員立即被封鎖

---

### 自訂狀態文字

機器人顯示多個輪流變化的自訂狀態文字，每 10 秒更換一次：

- 包含 24 種不同的狀態文字
- 自動循環切換
- 增強機器人的人格特色

---

### 歡迎訊息系統 + 歡迎頻道重試 (v1.2.4 增強)

機器人加入新伺服器時自動建立歡迎頻道並發送詳細的歡迎訊息：

- 自動建立 `antinuke360-welcome` 頻道
- 頻道設定為僅機器人可發送訊息
- 包含功能介紹、使用指南、防護參數和聯絡信息
- **若創建失敗，會每分鐘自動重試，直到成功建立或 Bot 自動退出該伺服器** (v1.2.4)

---

### 日誌頻道 & 私訊提示 (v1.2.4 新增)

- 伺服器可使用 `/set-log-channel` 指定日誌頻道
- 若未設定日誌頻道，重要事件會改為 **私訊伺服器擁有者與管理員**
- 所有這類私訊會附帶提示：  
  **「若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。」**

---

### 黑名單停權提醒與防踢白名單引導 (v1.2.4 新增)

- 當掃描並停權黑名單成員時，通知訊息中會加入提示：
  - 若該帳號在本伺服器是可信任的，可由伺服器擁有者使用 `/add-server-anti-kick`  
    將其加入本伺服器的 **防踢白名單**，避免未來再次被自動停權。

---

## 管理員指令

### `/status`

查看 AntiNuke360 的當前運行狀態：

- 顯示系統啟用狀態
- 顯示防護參數
- 顯示黑名單和白名單統計
- 顯示快照狀態
- 顯示進階保護狀態（反被盜帳等）

### `/scan-blacklist`

掃描伺服器中的所有成員並停權黑名單成員：

- 需要管理員權限
- 回傳掃描人數和停權人數統計
- 立即識別並移除已在伺服器中的惡意成員
- 通知中會提醒伺服器擁有者可用 `/add-server-anti-kick` 避免誤封可信任帳號 (v1.2.4)

### `/restore-snapshot`

手動還原伺服器最新的快照：

- 需要管理員權限
- 還原所有角色、分類、頻道、權限設定
- 快照必須在 **72 小時內有效**

### `/add-server-temp [ID]`

將機器人或成員加入臨時白名單 (1 小時自動過期)：

- 需要管理員權限
- 指定目標 ID
- 臨時白名單內的對象，敏感操作寬鬆至 **15 次/15 秒**

### `/remove-server-temp [ID]`

從臨時白名單移除機器人或成員：

- 需要管理員權限

### `/server-whitelist`

查看本伺服器的白名單狀態：

- 需要管理員權限
- 顯示防踢、臨時、永久白名單的所有成員
- 顯示臨時白名單的剩餘時間

### `/set-log-channel [#channel]`

設置本伺服器的白名單操作與防護事件記錄頻道：

- 需要管理員權限
- 所有白名單操作和防護警報都會發送到此頻道
- 若清除設定，未來事件會改為私訊並附上「未設日誌頻道」提示 (v1.2.4)

### `/gemini-security-scan`

- 需要管理員權限
- 使用 Gemini 2.5 Pro 深度檢查伺服器，逐條審核日誌、bot 權限與安全建議
- 每個伺服器與每個帳號 7 天僅能使用一次，並遵守每分鐘 10 次 Gemini 2.5 Pro 請求限制
- 報告以嵌入訊息回傳，字數自動控制在 Discord 限制內

### `/gemini-bot-report`

- 需要 Manage Server 權限
- 從 `AI_Analyse_Bot/` 快取讀取或強制刷新指定 bot 的 Gemini 安全報告
- 回傳風險等級、可疑徵象以及建議措施，協助管理員決策

### `/toggle-anti-hijack [on/off]`

開啟或關閉反被盜帳功能：

- 需要管理員權限
- 預設啟用

---

## 伺服器擁有者指令

### `/add-server-anti-kick [ID]`

將機器人或成員加入本伺服器防踢白名單 (伺服器擁有者專屬)：

- 需要伺服器擁有者權限
- 允許全域黑名單帳號加入但會被記錄監控
- 主要用於「我知道他在黑名單，但我信任他」的情境

### `/remove-server-anti-kick [ID]`

從防踢白名單移除機器人或成員：

- 需要伺服器擁有者權限

### `/add-server-perm [ID]`

將機器人或成員加入本伺服器永久白名單 (伺服器擁有者專屬)：

- 需要伺服器擁有者權限
- 該帳號對所有敏感操作完全豁免

### `/remove-server-perm [ID]`

從永久白名單移除機器人或成員：

- 需要伺服器擁有者權限

---

## 開發者指令

### `/add-black [ID] [原因]`

將機器人加入全域黑名單：

- 只有開發者可用
- 指定機器人 ID 和加入原因
- 立即在所有伺服器中掃描並停權

### `/remove-black [ID]`

從全域黑名單移除機器人：

- 只有開發者可用
- 指定機器人 ID

### `/add-white [ID] [原因]`

將機器人加入全域白名單：

- 只有開發者可用
- 指定機器人 ID 和白名單原因

### `/remove-white [ID]`

從全域白名單移除指定機器人：

- 只有開發者可用
- 指定機器人 ID

### `/blacklist`

查看全域黑名單：

- 只有開發者可用
- 顯示前 10 項黑名單記錄
- 包含 ID、名稱和原因

### `/whitelist-list`

查看全域白名單：

- 只有開發者可用
- 顯示前 10 項白名單記錄
- 包含 ID、名稱和原因

### `/scan-all-guilds`

在所有伺服器中掃描並停權黑名單成員：

- 只有開發者可用
- 遍歷所有伺服器進行掃描
- 全域黑名單成員都會被識別和移除

### `/check-black [ID]` (v1.3.0 新增)

查詢某個 ID 是否在全域黑名單或白名單：

- 只有開發者可用
- 顯示該 ID 是否在黑名單 / 白名單
- 顯示名稱、原因、timestamp 和偵測伺服器（若有）

---

## 安裝和設置

### 先決條件

- Python 3.8 或更高版本
- `discord.py` 庫
- `python-dotenv` 庫
- 可用的 MySQL/MariaDB 資料庫

### 依賴

```txt
discord.py>=2.0
python-dotenv
mysql-connector-python
google-generativeai
```

### MySQL 資料表

最少需要下列資料表（匯入/migrate 程式會自動建表，這裡只列出結構概念）：

```sql
CREATE TABLE IF NOT EXISTS bot_blacklist (
    bot_id BIGINT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    reason TEXT,
    timestamp DOUBLE,
    guilds_detected TEXT
);

CREATE TABLE IF NOT EXISTS bot_whitelist (
    bot_id BIGINT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    reason TEXT,
    timestamp DOUBLE
);

CREATE TABLE IF NOT EXISTS server_whitelist (
    id BIGINT NOT NULL AUTO_INCREMENT,
    guild_id BIGINT NOT NULL,
    anti_kick_user_id BIGINT DEFAULT NULL,
    temp_user_id BIGINT DEFAULT NULL,
    temp_expiry DOUBLE DEFAULT NULL,
    perm_user_id BIGINT DEFAULT NULL,
    log_channel_id BIGINT DEFAULT NULL,
    PRIMARY KEY (id),
    KEY idx_guild (guild_id)
);

CREATE TABLE IF NOT EXISTS guilds_data (
    guild_id BIGINT PRIMARY KEY,
    joined_at DOUBLE,
    welcome_channel_id BIGINT
);

CREATE TABLE IF NOT EXISTS snapshots (
    guild_id BIGINT PRIMARY KEY,
    snapshot_json LONGTEXT NOT NULL,
    updated_at DOUBLE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

### 安裝步驟（v2.0）

1. 複製倉庫或下載程式碼。
2. 建立 `.env` 檔案並設置：

   ```env
   DISCORD_TOKEN=your_discord_bot_token_here

   MYSQL_HOST=your_mysql_host_or_container_name
   MYSQL_PORT=3306
   MYSQL_USER=your_mysql_user
   MYSQL_PASSWORD=your_mysql_password
   MYSQL_DB=your_mysql_database
   ```

3. 在 `cogs/Gemini_keys.txt` 依序填入批量 Gemini API Key（每行一組，允許使用 # 開頭的註解）。

4. 安裝依賴：

   ```bash
   pip install -r requirements.txt
   ```

5. 先執行一次資料表建立/匯入腳本（若有）或直接啟動 Bot，程式會自動檢查 `snapshots` 資料表。

6. 執行機器人 (v2.0)：

  ```bash
  python AntiNuke360_v2.0.py
  ```

---

### Discord Bot 設置

1. 進入 [Discord Developer Portal](https://discord.com/developers/applications)
2. 建立新應用程式
3. 進入「Bot」標籤並建立機器人
4. 複製 Bot Token 到 `.env` 檔案
5. 在「OAuth2」→「URL Generator」中設置權限：
   - 勾選 **Administrator**
6. 使用生成的邀請 URL 將機器人加入伺服器

---

### Gemini API 設定

- 將所有 Gemini API Key 逐行填入 `cogs/Gemini_keys.txt`，程式會自動輪替使用。
- `gemini-2.5-pro-preview` 每分鐘最多觸發 10 次，若到達上限會自動暫停 60 秒後重試，無須人工介入。
- Bot 分析報告儲存在 `AI_Analyse_Bot/` 中，效期 3 天；刪除此資料夾即可強制全數刷新。
- 若未安裝 `google-generativeai` 或 key 檔為空，Gemini 擴充功能會停用並在主控台顯示警告。

---

## 資料儲存（v1.3.0）

機器人使用 **MySQL** 進行主要資料持久化：

- `bot_blacklist` - 全域黑名單
- `bot_whitelist` - 全域白名單
- `server_whitelist` - 各伺服器的本地白名單 (防踢、臨時、永久、log 頻道)
- `guilds_data` - 伺服器資訊、加入時間、歡迎頻道 ID
- `snapshots` - 伺服器架構快照（72 小時有效期）
- `AI_Analyse_Bot/` - Gemini 伺服器/機器人報告快取（3 天效期，可刪除以強制刷新）

舊版 JSON 檔案仍可用匯入腳本轉移到 MySQL：

- `bot_blacklist.json`
- `bot_whitelist.json`
- `server_whitelist.json`
- `guilds_data.json`

---

## 安全性

- 全域黑名單自動追蹤惡意機器人
- 防護參數已優化並固定，無法被不當調整
- 三層白名單系統防止管理員濫用
- 伺服器擁有者專屬權限確保關鍵決定權
- 自動快照和還原系統防止永久破壞
- 所有操作都記錄到指定頻道或通知管理員
- 自動權限監控和錯誤處理
- 所有敏感訊息使用 Discord Embed 形式發送
- v1.2.1 新增 Administrator 權限檢查
- v1.2.2 新增反被盜帳偵測和黑名單訊息屏蔽
- v1.2.3 新增延遲初始化和智能權限檢查
- v1.3.0 將核心資料與快照移至 MySQL，資料更安全可靠

---

## 開發者資訊

- 開發者 Discord ID：**800536911378251787**

只有開發者可以使用「開發者指令」。

---

## 許可

本專案採用 **MIT License**  
你可以自由地使用、修改、分發本軟體，但需要保留原始許可聲明。

---

## 貢獻

本專案完全開源，歡迎：

- 提交 Pull Request
- 回報 Issue
- 提出新功能建議
- 改進文件與翻譯

---

## 支持

如有任何問題或建議，請：

- 透過 GitHub Issues 回報
- 或直接聯繫開發者 (Discord ID: `800536911378251787`)

---

## 更新日誌

### v2.0 (2025年11月20日)

- 新增 `AntiNuke360_v2.0.py`，並支援自動掃描 `cogs/` 目錄以載入擴充包，無須修改主程式即可擴充功能。
- 推出 `Gemini_AI_Expansion_v1.0`：
  - `/gemini-security-scan` 使用 Gemini 2.5 Pro 深度分析伺服器，逐條審核日誌並給出行動建議。
  - `/gemini-bot-report` 讀取/刷新 `AI_Analyse_Bot/` 內的報告，快速掌握特定 bot 的風險狀態。
  - 新 bot 加入時自動觸發 Gemini Flash-Lite 檢查，若可疑立即通知並移除權限。
  - 報告快取 3 天並遵守「伺服器+帳號 7 天一次」與「每分鐘 10 次」雙重限制，減少 API 消耗。
- 新增 `cogs/Gemini_keys.txt` 檔案管理批量 key，並將 `google-generativeai` 納入預設依賴。

### v1.3.0 (2025年11月19日)

- 新增：**完整 MySQL 支援**
  - 全域黑名單、全域白名單、本地伺服器白名單、guild 資料與快照全部改用 MySQL 儲存。
  - 新增 `snapshots` 資料表，用於儲存伺服器結構快照 JSON。
- 新增：**`/check-black` 指令**
  - 可查詢任意 ID 是否在全域黑名單/白名單，顯示名稱、原因、timestamp 及偵測伺服器列表。

### v1.2.4 (2025年11月19日)

- 新增：**日誌頻道私訊註記**
  - 當伺服器未設定日誌頻道時，所有以私訊送出的重要通知會附註：  
    「若您是在私訊中看到此訊息，代表本伺服器尚未設定 AntiNuke360 的日誌頻道。」
- 新增：**黑名單停權提醒與防踢白名單引導**
  - 當掃描並停權黑名單成員時，在通知中提示可使用 `/add-server-anti-kick` 將可信任目標加入防踢白名單，避免未來再次被自動停權。
- 新增：**歡迎頻道自動重試機制**
  - 加入伺服器時若建立歡迎頻道失敗，會每分鐘自動重試，直到成功或機器人離開伺服器。

### v1.2.3 (2025年11月18日)

- 新增延遲初始化機制：加入伺服器時記錄時間，**10 分鐘後才檢查權限**
- 改進使用體驗：給予管理員充足時間配置身分組和權限

### v1.2.2 (2025年11月17日)

- 新增反被盜帳偵測：檢測 5 秒內在不同頻道發送相同訊息的異常行為
- 自動踢出被盜帳號，同時發送恢復邀請連結
- 新增黑名單訊息屏蔽：全域黑名單成員的訊息自動刪除
- 新增 `/toggle-anti-hijack` 指令控制反被盜帳功能
- 永久白名單成員不會因為可疑訊息被踢出
- 增強安全性：更完善的帳號安全保護

### v1.2.1 (2025年11月17日)

- 新增 Administrator 權限檢查：加入伺服器後自動驗證權限
- 每小時定期檢查一次 Administrator 權限
- 若無 Administrator 權限，自動通知擁有者和管理員後離開
- 優化權限錯誤處理邏輯
- 改進快照建立過程中的錯誤恢復
- 增強伺服器加入流程的穩定性

### v1.2 (2025年11月16日)

- 修復 0day 漏洞：三層白名單系統防止管理員濫用
- 新增防踢白名單 (伺服器擁有者管理)
- 新增臨時白名單 (管理員管理，1 小時自動過期)
- 新增永久白名單 (伺服器擁有者管理)
- 新增記錄頻道系統 (`/set-log-channel`)
- 改進白名單查詢指令 (`/server-whitelist`)
- 新增權限分層確保安全性

### v1.1.1 (2025年11月16日)

- 修復還原功能中的頻道刪除遺漏問題
- 改進角色順位調整邏輯
- 增強權限覆蓋還原精確度
- 改進錯誤處理和邊界情況處理
- 優化快照清理機制

### v1.1 (2025年11月16日)

- 新增伺服器快照系統 (72 小時 TTL)
- 新增一鍵還原功能 (`/restore-snapshot`)
- 新增自動快照詢問機制
- 新增完整的角色、頻道、權限還原
- 改進錯誤處理和日誌記錄
- 使用 `pathlib` 進行資料夾管理

### v1.0 (2025年11月15日)

- 初始版本發布
- 完整的反 Nuke 防護系統
- 全域黑名單系統
- 本地伺服器白名單系統
- 全域白名單系統
- 管理員和開發者指令
- 自訂狀態文字
- 歡迎訊息系統
- 權限錯誤監控

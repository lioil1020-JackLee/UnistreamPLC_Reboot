# Unistream PLC Reboot

這是一個以 Python 撰寫的 UniStream PLC reboot 工具，提供：

- Tkinter 圖形介面
- 系統匣常駐模式
- 命令列檢查 / 驗證 / 重啟功能
- OPC UA 健康檢查與自動重啟監控

專案採 clean-room 方式實作 PLC 的 HTTPS / WebSocket reboot 流程，不依賴 UniLogic 安裝目錄中的 DLL 或設定檔。

## 專案分析

目前專案結構很精簡，主要分成三個核心部分：

- `main.py`
  負責 GUI、系統匣、CLI 參數解析，以及 RUN 模式的背景監控流程。
- `unistream_client.py`
  負責 PLC 的 HTTPS / WebSocket 通訊、登入、驗證、reboot 指令與 OPC UA 檢查。
- `config.json`
  負責所有預設參數與啟動模式，執行程式時若存在就會自動套用。

目前依賴用途如下：

- `asyncua`: OPC UA 連線檢查
- `cryptography`: RSA 金鑰產生、PLC password 加密、token 解密
- `websockets`: WebSocket 驗證與 reboot 指令
- `pystray`: 系統匣功能
- `pillow`: 載入 tray icon
- `pyinstaller`: Windows 發行檔打包

## 設定檔

目前 `config.json` 只保留需要經常調整的 PLC 與啟動參數。
像是視窗標題、Windows App ID、icon 檔名這些固定值，已改回程式內硬編碼。
`config.json` 是必要檔案；程式啟動時會先讀它，再決定要開 GUI 還是直接執行命令。

```json
{
  "plc": {
    "ip": "10.80.1.10",
    "api_port": 8001,
    "opc_ua_port": 48484,
    "password": "Blue0324!"
  },
  "run_monitor": {
    "check_interval_seconds": 10,
    "cooldown_seconds": 300
  },
  "startup": {
    "command": "gui",
    "auto_run_monitor": false,
    "start_in_tray": false
  }
}
```

`startup.command` 可用值：

- `gui`
- `check`
- `validate`
- `reboot`
- `check-opcua`

`startup.command = "gui"` 的意思是：

- 這次啟動要進入圖形介面模式
- 不直接執行 CLI 子命令
- 是否自動開始 RUN 監控，交給 `startup.auto_run_monitor`
- 是否一開啟就縮到系統匣，交給 `startup.start_in_tray`

執行程式時，如果程式所在目錄有 `config.json`，而且你沒有額外帶 CLI 參數，程式就會直接依照 `config.json` 啟動。

例如：

- `command = "gui"` 並且 `start_in_tray = true`：啟動 GUI 並直接縮到系統匣
- `command = "gui"` 並且 `auto_run_monitor = true`：啟動 GUI 並自動開始 RUN 監控
- `command = "check"`：直接執行 PLC 檢查後結束
- `command = "reboot"`：直接依照設定檔內容送出 reboot

## 依賴管理

本專案現在改為由 `uv` 完整管理：

- runtime 依賴定義在 `pyproject.toml`
- build 依賴定義在 `dependency-groups.build`
- 鎖定版本記錄在 `uv.lock`
- 本地開發、執行與 CI 都使用 `uv sync` / `uv run`

不再使用 `requirements.txt` 或 `pip install -r ...` 流程。

## 環境需求

- Windows
- Python 3.12
- `uv`

安裝 `uv`：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## 安裝

```powershell
uv sync
```

如果要包含打包工具：

```powershell
uv sync --group build
```

## 執行

啟動 GUI：

```powershell
uv run python .\main.py
```

如果 `config.json` 的 `startup.start_in_tray` 是 `true`，上面這個指令就會直接縮到系統匣。

啟動後直接縮到系統匣：

```powershell
uv run python .\main.py -tray
```

啟動後直接進入 RUN 監控模式：

```powershell
uv run python .\main.py -run
```

同時啟動 RUN 並縮到系統匣：

```powershell
uv run python .\main.py -run -tray
```

## CLI 用法

如果你有明確帶 CLI 參數，CLI 參數會覆蓋設定檔的啟動模式。

檢查 PLC HTTPS 通訊：

```powershell
uv run python .\main.py check --ip 10.80.1.10 --port 8001
```

驗證登入與 WebSocket 流程：

```powershell
uv run python .\main.py validate --ip 10.80.1.10 --port 8001 --password YOUR_PASSWORD
```

送出 reboot：

```powershell
uv run python .\main.py reboot --ip 10.80.1.10 --port 8001 --password YOUR_PASSWORD
```

檢查 OPC UA：

```powershell
uv run python .\main.py check-opcua --ip 10.80.1.10 --opc-port 48484
```

## 打包

建立 onefile：

```powershell
uv run --group build pyinstaller --clean --noconfirm .\UnistreamPLC_Reboot_onefile.spec
```

建立 onedir：

```powershell
uv run --group build pyinstaller --clean --noconfirm .\UnistreamPLC_Reboot_onedir.spec
```

## GitHub Actions

CI 目前也已改成 uv 流程：

- 安裝 `uv`
- 使用 `uv sync --locked --group build`
- 使用 `uv run --locked --group build pyinstaller ...` 打包

這樣本地與 CI 會共用同一份 `pyproject.toml` / `uv.lock`。

## UI 功能

圖形介面提供以下欄位與功能：

- PLC IP
- OPC UA Port
- PLC Password
- `Check PLC`
- `Validate`
- `Check OPC UA`
- `Reboot PLC`
- `RUN/Stop RUN`

RUN 模式會每 10 秒檢查一次 OPC UA；若檢查失敗，會自動執行 reboot，並套用 5 分鐘冷卻時間避免重複重啟。

## Reverse Engineering 摘要

此工具依據 clean-room 分析整理出以下流程：

1. 透過 `GET /v3/hwVer` 驗證 PLC HTTPS 可達性
2. 透過 `POST /v1/login` 送出加密後的 PLC password 與 client public key
3. 解密 PLC 回傳的 WebSocket token
4. 連線到 `wss://<PLC-IP>:8001/`
5. 送出 token 完成 WebSocket 驗證
6. 送出 `/v1/put/workingMode {"mode":"Reboot"}` 觸發 reboot

## 注意事項

- PLC HTTPS port 固定為 `8001`
- OPC UA 預設 port 為 `48484`
- `--password` 若留空，代表送出空字串，不會讀取任何 UniLogic 儲存的密碼
- 此專案只負責 clean-room 通訊，不包含官方工具的密碼保存機制

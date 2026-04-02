# Unistream PLC Reboot

這個專案用純 Python 重新實作 UniStream PLC 的 reboot 流程。

執行期間**不需要** `Unitronics.CommDriver.dll`、`Unitronics.Shell.UI.exe.config`，也不依賴任何 UniLogic 執行期元件。

## 已確認的 Reboot 指令

PLC 的通訊位置是：

- `https://<PLC-IP>:8001`
- `wss://<PLC-IP>:8001/`

已確認的 reboot WebSocket 文字指令如下：

```text
/v1/put/workingMode {"mode":"Reboot"} 
```

目前的 Python client 會自己完成整個流程：

  "username": "UniLogicUser",
 OPC UA Port（預設 48484）
  "key": "<client public key PEM>"
}
```
```json
{
  "key": "<base64 RSA 加密後的 WebSocket auth token>",
```

5. 用剛剛產生的 private key 解出 WebSocket auth token。
6. 連線到 `wss://<PLC-IP>:<port>/`。
7. 先送出 auth token。
2. `POST /v1/login`
3. WebSocket auth token
4. WebSocket `/v1/swVer`
5. WebSocket `/v1/put/workingMode {"mode":"Reboot"} `

## 主要檔案

- [main.py](/e:/py/UnistreamPLC_Reboot/main.py)：Tk UI 與 CLI 入口
- [unistream_client.py](/e:/py/UnistreamPLC_Reboot/unistream_client.py)：純 Python 的 HTTPS / WebSocket 實作

## 安裝方式

建立虛擬環境並安裝相依套件：

```powershell
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## 執行方式

啟動桌面 UI：

```powershell
python .\main.py
```

啟動後自動最小化到系統列：

```powershell
python .\main.py -tray
```

啟動後自動進入 RUN 監控：

```powershell
python .\main.py -run
```

同時自動最小化到系統列並自動 RUN：

```powershell
python .\main.py -run -tray
```

檢查 PLC 基本通訊：

```powershell
python .\main.py check --ip 10.80.1.10 --port 8001
```

驗證登入與 WebSocket 流程：

```powershell
python .\main.py validate --ip 10.80.1.10 --port 8001 --password YOUR_PASSWORD
```

送出 reboot：

```powershell
python .\main.py reboot --ip 10.80.1.10 --port 8001 --password YOUR_PASSWORD
```

檢查 OPC UA 通訊：

```powershell
python .\main.py check-opcua --ip 10.80.1.10 --opc-port 48484
```

若使用打包後的 EXE，也可用相同參數：

```powershell
.\dist\UnistreamPLC_Reboot_onefile.exe -run -tray
```

如果 PLC 沒有設定 communication password，`--password` 可以留空，或在 UI 中把密碼欄位留白。

## UI 功能

目前 UI 提供：

- PLC IP
- OPC UA Port（預設 48484）
- PLC Password
- `Check PLC`
- `Validate`
- `Check OPC UA`
- `Reboot PLC`
- `RUN/Stop RUN`

各按鈕用途：

- PLC HTTPS Port 固定為 `8001`，不需在 UI 輸入
- `Check PLC`：做未登入的 `GET /v3/hwVer`，確認 PLC 在 `8001` 的 HTTPS 通訊是否正常
- `Validate`：做完整的 HTTPS login + WebSocket `/v1/swVer` 驗證
- `Check OPC UA`：做 OPC UA 連線檢查，使用 `opc.tcp://<PLC-IP>:<OPC-UA-Port>`，Security=None、Anonymous
- `Reboot PLC`：做完整登入後送出 reboot 指令
- `RUN`：每 10 秒做一次 OPC UA 檢查；若失敗會自動 reboot，一次 reboot 後冷卻 5 分鐘再恢復檢查

其他 UI 行為：

- PLC Password 預設值為 `Blue0324!`，預設遮罩顯示，可用眼睛按鈕切換
- 視窗最小化時會縮到系統列，並使用 `lioil.ico`
- 系統列背景下仍會持續執行 RUN 監控

## 目前限制

- 這個 clean-room 版本目前需要你自己輸入 PLC communication password。
- 目前**不會**去讀 UniLogic 本機保存的密碼。
- 如果其他程式已經連上 PLC，可能會影響 validate 或 reboot。

常見阻擋來源：

- UniLogic 正在線上連 PLC
- 其他工具已經占用 `8001` 連線

## Reverse Engineering 結論

透過封包與 Frida 分析，目前已確認：

- PLC 使用 `8001` 上的 HTTPS / WSS
- 先做 `/v1/login`
- 之後升級為 WebSocket
- reboot 指令就是 `/v1/put/workingMode {"mode":"Reboot"} `
- login 時 PLC password 會先經過 RSA 加密
- PLC 回傳的 WebSocket auth token 也會經過 RSA 加密


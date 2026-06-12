# MT4 XAUUSD / Binance Gold Arb Executor

独立的 MT4 XAUUSD 与 Binance 黄金相关合约/现货价差执行器。默认 `PAPER_MODE=true`、`LIVE_TRADING=false`，只能 dry-run/demo，不会直接实盘下单。

## 交易逻辑

- Binance 高于 MT4：
  - Binance `SELL LIMIT`，`timeInForce=GTX` Post Only。
  - Binance 成交多少，MT4 立即 `BUY` 对冲多少。
  - MT4 买入失败或超过允许价格，立即对 Binance 已成交数量做 market emergency close。
- Binance 低于 MT4：
  - Binance `BUY LIMIT`，`timeInForce=GTX` Post Only。
  - Binance 成交多少，MT4 立即 `SELL` 对冲多少。
  - MT4 卖出失败或低于允许价格，立即对 Binance 已成交数量做 market emergency close。
- 出场先 Binance Post Only，成交后 MT4 反向平仓。

状态机：

`IDLE -> QUOTING_BINANCE_ENTRY -> HEDGING_MT4 -> PAIR_OPEN -> QUOTING_BINANCE_EXIT -> CLOSING_MT4 -> IDLE`

异常：

`UNHEDGED -> EMERGENCY_CLOSE_BINANCE -> PAUSED`

## 安装

Windows VPS 推荐 Python 3.11+：

```powershell
cd C:\arb-bot
copy .env.example .env
notepad .env
.\scripts\run.ps1 -HostName 127.0.0.1 -Port 8011
```

Linux 验证：

```bash
cd /root/arb-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8011
```

## 前端页面

服务器已上线到：

```text
https://redzhong.top/xau-arb/
```

该入口继承 `redzhong.top` 的 Basic Auth 保护。执行器只监听本机：

```text
127.0.0.1:8011
```

页面包含：

- 状态机、dry-run/live 模式、Binance/MT4 连接状态
- Binance/MT4 最新 bid/ask
- 当前持仓摘要
- dry-run 行情推送
- MT4 命令拉取与模拟回报

Linux systemd 模板：

```bash
sudo cp deploy/xau-arb-bot.service /etc/systemd/system/xau-arb-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now xau-arb-bot
```

## MT4 EA 安装

1. 把 `mt4/ArbBridgeEA.mq4` 放到 MT4 数据目录：
   `MQL4\Experts\ArbBridgeEA.mq4`
2. 在 MetaEditor 编译。
3. MT4 菜单：`工具 -> 选项 -> EA 交易`
4. 勾选 `允许 WebRequest 用于下列 URL`，加入：
   `http://127.0.0.1:8011`
5. 把 EA 挂到 `XAUUSD` 图表。
6. EA 参数：
   - `BridgeBaseUrl`: `http://127.0.0.1:8011`
   - `BridgeToken`: 与 `.env` 的 `MT4_BRIDGE_TOKEN` 一致；为空则不校验。
   - `TradeSymbol`: `XAUUSD`
   - `PollMs`: `100`
   - `UploadHistoryOnStart`: 建议保持开启，EA 启动后会自动补传最近 7 天历史K线。
   - `HistoryTimeframeMinutes`: 默认 `1`，用于网页里的过去7天价差分析。

网页中的“历史价差”面板会用 MT4 已上传的历史K线，对齐 Binance `XAUUSDT` 同周期K线，检查过去7天有没有回到指定阈值内。

## Dry-run 验证

`.env` 默认就是 dry-run：

```env
LIVE_TRADING=false
PAPER_MODE=true
```

启动后检查：

```bash
curl http://127.0.0.1:8011/health
curl http://127.0.0.1:8011/status
```

模拟 Binance 行情：

```bash
curl -X POST "http://127.0.0.1:8011/paper/binance/book?bid=2001&ask=2002"
```

模拟 MT4 tick：

```bash
curl -X POST http://127.0.0.1:8011/mt4/tick \
  -H "Content-Type: application/json" \
  -d '{"symbol":"XAUUSD","bid":"1999","ask":"2000","positions":[]}'
```

等待策略报价和 paper fill 后拉 MT4 命令：

```bash
curl http://127.0.0.1:8011/mt4/command
```

模拟 MT4 成功回报：

```bash
curl -X POST http://127.0.0.1:8011/mt4/report \
  -H "Content-Type: application/json" \
  -d '{"command_id":"替换为上一步返回值","status":"ok","action":"BUY","ticket":10001,"fill_price":"2000","lots":"0.01"}'
```

## 开启 demo/live

不要在未小额验证前开启实盘。实盘必须同时满足：

```env
LIVE_TRADING=true
PAPER_MODE=false
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
MT4_BRIDGE_TOKEN=设置一个随机长字符串
```

同时确认以下参数：

```env
TARGET_OZ=1
OPEN_MIN_EDGE=1.50
CLOSE_MAX_SPREAD=0.30
MIN_LOCKED_EDGE=0.80
MAX_ORDER_AGE_MS=300
MAX_QUOTE_AGE_MS=500
MAX_HEDGE_DELAY_MS=800
MAX_UNHEDGED_LOSS_USD_PER_OZ=0.80
DAILY_LOSS_LIMIT_USDT=50
MT4_LOT_SIZE_OZ=100
MT4_SLIPPAGE_POINTS=30
```

Binance maker fee 启动时会优先调用 `/fapi/v1/commissionRate`。如果接口不可用，才使用 `.env` 的 `BINANCE_MAKER_FEE_RATE`。日志只会打印脱敏摘要，不会打印密钥或签名。

## Windows 自启动

管理员 PowerShell：

```powershell
cd C:\arb-bot
.\scripts\install_windows_task.ps1 -TaskName MT4BinanceXauArbBot -HostName 127.0.0.1 -Port 8011
```

## 测试

```bash
cd /root/arb-bot
pytest
```

覆盖：

- Binance 高开仓公式
- Binance 低开仓公式
- 部分成交只对冲部分
- MT4 失败触发 Binance emergency close
- 行情过期撤单并暂停

## 安全约束

- 不提交 `.env`
- 不硬编码 API key/secret/账号/密码
- 日志不打印密钥、完整签名或账户敏感信息
- MT4 对冲失败时，Binance emergency close 允许 taker，因为优先风控

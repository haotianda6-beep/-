# MT4 Bridge 安装说明

1. 把 `PerpArbMt4Bridge.mq4` 放到 MT4 数据目录的 `MQL4/Experts/`。
2. 在 MetaEditor 里编译，生成 `PerpArbMt4Bridge.ex4`。
3. MT4 里打开 `工具 -> 选项 -> EA交易`，勾选 `允许 WebRequest`，并添加：
   `https://redzhong.top`
4. 把 EA 挂到任意一个图表。
5. EA 参数：
   - `BridgeUrl`: `https://redzhong.top/api/mt4/quote`
   - `BridgeToken`: 与服务器 `.env` 里的 `MT4_BRIDGE_TOKEN` 保持一致
   - `CommoditySymbols`: 大宗商品符号，按你的 MT4 券商实际符号填写
   - `StockSymbols`: 美股个股符号，按你的 MT4 券商实际符号填写
   - `PushIntervalSeconds`: 默认 1 秒

注意：不同 MT4 券商的符号可能带后缀，例如 `XAUUSD.m`、`AAPL.cash`。后端会去掉非字母数字字符并转大写，但交易所合约映射仍需要确认实际名称。
如需扩展映射，复制 `config/mt4_symbols.example.json` 为 `config/mt4_symbols.json`，把 MT4 品种和五所合约别名填进去。

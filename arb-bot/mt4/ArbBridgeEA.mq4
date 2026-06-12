#property strict

input string BridgeBaseUrl = "http://127.0.0.1:8011";
input string BridgeToken = "";
input string TradeSymbol = "XAUUSD";
input int PollMs = 50;
input int MagicNumber = 260612;
input double DefaultLots = 0.01;
input bool UploadHistoryOnStart = true;
input int HistoryDays = 7;
input int HistoryTimeframeMinutes = 1;
input int HistoryChunkBars = 300;

datetime lastTickSent = 0;
bool historyStarted = false;
bool historyDone = false;
int historyNextShift = 0;
int historyPeriod = PERIOD_M1;
string historyInterval = "1m";

void PostTick();
void PrepareHistoryUpload();
bool UploadHistoryChunk();
void ExecuteMarket(string commandId, string symbol, int type, double lots, int slippage, double maxPrice, double minPrice);
void ExecuteClose(string commandId, int ticket, double lots, int slippage);
void PostReport(string commandId, string status, string action, int ticket, double fillPrice, double lots, int errorCode, string message);
string PositionsJson();
string HttpGet(string path);
string HttpPost(string path, string body);
int HistoryPeriod();
string HistoryInterval();
long NextRolloverMs();
string JsonString(string json, string key);
double JsonDouble(string json, string key, double fallback);
string JsonEscape(string value);
string UrlEncode(string value);
string OrderActionName(int type);

int OnInit()
{
   SymbolSelect(TradeSymbol, true);
   EventSetMillisecondTimer(PollMs);
   Print("ArbBridgeEA started. Add WebRequest whitelist: ", BridgeBaseUrl);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTick()
{
   if (Symbol() != TradeSymbol) return;
   PostTick();
}

void OnTimer()
{
   if (UploadHistoryOnStart && !historyDone)
   {
      if (!historyStarted) PrepareHistoryUpload();
      UploadHistoryChunk();
   }

   string body = HttpGet("/mt4/command");
   if (StringLen(body) <= 0) return;
   string action = JsonString(body, "action");
   if (action == "" || action == "NONE") return;

   string commandId = JsonString(body, "command_id");
   string symbol = JsonString(body, "symbol");
   double lots = JsonDouble(body, "lots", DefaultLots);
   int slippage = (int)JsonDouble(body, "slippage_points", 30);
   double maxPrice = JsonDouble(body, "max_price", 0);
   double minPrice = JsonDouble(body, "min_price", 0);
   int ticket = (int)JsonDouble(body, "ticket", 0);

   if (symbol == "") symbol = TradeSymbol;
   if (action == "BUY") ExecuteMarket(commandId, symbol, OP_BUY, lots, slippage, maxPrice, minPrice);
   else if (action == "SELL") ExecuteMarket(commandId, symbol, OP_SELL, lots, slippage, maxPrice, minPrice);
   else if (action == "CLOSE") ExecuteClose(commandId, ticket, lots, slippage);
}

void PostTick()
{
   RefreshRates();
   string positions = PositionsJson();
   string json = "{";
   json += "\"symbol\":\"" + JsonEscape(TradeSymbol) + "\",";
   json += "\"bid\":" + DoubleToString(Bid, Digits) + ",";
   json += "\"ask\":" + DoubleToString(Ask, Digits) + ",";
   json += "\"swap_long_per_lot\":" + DoubleToString(MarketInfo(TradeSymbol, MODE_SWAPLONG), 8) + ",";
   json += "\"swap_short_per_lot\":" + DoubleToString(MarketInfo(TradeSymbol, MODE_SWAPSHORT), 8) + ",";
   json += "\"swap_type\":" + IntegerToString((int)MarketInfo(TradeSymbol, MODE_SWAPTYPE)) + ",";
   json += "\"tick_value\":" + DoubleToString(MarketInfo(TradeSymbol, MODE_TICKVALUE), 8) + ",";
   json += "\"tick_size\":" + DoubleToString(MarketInfo(TradeSymbol, MODE_TICKSIZE), 8) + ",";
   json += "\"point\":" + DoubleToString(MarketInfo(TradeSymbol, MODE_POINT), 8) + ",";
   json += "\"next_rollover_time_ms\":" + IntegerToString(NextRolloverMs()) + ",";
   long timestampMs = (long)TimeCurrent() * 1000;
   json += "\"timestamp_ms\":" + IntegerToString(timestampMs) + ",";
   json += "\"positions\":" + positions;
   json += "}";
   HttpPost("/mt4/tick", json);
}

void PrepareHistoryUpload()
{
   historyPeriod = HistoryPeriod();
   historyInterval = HistoryInterval();
   int totalBars = iBars(TradeSymbol, historyPeriod);
   int minutes = HistoryTimeframeMinutes;
   if (minutes < 1) minutes = 1;
   int days = HistoryDays;
   if (days < 1) days = 1;
   int wantedBars = days * (1440 / minutes);
   historyNextShift = totalBars - 1;
   if (historyNextShift > wantedBars) historyNextShift = wantedBars;
   historyStarted = true;
   if (historyNextShift <= 0)
   {
      historyDone = true;
      Print("History upload skipped. No closed bars for ", TradeSymbol);
      return;
   }
   Print("History upload started symbol=", TradeSymbol, " interval=", historyInterval, " bars=", historyNextShift);
}

bool UploadHistoryChunk()
{
   if (historyDone || historyNextShift <= 0) return false;

   int sent = 0;
   int chunkBars = HistoryChunkBars;
   if (chunkBars < 1) chunkBars = 1;
   int digits = (int)MarketInfo(TradeSymbol, MODE_DIGITS);
   int serverOffsetSec = (int)MathRound((TimeCurrent() - TimeGMT()) / 60.0) * 60;
   string bars = "[";

   while (historyNextShift >= 1 && sent < chunkBars)
   {
      datetime openTime = iTime(TradeSymbol, historyPeriod, historyNextShift);
      if (openTime <= 0)
      {
         historyNextShift--;
         continue;
      }
      long openMs = (long)(openTime - serverOffsetSec) * 1000;
      if (sent > 0) bars += ",";
      bars += "{";
      bars += "\"open_time_ms\":" + IntegerToString(openMs) + ",";
      bars += "\"open\":" + DoubleToString(iOpen(TradeSymbol, historyPeriod, historyNextShift), digits) + ",";
      bars += "\"high\":" + DoubleToString(iHigh(TradeSymbol, historyPeriod, historyNextShift), digits) + ",";
      bars += "\"low\":" + DoubleToString(iLow(TradeSymbol, historyPeriod, historyNextShift), digits) + ",";
      bars += "\"close\":" + DoubleToString(iClose(TradeSymbol, historyPeriod, historyNextShift), digits) + ",";
      bars += "\"volume\":" + DoubleToString((double)iVolume(TradeSymbol, historyPeriod, historyNextShift), 2);
      bars += "}";
      sent++;
      historyNextShift--;
   }

   bars += "]";
   if (sent <= 0)
   {
      historyDone = true;
      return false;
   }

   string json = "{";
   json += "\"symbol\":\"" + JsonEscape(TradeSymbol) + "\",";
   json += "\"interval\":\"" + JsonEscape(historyInterval) + "\",";
   json += "\"bars\":" + bars;
   json += "}";
   HttpPost("/mt4/history", json);

   if (historyNextShift <= 0)
   {
      historyDone = true;
      Print("History upload completed symbol=", TradeSymbol, " interval=", historyInterval);
   }
   return true;
}

void ExecuteMarket(string commandId, string symbol, int type, double lots, int slippage, double maxPrice, double minPrice)
{
   RefreshRates();
   double price = (type == OP_BUY) ? MarketInfo(symbol, MODE_ASK) : MarketInfo(symbol, MODE_BID);
   if (type == OP_BUY && maxPrice > 0 && price > maxPrice)
   {
      PostReport(commandId, "error", "BUY", -1, 0, lots, 9001, "max price exceeded");
      return;
   }
   if (type == OP_SELL && minPrice > 0 && price < minPrice)
   {
      PostReport(commandId, "error", "SELL", -1, 0, lots, 9002, "min price exceeded");
      return;
   }
   int ticket = OrderSend(symbol, type, lots, price, slippage, 0, 0, "arb hedge", MagicNumber, 0, clrDodgerBlue);
   if (ticket < 0)
   {
      int err = GetLastError();
      PostReport(commandId, "error", OrderActionName(type), -1, 0, lots, err, "OrderSend failed");
      ResetLastError();
      return;
   }
   PostReport(commandId, "ok", OrderActionName(type), ticket, price, lots, 0, "filled");
}

void ExecuteClose(string commandId, int ticket, double lots, int slippage)
{
   if (!OrderSelect(ticket, SELECT_BY_TICKET))
   {
      PostReport(commandId, "error", "CLOSE", ticket, 0, lots, GetLastError(), "ticket not found");
      ResetLastError();
      return;
   }
   RefreshRates();
   int type = OrderType();
   double closeLots = lots > 0 ? MathMin(lots, OrderLots()) : OrderLots();
   double price = (type == OP_BUY) ? MarketInfo(OrderSymbol(), MODE_BID) : MarketInfo(OrderSymbol(), MODE_ASK);
   bool ok = OrderClose(ticket, closeLots, price, slippage, clrTomato);
   if (!ok)
   {
      int err = GetLastError();
      PostReport(commandId, "error", "CLOSE", ticket, price, closeLots, err, "OrderClose failed");
      ResetLastError();
      return;
   }
   PostReport(commandId, "ok", "CLOSE", ticket, price, closeLots, 0, "closed");
}

void PostReport(string commandId, string status, string action, int ticket, double fillPrice, double lots, int errorCode, string message)
{
   string json = "{";
   json += "\"command_id\":\"" + JsonEscape(commandId) + "\",";
   json += "\"status\":\"" + JsonEscape(status) + "\",";
   json += "\"action\":\"" + JsonEscape(action) + "\",";
   json += "\"ticket\":" + IntegerToString(ticket) + ",";
   json += "\"fill_price\":" + DoubleToString(fillPrice, Digits) + ",";
   json += "\"lots\":" + DoubleToString(lots, 2) + ",";
   json += "\"error_code\":" + IntegerToString(errorCode) + ",";
   json += "\"message\":\"" + JsonEscape(message) + "\"";
   json += "}";
   HttpPost("/mt4/report", json);
}

string PositionsJson()
{
   string json = "[";
   bool first = true;
   for (int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderSymbol() != TradeSymbol || OrderMagicNumber() != MagicNumber) continue;
      if (OrderType() != OP_BUY && OrderType() != OP_SELL) continue;
      if (!first) json += ",";
      first = false;
      string side = OrderType() == OP_BUY ? "BUY" : "SELL";
      json += "{";
      json += "\"ticket\":" + IntegerToString(OrderTicket()) + ",";
      json += "\"symbol\":\"" + JsonEscape(OrderSymbol()) + "\",";
      json += "\"side\":\"" + side + "\",";
      json += "\"lots\":" + DoubleToString(OrderLots(), 2) + ",";
      json += "\"open_price\":" + DoubleToString(OrderOpenPrice(), Digits) + ",";
      json += "\"profit\":" + DoubleToString(OrderProfit(), 2) + ",";
      json += "\"swap\":" + DoubleToString(OrderSwap(), 2);
      json += "}";
   }
   json += "]";
   return json;
}

string HttpGet(string path)
{
   char data[];
   char result[];
   string headers = "X-MT4-Token: " + BridgeToken + "\r\n";
   string resultHeaders = "";
   int code = WebRequest("GET", BridgeBaseUrl + path, headers, 1000, data, result, resultHeaders);
   if (code < 200 || code >= 300) return "";
   return CharArrayToString(result, 0, -1, CP_UTF8);
}

string HttpPost(string path, string body)
{
   char data[];
   char result[];
   string headers = "Content-Type: application/json\r\nX-MT4-Token: " + BridgeToken + "\r\n";
   string resultHeaders = "";
   int len = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if (len > 0) ArrayResize(data, len - 1);
   int code = WebRequest("POST", BridgeBaseUrl + path, headers, 1000, data, result, resultHeaders);
   if (code < 200 || code >= 300) Print("Bridge POST failed code=", code, " path=", path);
   return CharArrayToString(result, 0, -1, CP_UTF8);
}

int HistoryPeriod()
{
   if (HistoryTimeframeMinutes == 5) return PERIOD_M5;
   if (HistoryTimeframeMinutes == 15) return PERIOD_M15;
   if (HistoryTimeframeMinutes == 60) return PERIOD_H1;
   return PERIOD_M1;
}

string HistoryInterval()
{
   if (HistoryTimeframeMinutes == 5) return "5m";
   if (HistoryTimeframeMinutes == 15) return "15m";
   if (HistoryTimeframeMinutes == 60) return "1h";
   return "1m";
}

long NextRolloverMs()
{
   datetime nowServer = TimeCurrent();
   datetime serverDate = StrToTime(TimeToString(nowServer, TIME_DATE));
   datetime nextServer = serverDate + 86400;
   int serverOffsetSec = (int)MathRound((TimeCurrent() - TimeGMT()) / 60.0) * 60;
   return (long)(nextServer - serverOffsetSec) * 1000;
}

string JsonString(string json, string key)
{
   string needle = "\"" + key + "\":";
   int pos = StringFind(json, needle);
   if (pos < 0) return "";
   pos += StringLen(needle);
   while (pos < StringLen(json) && StringGetChar(json, pos) == ' ') pos++;
   if (StringGetChar(json, pos) != '"') return "";
   pos++;
   int end = StringFind(json, "\"", pos);
   if (end < 0) return "";
   return StringSubstr(json, pos, end - pos);
}

double JsonDouble(string json, string key, double fallback)
{
   string needle = "\"" + key + "\":";
   int pos = StringFind(json, needle);
   if (pos < 0) return fallback;
   pos += StringLen(needle);
   while (pos < StringLen(json) && StringGetChar(json, pos) == ' ') pos++;
   int end = pos;
   while (end < StringLen(json))
   {
      int ch = StringGetChar(json, end);
      if ((ch >= '0' && ch <= '9') || ch == '-' || ch == '.') end++;
      else break;
   }
   if (end <= pos) return fallback;
   return StrToDouble(StringSubstr(json, pos, end - pos));
}

string JsonEscape(string value)
{
   string out = value;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   return out;
}

string UrlEncode(string value)
{
   string out = "";
   for (int i = 0; i < StringLen(value); i++)
   {
      int ch = StringGetChar(value, i);
      if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9')) out += CharToString((uchar)ch);
      else if (ch == '-' || ch == '_' || ch == '.' || ch == '~') out += CharToString((uchar)ch);
      else out += "%" + StringFormat("%02X", ch);
   }
   return out;
}

string OrderActionName(int type)
{
   if (type == OP_BUY) return "BUY";
   return "SELL";
}

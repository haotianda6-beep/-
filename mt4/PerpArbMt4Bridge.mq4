// PerpArbMt4Bridge.mq4
// Attach this EA to one chart. It pushes selected MT4 symbols to the backend.
#property strict
#property version "1.00"

input string BridgeUrl = "https://redzhong.top/api/mt4/quote";
input string BridgeToken = "";
input string CommoditySymbols = "XAUUSD,XAGUSD,USOIL,UKOIL,NATGAS";
input string StockSymbols = "AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL";
input double LotsForSwapEstimate = 1.0;
input int PushIntervalSeconds = 1;
input int RequestTimeoutMs = 3000;

int OnInit()
{
   EventSetTimer(MathMax(PushIntervalSeconds, 1));
   Print("PerpArb MT4 bridge started. Allow WebRequest URL: ", BridgeUrl);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   PushSymbols(CommoditySymbols, "commodity");
   PushSymbols(StockSymbols, "stock");
}

void PushSymbols(string symbols, string instrumentType)
{
   string parts[];
   int count = StringSplit(symbols, ',', parts);
   for(int i = 0; i < count; i++)
   {
      string symbol = Trim(parts[i]);
      if(symbol == "") continue;
      PushOne(symbol, instrumentType);
   }
}

void PushOne(string symbol, string instrumentType)
{
   if(!SymbolSelect(symbol, true))
   {
      Print("SymbolSelect failed: ", symbol);
      return;
   }

   RefreshRates();
   double bid = MarketInfo(symbol, MODE_BID);
   double ask = MarketInfo(symbol, MODE_ASK);
   if(bid <= 0 || ask <= 0)
   {
      Print("Invalid quote: ", symbol, " bid=", bid, " ask=", ask);
      return;
   }

   double contractSize = MarketInfo(symbol, MODE_LOTSIZE);
   double tickValue = MarketInfo(symbol, MODE_TICKVALUE);
   double tickSize = MarketInfo(symbol, MODE_TICKSIZE);
   double swapLong = MarketInfo(symbol, MODE_SWAPLONG);
   double swapShort = MarketInfo(symbol, MODE_SWAPSHORT);

   string body = "{"
      + "\"symbol\":\"" + JsonEscape(symbol) + "\","
      + "\"instrument_type\":\"" + instrumentType + "\","
      + "\"bid\":" + DoubleToString(bid, DigitsFor(symbol)) + ","
      + "\"ask\":" + DoubleToString(ask, DigitsFor(symbol)) + ","
      + "\"contract_size\":" + DoubleToString(contractSize, 8) + ","
      + "\"lots\":" + DoubleToString(LotsForSwapEstimate, 4) + ","
      + "\"tick_value\":" + DoubleToString(tickValue, 8) + ","
      + "\"tick_size\":" + DoubleToString(tickSize, 8) + ","
      + "\"swap_long_points\":" + DoubleToString(swapLong, 8) + ","
      + "\"swap_short_points\":" + DoubleToString(swapShort, 8)
      + "}";

   char post[];
   int bytes = StringToCharArray(body, post, 0, WHOLE_ARRAY, CP_UTF8);
   if(bytes > 0) ArrayResize(post, bytes - 1);

   char result[];
   string resultHeaders;
   string headers = "Content-Type: application/json\r\nX-MT4-Token: " + BridgeToken + "\r\n";
   int status = WebRequest("POST", BridgeUrl, headers, RequestTimeoutMs, post, result, resultHeaders);
   if(status < 200 || status >= 300)
   {
      Print("Bridge push failed: ", symbol, " status=", status, " error=", GetLastError());
   }
}

int DigitsFor(string symbol)
{
   int digits = (int)MarketInfo(symbol, MODE_DIGITS);
   if(digits < 0 || digits > 10) return 5;
   return digits;
}

string Trim(string value)
{
   value = StringTrimLeft(value);
   value = StringTrimRight(value);
   return value;
}

string JsonEscape(string value)
{
   string result = value;
   StringReplace(result, "\\", "\\\\");
   StringReplace(result, "\"", "\\\"");
   return result;
}

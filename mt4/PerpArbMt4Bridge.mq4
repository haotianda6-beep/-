// PerpArbMt4Bridge.mq4
// Attach this EA to one chart. It pushes selected MT4 symbols to the backend.
#property strict
#property version "1.00"

input string BridgeUrl = "https://redzhong.top/api/mt4/quote";
input string BridgeToken = "";
input string CommoditySymbols = "XAUUSD,XAGUSD,USOIL,UKOIL,NATGAS";
input string StockSymbols = "AAPL.NAS,AMZN.NAS,GOOG.NAS,META.NAS,MSFT.NAS,NVDA.NAS,TSLA.NAS";
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
   string actualSymbol = ResolveSymbol(symbol);
   if(actualSymbol == "")
   {
      Print("SymbolSelect failed: ", symbol);
      return;
   }

   RefreshRates();
   double bid = MarketInfo(actualSymbol, MODE_BID);
   double ask = MarketInfo(actualSymbol, MODE_ASK);
   if(bid <= 0 || ask <= 0)
   {
      Print("Invalid quote: ", actualSymbol, " bid=", bid, " ask=", ask);
      return;
   }

   double contractSize = MarketInfo(actualSymbol, MODE_LOTSIZE);
   double tickValue = MarketInfo(actualSymbol, MODE_TICKVALUE);
   double tickSize = MarketInfo(actualSymbol, MODE_TICKSIZE);
   double swapLong = MarketInfo(actualSymbol, MODE_SWAPLONG);
   double swapShort = MarketInfo(actualSymbol, MODE_SWAPSHORT);

   string body = "{"
      + "\"symbol\":\"" + JsonEscape(actualSymbol) + "\","
      + "\"instrument_type\":\"" + instrumentType + "\","
      + "\"bid\":" + DoubleToString(bid, DigitsFor(actualSymbol)) + ","
      + "\"ask\":" + DoubleToString(ask, DigitsFor(actualSymbol)) + ","
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
      Print("Bridge push failed: ", actualSymbol, " status=", status, " error=", GetLastError());
   }
}

string ResolveSymbol(string requested)
{
   if(SymbolSelect(requested, true)) return requested;

   int total = SymbolsTotal(false);
   for(int i = 0; i < total; i++)
   {
      string candidate = SymbolName(i, false);
      if(SymbolMatches(requested, candidate) && SymbolSelect(candidate, true))
      {
         Print("Resolved symbol: ", requested, " -> ", candidate);
         return candidate;
      }
   }
   return "";
}

bool SymbolMatches(string requested, string candidate)
{
   string target = NormalizeSymbolName(requested);
   string actual = NormalizeSymbolName(candidate);
   if(actual == target) return true;
   if(StringFind(actual, target) == 0) return true;
   int pos = StringFind(actual, target);
   return pos >= 0 && pos + StringLen(target) == StringLen(actual);
}

string NormalizeSymbolName(string value)
{
   string result = "";
   for(int i = 0; i < StringLen(value); i++)
   {
      int ch = StringGetCharacter(value, i);
      if(ch >= 97 && ch <= 122) ch -= 32;
      if((ch >= 65 && ch <= 90) || (ch >= 48 && ch <= 57))
      {
         result += CharToString((uchar)ch);
      }
   }
   return result;
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

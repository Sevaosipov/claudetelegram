//+------------------------------------------------------------------+
//| BotBridge.mq5 -- writes bars and quotes to CSV files that         |
//| disclosure-bot reads (cfd/mt5_bridge.py). It never trades: there  |
//| is no order function in this file.                                |
//|                                                                    |
//| Attach it to one chart (any symbol) and leave the terminal open.  |
//| Files go to the terminal's common folder, in botbridge/:          |
//|   <SYMBOL>_<TF>_recent.csv   the last RecentBars bars, refreshed   |
//|   <SYMBOL>_<TF>_history.csv  up to HistoryBars bars, every 6 hours |
//|   quotes.csv                 bid, ask and the clocks, refreshed    |
//|   account.csv, positions.csv, deals.csv   the account's balance, |
//|                              its open positions and the deals of  |
//|                              the last 90 days, once a minute       |
//| Times are the trade server's; the reader converts them.           |
//+------------------------------------------------------------------+
#property copyright "disclosure-bot"
#property version   "1.10"
#property description "Writes bars and quotes to files for disclosure-bot. It never trades."

input string Symbols        = "XAUUSD,EURUSD";  // symbols, comma-separated, as Market Watch names them
input string Timeframes     = "M15,H1";         // M1 M5 M15 M30 H1 H4 D1
input int    RecentBars     = 600;              // bars in the file refreshed every RefreshSeconds
input int    HistoryBars    = 200000;           // bars in the history file
input int    RefreshSeconds = 20;

string   g_symbols[];
string   g_tfs[];
datetime g_history_at = 0;
datetime g_account_at = 0;
int      g_history_rows = 0;

ENUM_TIMEFRAMES TfOf(const string name)
  {
   if(name == "M1")  return PERIOD_M1;
   if(name == "M5")  return PERIOD_M5;
   if(name == "M15") return PERIOD_M15;
   if(name == "M30") return PERIOD_M30;
   if(name == "H1")  return PERIOD_H1;
   if(name == "H4")  return PERIOD_H4;
   if(name == "D1")  return PERIOD_D1;
   return PERIOD_CURRENT;
  }

void Trim(string &items[])
  {
   for(int i = 0; i < ArraySize(items); i++)
     {
      StringTrimLeft(items[i]);
      StringTrimRight(items[i]);
     }
  }

// The last `count` bars of a symbol to botbridge/<symbol>_<tf>_<kind>.csv, oldest first; the file is
// written beside its name and moved over it, so a reader never sees half a file. Returns the rows written.
int WriteBars(const string symbol, const string tf, const int count, const string kind)
  {
   MqlRates rates[];
   int n = CopyRates(symbol, TfOf(tf), 0, count, rates);
   if(n <= 0)
      return 0;
   string name = "botbridge\\" + symbol + "_" + tf + "_" + kind;
   int handle = FileOpen(name + ".tmp", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return 0;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   FileWriteString(handle, "time,open,high,low,close,tick_volume,spread_points\n");
   for(int i = 0; i < n; i++)
      FileWriteString(handle, TimeToString(rates[i].time, TIME_DATE | TIME_MINUTES) + ","
                      + DoubleToString(rates[i].open, digits) + "," + DoubleToString(rates[i].high, digits) + ","
                      + DoubleToString(rates[i].low, digits) + "," + DoubleToString(rates[i].close, digits) + ","
                      + IntegerToString(rates[i].tick_volume) + "," + IntegerToString(rates[i].spread) + "\n");
   FileClose(handle);
   FileMove(name + ".tmp", FILE_COMMON, name + ".csv", FILE_COMMON | FILE_REWRITE);
   return n;
  }

void WriteQuotes()
  {
   int handle = FileOpen("botbridge\\quotes.tmp", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return;
   FileWriteString(handle, "symbol,bid,ask,point,server_time,gmt_time\n");
   for(int i = 0; i < ArraySize(g_symbols); i++)
     {
      string s = g_symbols[i];
      int digits = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
      FileWriteString(handle, s + "," + DoubleToString(SymbolInfoDouble(s, SYMBOL_BID), digits) + ","
                      + DoubleToString(SymbolInfoDouble(s, SYMBOL_ASK), digits) + ","
                      + DoubleToString(SymbolInfoDouble(s, SYMBOL_POINT), digits) + ","
                      + TimeToString(TimeTradeServer(), TIME_DATE | TIME_SECONDS) + ","
                      + TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS) + "\n");
     }
   FileClose(handle);
   FileMove("botbridge\\quotes.tmp", FILE_COMMON, "botbridge\\quotes.csv", FILE_COMMON | FILE_REWRITE);
  }

// The account, read only: its balance, what is open, and the deals of the last 90 days.
void WriteAccount()
  {
   int handle = FileOpen("botbridge\\account.tmp", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle != INVALID_HANDLE)
     {
      FileWriteString(handle, "balance,equity,margin,free_margin,currency,leverage,server_time\n");
      FileWriteString(handle, DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + ","
                      + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) + ","
                      + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 2) + ","
                      + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) + ","
                      + AccountInfoString(ACCOUNT_CURRENCY) + ","
                      + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + ","
                      + TimeToString(TimeTradeServer(), TIME_DATE | TIME_SECONDS) + "\n");
      FileClose(handle);
      FileMove("botbridge\\account.tmp", FILE_COMMON, "botbridge\\account.csv", FILE_COMMON | FILE_REWRITE);
     }

   handle = FileOpen("botbridge\\positions.tmp", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle != INVALID_HANDLE)
     {
      FileWriteString(handle, "ticket,time,symbol,side,volume,price_open,sl,tp,profit,swap\n");
      for(int i = 0; i < PositionsTotal(); i++)
        {
         ulong ticket = PositionGetTicket(i);
         if(ticket == 0)
            continue;
         string symbol = PositionGetString(POSITION_SYMBOL);
         int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
         FileWriteString(handle, IntegerToString((long)ticket) + ","
                         + TimeToString((datetime)PositionGetInteger(POSITION_TIME), TIME_DATE | TIME_SECONDS) + ","
                         + symbol + ","
                         + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell") + ","
                         + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + ","
                         + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), digits) + ","
                         + DoubleToString(PositionGetDouble(POSITION_SL), digits) + ","
                         + DoubleToString(PositionGetDouble(POSITION_TP), digits) + ","
                         + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + ","
                         + DoubleToString(PositionGetDouble(POSITION_SWAP), 2) + "\n");
        }
      FileClose(handle);
      FileMove("botbridge\\positions.tmp", FILE_COMMON, "botbridge\\positions.csv", FILE_COMMON | FILE_REWRITE);
     }

   datetime to = TimeTradeServer() + 86400;
   if(!HistorySelect(to - 91 * 86400, to))
      return;
   handle = FileOpen("botbridge\\deals.tmp", FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return;
   FileWriteString(handle, "ticket,time,symbol,side,entry,volume,price,profit,commission,swap,position\n");
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong deal = HistoryDealGetTicket(i);
      if(deal == 0)
         continue;
      long type = HistoryDealGetInteger(deal, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue;                                  // deposits, credits and the like are not trades
      string symbol = HistoryDealGetString(deal, DEAL_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      long entry = HistoryDealGetInteger(deal, DEAL_ENTRY);
      FileWriteString(handle, IntegerToString((long)deal) + ","
                      + TimeToString((datetime)HistoryDealGetInteger(deal, DEAL_TIME), TIME_DATE | TIME_SECONDS) + ","
                      + symbol + "," + (type == DEAL_TYPE_BUY ? "buy" : "sell") + ","
                      + (entry == DEAL_ENTRY_IN ? "in" : entry == DEAL_ENTRY_OUT ? "out" : "other") + ","
                      + DoubleToString(HistoryDealGetDouble(deal, DEAL_VOLUME), 2) + ","
                      + DoubleToString(HistoryDealGetDouble(deal, DEAL_PRICE), digits) + ","
                      + DoubleToString(HistoryDealGetDouble(deal, DEAL_PROFIT), 2) + ","
                      + DoubleToString(HistoryDealGetDouble(deal, DEAL_COMMISSION), 2) + ","
                      + DoubleToString(HistoryDealGetDouble(deal, DEAL_SWAP), 2) + ","
                      + IntegerToString(HistoryDealGetInteger(deal, DEAL_POSITION_ID)) + "\n");
     }
   FileClose(handle);
   FileMove("botbridge\\deals.tmp", FILE_COMMON, "botbridge\\deals.csv", FILE_COMMON | FILE_REWRITE);
  }

// The history files: at start, then every 6 hours; and again a minute later while the terminal is still
// loading more bars from the server (the count keeps growing).
void WriteHistory()
  {
   int rows = 0;
   for(int s = 0; s < ArraySize(g_symbols); s++)
      for(int t = 0; t < ArraySize(g_tfs); t++)
         rows += WriteBars(g_symbols[s], g_tfs[t], HistoryBars, "history");
   g_history_at = (rows > g_history_rows) ? TimeLocal() - 6 * 3600 + 60 : TimeLocal();
   g_history_rows = rows;
  }

int OnInit()
  {
   StringSplit(Symbols, ',', g_symbols);
   StringSplit(Timeframes, ',', g_tfs);
   Trim(g_symbols);
   Trim(g_tfs);
   for(int i = 0; i < ArraySize(g_symbols); i++)
      SymbolSelect(g_symbols[i], true);
   FolderCreate("botbridge", FILE_COMMON);
   EventSetTimer(MathMax(RefreshSeconds, 5));
   Print("BotBridge: writing to ", TerminalInfoString(TERMINAL_COMMONDATA_PATH), "\\Files\\botbridge");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   for(int s = 0; s < ArraySize(g_symbols); s++)
      for(int t = 0; t < ArraySize(g_tfs); t++)
         WriteBars(g_symbols[s], g_tfs[t], RecentBars, "recent");
   WriteQuotes();
   if(TimeLocal() - g_account_at >= 60)
     {
      g_account_at = TimeLocal();
      WriteAccount();
     }
   if(TimeLocal() - g_history_at >= 6 * 3600)
      WriteHistory();
  }

void OnTick()
  {
  }

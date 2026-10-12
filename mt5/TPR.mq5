//+------------------------------------------------------------------+
//|                                                          TPR.mq5 |
//+------------------------------------------------------------------+
#property indicator_chart_window
#property indicator_plots 0

int OnInit()
  {
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, "TPR_");
   ChartRedraw();
  }

int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
  {
   int    barsToScan = 300;
   double minRetrace = 0.382;
   color  colorP0    = clrGray;
   color  colorPA    = clrBlue;
   color  colorTP    = clrGreen;
   color  colorSL    = clrRed;

   if(rates_total < barsToScan + 1) return(0);

   // index 0 = the newest bar, as the loops below assume
   ArraySetAsSeries(time, true);
   ArraySetAsSeries(high, true);
   ArraySetAsSeries(low, true);

   ObjectsDeleteAll(0, "TPR_");

   // the highest high and the lowest low of the last barsToScan completed bars
   int maxHighBar = ArrayMaximum(high, 1, barsToScan);
   int minLowBar  = ArrayMinimum(low, 1, barsToScan);
   if(maxHighBar < 0 || minLowBar < 0) return(0);

   double maxHigh = high[maxHighBar];
   double minLow  = low[minLowBar];

   // the older extreme is Point 0: a low first means a bullish sequence
   bool isBullish = (minLowBar > maxHighBar);

   double p0 = 0.0, pA = 0.0;
   datetime t0 = 0, tA = 0;

   if(isBullish)
     {
      p0 = minLow;
      t0 = time[minLowBar];

      pA = p0;
      int pA_Bar = minLowBar;
      for(int i = minLowBar - 1; i >= 1; i--)
        {
         if(high[i] > pA) { pA = high[i]; pA_Bar = i; }
        }
      tA = time[pA_Bar];

      double impulseDistance = pA - p0;
      double minCorrectionPrice = pA - (impulseDistance * minRetrace);

      bool validCorrection = false;
      double currentCorrectionLow = pA;
      for(int i = pA_Bar - 1; i >= 0; i--)
        {
         if(low[i] <= minCorrectionPrice) validCorrection = true;
         if(low[i] < currentCorrectionLow) currentCorrectionLow = low[i];
        }

      if(impulseDistance > 0 && validCorrection && currentCorrectionLow > p0)
        {
         double tp1618 = p0 + (impulseDistance * 1.618);
         double slLevel = p0;

         CreateTextObject("TPR_P0", t0, p0, "Point 0", colorP0, ANCHOR_UPPER);
         CreateTextObject("TPR_PA", tA, pA, "Point A", colorPA, ANCHOR_LOWER);
         CreateTrendLine("TPR_TP_Line", tA, tp1618, time[0], tp1618, colorTP, STYLE_SOLID, 2);
         CreateTrendLine("TPR_SL_Line", tA, slLevel, time[0], slLevel, colorSL, STYLE_DASH, 1);
        }
     }
   else
     {
      p0 = maxHigh;
      t0 = time[maxHighBar];

      pA = p0;
      int pA_Bar = maxHighBar;
      for(int i = maxHighBar - 1; i >= 1; i--)
        {
         if(low[i] < pA) { pA = low[i]; pA_Bar = i; }
        }
      tA = time[pA_Bar];

      double impulseDistance = p0 - pA;
      double minCorrectionPrice = pA + (impulseDistance * minRetrace);

      bool validCorrection = false;
      double currentCorrectionHigh = pA;
      for(int i = pA_Bar - 1; i >= 0; i--)
        {
         if(high[i] >= minCorrectionPrice) validCorrection = true;
         if(high[i] > currentCorrectionHigh) currentCorrectionHigh = high[i];
        }

      if(impulseDistance > 0 && validCorrection && currentCorrectionHigh < p0)
        {
         double tp1618 = p0 - (impulseDistance * 1.618);
         double slLevel = p0;

         CreateTextObject("TPR_P0", t0, p0, "Point 0", colorP0, ANCHOR_LOWER);
         CreateTextObject("TPR_PA", tA, pA, "Point A", colorPA, ANCHOR_UPPER);
         CreateTrendLine("TPR_TP_Line", tA, tp1618, time[0], tp1618, colorTP, STYLE_SOLID, 2);
         CreateTrendLine("TPR_SL_Line", tA, slLevel, time[0], slLevel, colorSL, STYLE_DASH, 1);
        }
     }

   return(rates_total);
  }

void CreateTextObject(string name, datetime time1, double price1, string text, color clr, ENUM_ANCHOR_POINT anchor)
  {
   ObjectCreate(0, name, OBJ_TEXT, 0, time1, price1);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 10);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
  }

void CreateTrendLine(string name, datetime time1, double price1, datetime time2, double price2, color clr, ENUM_LINE_STYLE style, int width)
  {
   ObjectCreate(0, name, OBJ_TREND, 0, time1, price1, time2, price2);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
  }

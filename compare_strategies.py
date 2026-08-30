import sys
import pandas as pd
import numpy as np
import yfinance as yf
import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings("ignore")

if sys.platform == "win32":
    # Windows 콘솔 기본 인코딩(cp949)이 이모지를 못 그려 print()가 죽는 문제 방지
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
from strategies import get_strategy, list_strategies

CFG = {
    "start_date": "2022-01-01", "end_date": "2025-01-01",
    "initial_cash": 10_000_000, "max_positions": 5,
    "fee_rate": 0.0015, "usd_krw": 1350,
    "strategies": ["rsi", "ma_cross", "bollinger", "breakout", "macd"],
}
US_TICKERS = ["AAPL","MSFT","GOOGL","AMZN","NVDA","META","JPM","V","KO","JNJ"]
KR_TICKERS = ["005930","000660","035420","051910","005380","005490","035720","012330","068270","105560"]

def load_ohlcv(ticker, market):
    try:
        if market == "US":
            df = yf.download(ticker, start=CFG["start_date"], end=CFG["end_date"], progress=False, auto_adjust=True)
            if df.empty: return None
            df = df[["Open","High","Low","Close","Volume"]]; df.columns = ["open","high","low","close","volume"]
        else:
            df = fdr.DataReader(ticker, CFG["start_date"], CFG["end_date"])
            if df.empty: return None
            df = df[["Open","High","Low","Close","Volume"]]; df.columns = ["open","high","low","close","volume"]
        return df.dropna()
    except Exception: return None

def backtest_strategy(strat_name, data):
    strat = get_strategy(strat_name)
    cash = CFG["initial_cash"]; positions = {}; trades = []
    all_dates = sorted(set().union(*[set(d.index) for d,_ in data.values()]))
    def to_krw(mkt, px): return px*CFG["usd_krw"] if mkt=="US" else px
    equity_curve = []
    for date in all_dates:
        for t in list(positions.keys()):
            d, mkt = data[t]
            if date not in d.index: continue
            hist = d.loc[:date]
            if len(hist) < strat.required_days: continue
            sell, reason = strat.check_sell(hist, positions[t]["entry"])
            if sell:
                px = d.loc[date,"close"]
                cash += to_krw(mkt,px)*positions[t]["qty"]*(1-CFG["fee_rate"])
                trades.append((px-positions[t]["entry"])/positions[t]["entry"])
                del positions[t]
        if len(positions) < CFG["max_positions"]:
            for t,(d,mkt) in data.items():
                if t in positions or date not in d.index: continue
                hist = d.loc[:date]
                if len(hist) < strat.required_days: continue
                buy, reason = strat.check_buy(hist)
                if buy:
                    px = d.loc[date,"close"]; slots = CFG["max_positions"]-len(positions)
                    budget = cash*0.9/slots; px_krw = to_krw(mkt,px); qty = int(budget//px_krw)
                    if qty>=1 and px_krw*qty*(1+CFG["fee_rate"])<=cash:
                        cash -= px_krw*qty*(1+CFG["fee_rate"]); positions[t]={"qty":qty,"entry":px}
                    if len(positions)>=CFG["max_positions"]: break
        val = sum(to_krw(data[t][1], data[t][0].loc[date,"close"] if date in data[t][0].index else p["entry"])*p["qty"] for t,p in positions.items())
        equity_curve.append({"date":date,"equity":cash+val})
    return pd.DataFrame(equity_curve).set_index("date"), trades

def metrics(eq, trades):
    final = eq["equity"].iloc[-1]; total = (final/CFG["initial_cash"]-1)*100
    roll = eq["equity"].cummax(); mdd = ((eq["equity"]-roll)/roll).min()*100
    daily = eq["equity"].pct_change().dropna()
    sharpe = (daily.mean()/daily.std()*np.sqrt(252)) if daily.std()>0 else 0
    n = len(trades); wr = (sum(1 for r in trades if r>0)/n*100) if n else 0
    return {"total":total,"mdd":mdd,"sharpe":sharpe,"trades":n,"win_rate":wr}

def main():
    print("="*74)
    print("  전략 비교 백테스트")
    print(f"  기간 {CFG['start_date']} ~ {CFG['end_date']}  |  대형주 {len(US_TICKERS)+len(KR_TICKERS)}종목")
    print("="*74)
    print("\n데이터 로딩 중...")
    data = {}
    for t in US_TICKERS:
        d = load_ohlcv(t,"US");  data.update({t:(d,"US")} if d is not None else {})
    for t in KR_TICKERS:
        d = load_ohlcv(t,"KR");  data.update({t:(d,"KR")} if d is not None else {})
    if not data:
        print("\n데이터 로드 실패 - 인터넷 연결 확인"); return
    print(f"  {len(data)}종목 로드 완료")
    results = {}; curves = {}
    print("\n전략별 백테스트 실행 중...")
    for sname in CFG["strategies"]:
        print(f"  . {sname} ...", end=" ", flush=True)
        eq, trades = backtest_strategy(sname, data)
        results[sname] = metrics(eq, trades); curves[sname] = eq
        print(f"완료 (거래 {results[sname]['trades']}건)")
    print("\n"+"="*74)
    print(f"  {'전략':12} {'총수익률':>10} {'MDD':>9} {'샤프':>7} {'거래':>6} {'승률':>7}")
    print("-"*74)
    ranked = sorted(results.items(), key=lambda x:x[1]["sharpe"], reverse=True)
    for sname,m in ranked:
        print(f"  {sname:12} {m['total']:>9.1f}% {m['mdd']:>8.1f}% {m['sharpe']:>7.2f} {m['trades']:>6} {m['win_rate']:>6.1f}%")
    print("="*74)
    best = ranked[0][0]
    print(f"\n  이 기간 샤프지수 1위: {best}")
    print("  샤프지수 = 위험 대비 수익. 높을수록 효율적으로 번 것.")
    print("  단, 이 순위는 이 기간의 결과일 뿐. 여러 기간으로 돌려봐야 진짜 강한 전략이 보입니다.")
    plt.figure(figsize=(13,7))
    for sname,eq in curves.items():
        plt.plot(eq.index, eq["equity"], label=sname, linewidth=1.5, alpha=0.85)
    plt.axhline(CFG["initial_cash"], color="gray", ls="--", alpha=0.4, label="초기자본")
    plt.title("Strategy Comparison"); plt.ylabel("Equity (KRW)"); plt.legend(); plt.grid(alpha=0.3)
    plt.tight_layout(); plt.savefig("strategy_compare.png", dpi=120)
    print("\n  strategy_compare.png 저장됨"); print("="*74)

if __name__ == "__main__":
    main()

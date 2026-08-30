"""
==============================================================================
  대형주 멀티팩터 백테스트  (전문가 버전)   한국 + 미국
==============================================================================
  트레이더 관점에서 5가지 기법을 통합했습니다.

  [1] 멀티팩터 진입      : RSI 단독의 헛신호를 여러 필터로 제거
  [2] 추세 필터         : 하락추세 종목은 RSI 낮아도 매수 금지 ("떨어지는 칼날" 회피)
  [3] ATR 동적 손절     : 종목 변동성에 맞춰 손절폭 자동 조정 (고정 %의 한계 극복)
  [4] 켈리 포지션 사이징 : 확신도(손익비·승률)에 따라 투입 비중 최적화
  [5] 뉴스 필터 구조     : 실전 봇에서 악재 종목 제외 (백테스트는 미래정보 누출 방지 위해 OFF)

  ─ 위험 회피 원칙 유지 : 레버리지/인버스 없음, 대형주만, 분산 필수 ─

  사용법
    pip install yfinance finance-datareader pandas numpy matplotlib
    python backtest_pro.py

  산출물
    - 콘솔 성과 리포트 (기본전략 vs 개선전략 비교)
    - equity_curve_pro.png  수익곡선
    - trade_log_pro.csv     매매내역
==============================================================================
"""

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


# ═════════════════════════════════════════════════════════════════════════
#  설정
# ═════════════════════════════════════════════════════════════════════════
CFG = {
    "start_date": "2022-01-01",
    "end_date":   "2025-01-01",
    "initial_cash": 10_000_000,

    # ── RSI ──
    "rsi_period": 14,
    "rsi_buy_low": 30,
    "rsi_buy_high": 40,
    "rsi_overbought": 70,

    # ── [2] 추세 필터 ──
    "use_trend_filter": True,
    "ma_fast": 50,          # 단기 이동평균
    "ma_slow": 200,         # 장기 이동평균 (200일선 = 대세 판단 기준)

    # ── [4] 거래량 필터 ──
    "use_volume_filter": True,
    "vol_ma": 20,           # 20일 평균 거래량

    # ── [3] ATR 동적 손절/익절 ──
    "use_atr_stops": True,
    "atr_period": 14,
    "atr_stop_mult": 2.0,   # 손절 = 진입가 - 2.0*ATR
    "atr_profit_mult": 3.0, # 익절 = 진입가 + 3.0*ATR (손익비 1:1.5)
    # ATR 안 쓸 때 고정값 (fallback)
    "fixed_stop": -0.05,
    "fixed_profit": 0.08,

    # ── [4] 켈리 사이징 ──
    "use_kelly": True,
    "kelly_fraction": 0.25, # 1/4 켈리 (풀켈리는 너무 공격적이라 위험)
    "max_weight": 0.25,     # 한 종목 최대 비중 25% (분산 강제)
    "min_weight": 0.05,

    # ── [5] 뉴스 필터 ──
    "use_news_filter": False,  # 백테스트에선 OFF (실전 봇에서 ON)

    "max_positions": 5,
    "usd_krw": 1350,
    "fee_rate": 0.0015,     # 매매 수수료+세금 근사 (한국 기준 보수적)
}

US_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "JPM", "V", "KO", "JNJ", "WMT", "PG"]
KR_TICKERS = ["005930", "000660", "035420", "051910", "005380",
              "005490", "035720", "012330", "068270", "105560", "055550", "015760"]


# ═════════════════════════════════════════════════════════════════════════
#  지표 계산
# ═════════════════════════════════════════════════════════════════════════
def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, min_periods=period).mean()
    al = loss.ewm(alpha=1/period, min_periods=period).mean()
    return 100 - (100 / (1 + ag/al))

def calc_atr(high, low, close, period=14):
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period).mean()


# ═════════════════════════════════════════════════════════════════════════
#  데이터 로드 (OHLCV 전체)
# ═════════════════════════════════════════════════════════════════════════
def load_ohlcv(ticker, market):
    try:
        if market == "US":
            df = yf.download(ticker, start=CFG["start_date"], end=CFG["end_date"],
                             progress=False, auto_adjust=True)
            if df.empty: return None
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]
        else:
            df = fdr.DataReader(ticker, CFG["start_date"], CFG["end_date"])
            if df.empty: return None
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.columns = ["open", "high", "low", "close", "volume"]
        # 지표 부착
        df["rsi"] = calc_rsi(df["close"], CFG["rsi_period"])
        df["ma_fast"] = df["close"].rolling(CFG["ma_fast"]).mean()
        df["ma_slow"] = df["close"].rolling(CFG["ma_slow"]).mean()
        df["atr"] = calc_atr(df["high"], df["low"], df["close"], CFG["atr_period"])
        df["vol_ma"] = df["volume"].rolling(CFG["vol_ma"]).mean()
        return df.dropna()
    except Exception as e:
        print(f"  [{ticker}] 로드 실패: {str(e)[:60]}")
        return None


# ═════════════════════════════════════════════════════════════════════════
#  [5] 뉴스 필터 (실전 봇용 훅)
# ═════════════════════════════════════════════════════════════════════════
def news_sentiment_ok(ticker, date):
    """
    실전 봇에서 이 함수가 활성화됩니다.
    Claude API로 최근 뉴스를 분석해 악재(실적쇼크/소송/규제/회계문제)면 False.

    실전 구현 예시:
        news = fetch_recent_news(ticker)          # 뉴스 API
        prompt = f"다음 뉴스가 {ticker} 주가에 악재면 BAD, 아니면 OK만 답해: {news}"
        resp = claude_api(prompt)
        return "BAD" not in resp

    백테스트에서는 과거 뉴스를 그 시점 기준으로 정확히 복원하기 어렵고,
    잘못하면 '미래 정보 누출(look-ahead bias)'로 성과가 뻥튀기됩니다.
    그래서 백테스트에서는 항상 통과시킵니다.
    """
    if not CFG["use_news_filter"]:
        return True
    return True  # 실전에서 교체


# ═════════════════════════════════════════════════════════════════════════
#  [4] 켈리 포지션 사이징
# ═════════════════════════════════════════════════════════════════════════
def kelly_weight(win_rate, avg_win, avg_loss):
    if not CFG["use_kelly"] or avg_loss == 0 or win_rate == 0:
        return CFG["min_weight"]
    b = avg_win / abs(avg_loss)
    p, q = win_rate, 1 - win_rate
    k = (p - q / b) * CFG["kelly_fraction"]
    return float(np.clip(k, CFG["min_weight"], CFG["max_weight"]))


# ═════════════════════════════════════════════════════════════════════════
#  백테스트 엔진
# ═════════════════════════════════════════════════════════════════════════
def run(strategy="pro"):
    """strategy: 'basic'(RSI만) 또는 'pro'(5기법 전부)"""
    is_pro = (strategy == "pro")

    data = {}
    for t in US_TICKERS:
        d = load_ohlcv(t, "US")
        if d is not None: data[t] = (d, "US")
    for t in KR_TICKERS:
        d = load_ohlcv(t, "KR")
        if d is not None: data[t] = (d, "KR")
    if not data:
        return None, "데이터 로드 실패"

    all_dates = sorted(set().union(*[set(d.index) for d, _ in data.values()]))
    cash = CFG["initial_cash"]
    positions = {}
    trades = []
    equity = []

    # 켈리용 러닝 통계 (최근 거래 성과로 사이징 조정)
    recent_returns = []

    def to_krw(mkt, px):
        return px * CFG["usd_krw"] if mkt == "US" else px

    for date in all_dates:
        # ─── 매도 ───
        for t in list(positions.keys()):
            d, mkt = data[t]
            if date not in d.index:
                continue
            row = d.loc[date]
            px = row["close"]
            pos = positions[t]
            ret = (px - pos["entry"]) / pos["entry"]

            reason = None
            if px <= pos["stop"]:
                reason = "손절"
            elif px >= pos["target"]:
                reason = "익절"
            elif row["rsi"] >= CFG["rsi_overbought"]:
                reason = "익절(RSI70)"

            if reason:
                proceeds = to_krw(mkt, px) * pos["qty"] * (1 - CFG["fee_rate"])
                cash += proceeds
                recent_returns.append(ret)
                if len(recent_returns) > 30:
                    recent_returns.pop(0)
                trades.append({"date": date, "ticker": t, "action": "SELL",
                               "price": round(px, 2), "return_%": round(ret*100, 2),
                               "reason": reason})
                del positions[t]

        # ─── 매수 ───
        if len(positions) < CFG["max_positions"]:
            cands = []
            for t, (d, mkt) in data.items():
                if t in positions or date not in d.index:
                    continue
                row = d.loc[date]
                if pd.isna(row["rsi"]):
                    continue

                # [1] RSI 조건 (공통)
                if not (CFG["rsi_buy_low"] <= row["rsi"] <= CFG["rsi_buy_high"]):
                    continue

                if is_pro:
                    # [2] 추세 필터: 상승추세만 (하락추세 칼날 회피)
                    if CFG["use_trend_filter"]:
                        uptrend = (row["ma_fast"] > row["ma_slow"]) and (px_ok := row["close"] > row["ma_slow"])
                        if not uptrend:
                            continue
                    # [4] 거래량 필터: 관심 몰린 종목만
                    if CFG["use_volume_filter"] and row["volume"] < row["vol_ma"]:
                        continue
                    # [5] 뉴스 필터
                    if not news_sentiment_ok(t, date):
                        continue

                cands.append((t, row["rsi"]))

            cands.sort(key=lambda x: x[1])  # 더 과매도인 것 우선
            slots = CFG["max_positions"] - len(positions)

            # 켈리 비중 계산 (pro만)
            if is_pro and len(recent_returns) >= 5:
                wins = [r for r in recent_returns if r > 0]
                wr = len(wins) / len(recent_returns)
                aw = np.mean(wins) if wins else 0.05
                losses = [r for r in recent_returns if r <= 0]
                al = np.mean(losses) if losses else -0.05
                w = kelly_weight(wr, aw, al)
            else:
                w = 1.0 / CFG["max_positions"]

            for t, rsi_v in cands[:slots]:
                d, mkt = data[t]
                row = d.loc[date]
                px = row["close"]
                px_krw = to_krw(mkt, px)

                budget = cash * w if is_pro else cash / slots
                qty = int(budget // px_krw)
                if qty < 1:
                    continue
                cost = px_krw * qty * (1 + CFG["fee_rate"])
                if cost > cash:
                    continue
                cash -= cost

                # [3] ATR 동적 손절/익절
                if is_pro and CFG["use_atr_stops"] and not pd.isna(row["atr"]):
                    stop = px - CFG["atr_stop_mult"] * row["atr"]
                    target = px + CFG["atr_profit_mult"] * row["atr"]
                else:
                    stop = px * (1 + CFG["fixed_stop"])
                    target = px * (1 + CFG["fixed_profit"])

                positions[t] = {"qty": qty, "entry": px, "stop": stop, "target": target}
                trades.append({"date": date, "ticker": t, "action": "BUY",
                               "price": round(px, 2), "return_%": None,
                               "reason": f"RSI {rsi_v:.1f}"})

        # ─── 평가 ───
        val = 0
        for t, pos in positions.items():
            d, mkt = data[t]
            px = d.loc[date, "close"] if date in d.index else pos["entry"]
            val += to_krw(mkt, px) * pos["qty"]
        equity.append({"date": date, "equity": cash + val})

    return pd.DataFrame(equity).set_index("date"), pd.DataFrame(trades)


# ═════════════════════════════════════════════════════════════════════════
#  성과 지표
# ═════════════════════════════════════════════════════════════════════════
def metrics(eq, trades, label):
    final = eq["equity"].iloc[-1]
    total = (final / CFG["initial_cash"] - 1) * 100
    roll = eq["equity"].cummax()
    mdd = ((eq["equity"] - roll) / roll).min() * 100

    days = (eq.index[-1] - eq.index[0]).days
    years = days / 365.25
    cagr = ((final / CFG["initial_cash"]) ** (1/years) - 1) * 100 if years > 0 else 0

    # 월평균 수익률
    try:
        monthly = eq["equity"].resample("ME").last().pct_change().dropna()
    except ValueError:
        monthly = eq["equity"].resample("M").last().pct_change().dropna()
    avg_month = monthly.mean() * 100
    # 샤프 (연율화, 무위험 0 가정)
    daily = eq["equity"].pct_change().dropna()
    sharpe = (daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0

    sells = trades[trades["action"] == "SELL"] if not trades.empty else pd.DataFrame()
    n = len(sells)
    if n:
        wins = sells[sells["return_%"] > 0]
        wr = len(wins) / n * 100
        aw = wins["return_%"].mean() if len(wins) else 0
        ls = sells[sells["return_%"] <= 0]
        al = ls["return_%"].mean() if len(ls) else 0
    else:
        wr = aw = al = 0

    print(f"\n{'─'*66}")
    print(f"  [{label}]")
    print(f"{'─'*66}")
    print(f"  총 수익률        : {total:>12.2f} %")
    print(f"  연환산 CAGR      : {cagr:>12.2f} %")
    print(f"  월평균 수익률    : {avg_month:>12.2f} %   ← '월 10%' 목표와 비교")
    print(f"  최대낙폭 MDD     : {mdd:>12.2f} %   ← 위험 지표 (작을수록 좋음)")
    print(f"  샤프지수         : {sharpe:>12.2f}     ← 1 넘으면 양호, 2 넘으면 우수")
    print(f"  거래수 / 승률    : {n:>6} 건 / {wr:.1f} %")
    print(f"  평균익절/평균손절: {aw:>6.2f}% / {al:.2f}%")
    return {"label": label, "total": total, "cagr": cagr, "mdd": mdd,
            "sharpe": sharpe, "avg_month": avg_month, "eq": eq}


# ═════════════════════════════════════════════════════════════════════════
def main():
    print("=" * 66)
    print("  대형주 멀티팩터 백테스트 (전문가 버전)")
    print(f"  기간 {CFG['start_date']} ~ {CFG['end_date']}")
    print("=" * 66)

    print("\n[기본 전략] RSI 30~40 단독 (비교 기준)")
    eq_b, tr_b = run("basic")
    if eq_b is None:
        print("\n❌ 데이터 로드 실패 — 인터넷 연결 확인")
        return
    m_basic = metrics(eq_b, tr_b, "기본: RSI만")

    print("\n\n[개선 전략] 5기법 통합 (추세+거래량+ATR+켈리)")
    eq_p, tr_p = run("pro")
    m_pro = metrics(eq_p, tr_p, "개선: 멀티팩터")

    # 비교 그래프
    plt.figure(figsize=(13, 7))
    plt.plot(m_basic["eq"].index, m_basic["eq"]["equity"], label="기본 (RSI만)", alpha=0.7)
    plt.plot(m_pro["eq"].index, m_pro["eq"]["equity"], label="개선 (멀티팩터)", linewidth=2)
    plt.axhline(CFG["initial_cash"], color="gray", ls="--", alpha=0.4)
    plt.title("Basic vs Pro Strategy")
    plt.ylabel("Equity (KRW)")
    plt.legend(); plt.grid(alpha=0.3); plt.tight_layout()
    plt.savefig("equity_curve_pro.png", dpi=120)
    if not tr_p.empty:
        tr_p.to_csv("trade_log_pro.csv", index=False, encoding="utf-8-sig")

    print(f"\n\n{'='*66}")
    print("  결론")
    print(f"{'='*66}")
    print(f"  MDD 개선   : {m_basic['mdd']:.1f}% → {m_pro['mdd']:.1f}%  (0에 가까울수록 안전)")
    print(f"  샤프 개선  : {m_basic['sharpe']:.2f} → {m_pro['sharpe']:.2f}  (높을수록 효율적)")
    print(f"  월평균     : 개선전략 {m_pro['avg_month']:.2f}%")
    print()
    if m_pro["avg_month"] < 3:
        print("  ※ 월평균이 목표(10%)보다 낮아도 정상입니다. 위험회피형은")
        print("    '잃지 않고 꾸준히'가 핵심이고, MDD·샤프가 좋으면 성공한 전략입니다.")
    print("  📈 equity_curve_pro.png / 📋 trade_log_pro.csv 저장됨")
    print("=" * 66)


if __name__ == "__main__":
    main()

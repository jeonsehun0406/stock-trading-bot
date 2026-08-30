"""
==============================================================================
  매매 전략 모듈  (플러그인 방식)
==============================================================================
  봇 본체는 그대로 두고, 전략만 갈아끼우는 구조입니다.

  모든 전략은 같은 인터페이스를 따릅니다:
    - required_days : 지표 계산에 필요한 최소 일봉 수
    - check_buy(df)  -> (매수여부, 사유)      # 오늘 이 종목 사야 하나?
    - check_sell(df, entry_price) -> (매도여부, 사유)  # 팔아야 하나?

  df 는 최소 required_days 길이의 일봉 DataFrame (컬럼: open/high/low/close/volume)

  ---------------------------------------------------------------------------
  전략 목록
    [1] RSIStrategy       : 과매도 저가매수 (횡보,눌림목)     위험회피형
    [2] MACrossStrategy   : 이동평균 골든크로스 (상승추세)    추세추종
    [3] BollingerStrategy : 볼린저밴드 하단 반등 (변동성 횡보) 평균회귀
    [4] BreakoutStrategy  : N일 신고가 돌파 (강한 상승 시작)  공격적
    [5] MACDStrategy      : MACD 골든크로스 (추세 전환점)     중립

  사용:
    from strategies import get_strategy
    strat = get_strategy("rsi")
    buy, reason = strat.check_buy(df)
==============================================================================
"""

import pandas as pd
import numpy as np


# 공통 지표 함수 ------------------------------------------------------------
def calc_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1/period, min_periods=period).mean()
    al = loss.ewm(alpha=1/period, min_periods=period).mean()
    return 100 - (100 / (1 + ag / al))


def calc_atr(high, low, close, period=14):
    tr = pd.concat([high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period).mean()


def calc_bollinger(close, period=20, num_std=2):
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return ma - num_std * sd, ma, ma + num_std * sd


def calc_macd(close, fast=12, slow=26, signal=9):
    ema_f = close.ewm(span=fast).mean()
    ema_s = close.ewm(span=slow).mean()
    macd_line = ema_f - ema_s
    signal_line = macd_line.ewm(span=signal).mean()
    return macd_line, signal_line


# 전략 베이스 ---------------------------------------------------------------
class BaseStrategy:
    name = "base"
    description = ""
    required_days = 200

    # 공통 청산 규칙 (ATR 동적 손절/익절). 전략별로 덮어쓸 수 있음.
    atr_stop_mult = 2.0
    atr_profit_mult = 3.0
    rsi_exit = 70

    def check_buy(self, df):
        raise NotImplementedError

    def check_sell(self, df, entry_price):
        """공통 청산: ATR 손절/익절 + RSI 과매수."""
        close = df["close"]
        price = close.iloc[-1]
        atr = calc_atr(df["high"], df["low"], close).iloc[-1]
        rsi = calc_rsi(close).iloc[-1]

        stop = entry_price - self.atr_stop_mult * atr
        target = entry_price + self.atr_profit_mult * atr

        if price <= stop:
            return True, "손절"
        if price >= target:
            return True, "익절"
        if not np.isnan(rsi) and rsi >= self.rsi_exit:
            return True, f"익절(RSI{self.rsi_exit})"
        return False, None


# [1] RSI 저가매수 ----------------------------------------------------------
class RSIStrategy(BaseStrategy):
    name = "rsi"
    description = "RSI 과매도 저가매수 (횡보,눌림목). 위험회피형."
    required_days = 200
    rsi_low = 30
    rsi_high = 40
    ma_fast = 50
    ma_slow = 200

    def check_buy(self, df):
        close = df["close"]
        rsi = calc_rsi(close).iloc[-1]
        if np.isnan(rsi):
            return False, None
        if not (self.rsi_low <= rsi <= self.rsi_high):
            return False, None
        ma_f = close.rolling(self.ma_fast).mean().iloc[-1]
        ma_s = close.rolling(self.ma_slow).mean().iloc[-1]
        if not (ma_f > ma_s and close.iloc[-1] > ma_s):
            return False, None
        return True, f"RSI {rsi:.1f}"


# [2] 이동평균 골든크로스 ---------------------------------------------------
class MACrossStrategy(BaseStrategy):
    name = "ma_cross"
    description = "단기선이 장기선을 상향 돌파(골든크로스). 상승추세 진입."
    required_days = 120
    ma_fast = 20
    ma_slow = 60

    def check_buy(self, df):
        close = df["close"]
        ma_f = close.rolling(self.ma_fast).mean()
        ma_s = close.rolling(self.ma_slow).mean()
        if np.isnan(ma_s.iloc[-1]):
            return False, None
        crossed = (ma_f.iloc[-1] > ma_s.iloc[-1]) and (ma_f.iloc[-2] <= ma_s.iloc[-2])
        if crossed:
            return True, f"골든크로스 {self.ma_fast}/{self.ma_slow}"
        return False, None


# [3] 볼린저밴드 하단 반등 --------------------------------------------------
class BollingerStrategy(BaseStrategy):
    name = "bollinger"
    description = "볼린저 하단 이탈 후 복귀(반등 시작). 변동성 횡보장."
    required_days = 60
    period = 20
    num_std = 2

    def check_buy(self, df):
        close = df["close"]
        lower, mid, upper = calc_bollinger(close, self.period, self.num_std)
        if np.isnan(lower.iloc[-1]):
            return False, None
        rebounded = (close.iloc[-2] < lower.iloc[-2]) and (close.iloc[-1] >= lower.iloc[-1])
        if rebounded:
            return True, "볼린저 하단반등"
        return False, None

    def check_sell(self, df, entry_price):
        close = df["close"]
        lower, mid, upper = calc_bollinger(close, self.period, self.num_std)
        price = close.iloc[-1]
        if not np.isnan(mid.iloc[-1]) and price >= mid.iloc[-1] and price > entry_price:
            return True, "익절(중간선복귀)"
        return super().check_sell(df, entry_price)


# [4] 돌파매매 --------------------------------------------------------------
class BreakoutStrategy(BaseStrategy):
    name = "breakout"
    description = "N일 신고가 돌파(강한 상승 시작). 공격적, 추세 초입 포착."
    required_days = 60
    lookback = 20
    volume_surge = 1.5

    def check_buy(self, df):
        close = df["close"]
        high = df["high"]
        volume = df["volume"]
        if len(df) < self.lookback + 1:
            return False, None
        prior_high = high.iloc[-(self.lookback+1):-1].max()
        price = close.iloc[-1]
        vol_ma = volume.rolling(self.lookback).mean().iloc[-1]
        vol_ok = volume.iloc[-1] >= vol_ma * self.volume_surge
        if price > prior_high and vol_ok:
            return True, f"{self.lookback}일 신고가 돌파"
        return False, None


# [5] MACD 골든크로스 -------------------------------------------------------
class MACDStrategy(BaseStrategy):
    name = "macd"
    description = "MACD선이 시그널선 상향 돌파(추세 전환). 중립."
    required_days = 80

    def check_buy(self, df):
        close = df["close"]
        macd_line, signal_line = calc_macd(close)
        if np.isnan(signal_line.iloc[-1]):
            return False, None
        crossed = (macd_line.iloc[-1] > signal_line.iloc[-1]) and \
                  (macd_line.iloc[-2] <= signal_line.iloc[-2])
        if crossed and macd_line.iloc[-1] < 0:
            return True, "MACD 골든크로스(바닥권)"
        elif crossed:
            return True, "MACD 골든크로스"
        return False, None


# 레지스트리 ----------------------------------------------------------------
_STRATEGIES = {
    "rsi": RSIStrategy,
    "ma_cross": MACrossStrategy,
    "bollinger": BollingerStrategy,
    "breakout": BreakoutStrategy,
    "macd": MACDStrategy,
}


def get_strategy(name):
    if name not in _STRATEGIES:
        raise ValueError(f"모르는 전략: {name}. 가능: {list(_STRATEGIES)}")
    return _STRATEGIES[name]()


def list_strategies():
    return [(n, cls.description) for n, cls in _STRATEGIES.items()]


if __name__ == "__main__":
    print("=" * 66)
    print("  사용 가능한 매매 전략")
    print("=" * 66)
    for name, desc in list_strategies():
        print(f"  [{name:10}] {desc}")
    print("=" * 66)
    print("  봇에서 사용: get_strategy('rsi') 처럼 이름으로 선택")

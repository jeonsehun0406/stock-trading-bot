"""
==============================================================================
  유니버스(거래대상) 스크리닝 — 오프라인 도구
==============================================================================
  "대형주만" 원칙(미국 시총 100억달러↑ / 한국 시총 1조원↑ + 유동성 하한)에 맞춰
  실제 상장종목 전체를 스크리닝하고 결과를 universe.json에 저장합니다.

  ⚠️ 이 스크립트는 trading_bot.py가 자동으로 실행하지 않습니다.
     장중 스캔마다 유니버스를 새로 뽑으면 무인 봇의 매매 대상이 예측 불가능하게
     흔들리고, 매번 503개 종목을 조회하는 건 느리고 API 부담도 큽니다.
     대신 사람이 의도적으로 주기적(예: 월 1회)으로 재실행하세요.

  사용법
    pip install finance-datareader yfinance pandas
    python universe.py

  산출물
    universe.json  — {"generated_at", "kr": [...], "us": [...]}
    trading_bot.py는 시작 시 이 파일이 있으면 읽어서 한국 유니버스로 사용하고,
    없으면 하드코딩된 기본 10종목으로 안전하게 폴백합니다.
==============================================================================
"""

import sys
import re
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32":
    # Windows 콘솔 기본 인코딩(cp949)이 한글/이모지를 못 그려 print()가 죽는 문제 방지
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import FinanceDataReader as fdr
import yfinance as yf


# 종목명 끝이 "우"/"N우"/"N우B"/"우(전환)" 형태면 우선주로 추정 (완벽하진 않지만 상식적인 선의 필터)
PREFERRED_STOCK_PATTERN = re.compile(r".+\d*우(\([^)]*\))?[A-Z]?$")


# ═════════════════════════════════════════════════════════════════════════
#  한국 — FinanceDataReader KRX 전종목에서 바로 필터링
# ═════════════════════════════════════════════════════════════════════════
def screen_kr_universe(min_market_cap=1_000_000_000_000, min_avg_amount=10_000_000_000):
    """한국 대형주 스크리닝. 시가총액(min_market_cap) + 거래대금(min_avg_amount, 유동성) 기준.

    반환: [{"code", "name", "market_cap", "amount"}, ...] 시가총액 내림차순
    """
    print("[한국] FinanceDataReader로 KRX 전종목 조회 중...")
    df = fdr.StockListing("KRX")
    print(f"[한국] 전체 {len(df)}종목 조회 완료")

    df = df[(df["Marcap"] >= min_market_cap) & (df["Amount"] >= min_avg_amount)]
    print(
        f"[한국] 시총 {min_market_cap/1e8:,.0f}억원 이상 + "
        f"거래대금 {min_avg_amount/1e8:,.0f}억원 이상: {len(df)}종목"
    )

    # 우선주 제외 (간단한 이름 패턴 — 과도하게 정밀하게 만들지 않음)
    is_preferred = df["Name"].apply(lambda n: bool(PREFERRED_STOCK_PATTERN.match(str(n))))
    if is_preferred.sum() > 0:
        print(f"[한국] 우선주로 추정되는 {is_preferred.sum()}종목 제외")
    df = df[~is_preferred]

    # 스팩(SPAC) 제외
    is_spac = df["Name"].str.contains("스팩", na=False)
    if is_spac.sum() > 0:
        print(f"[한국] 스팩 {is_spac.sum()}종목 제외")
    df = df[~is_spac]

    df = df.sort_values("Marcap", ascending=False)

    result = [
        {
            "code": str(row["Code"]),
            "name": str(row["Name"]),
            "market_cap": int(row["Marcap"]),
            "amount": int(row["Amount"]),
        }
        for _, row in df.iterrows()
    ]
    print(f"[한국] 최종 통과: {len(result)}종목")
    return result


# ═════════════════════════════════════════════════════════════════════════
#  미국 — S&P500을 후보군으로 삼고 yfinance로 개별 시총/거래량 조회
# ═════════════════════════════════════════════════════════════════════════
def _fetch_us_stock(symbol, name, sector):
    """개별 종목 시총/평균거래량 조회. 실패해도 예외를 밖으로 던지지 않고 None 반환
    (하나 실패했다고 전체 스크리닝이 죽으면 안 되므로)."""
    try:
        fast_info = yf.Ticker(symbol).fast_info
        market_cap = fast_info.market_cap
        avg_volume = fast_info.three_month_average_volume
        if market_cap is None or avg_volume is None:
            return None
        return {
            "symbol": symbol,
            "name": name,
            "market_cap": int(market_cap),
            "avg_volume": int(avg_volume),
            "sector": sector,
        }
    except Exception:
        return None


def screen_us_universe(min_market_cap_usd=10_000_000_000, min_avg_volume=1_000_000, max_workers=10):
    """미국 대형주 스크리닝. S&P500 편입종목을 후보군으로 yfinance에서 시총/평균거래량 조회.

    반환: [{"symbol", "name", "market_cap", "sector"}, ...] 시가총액 내림차순
    503개 종목을 순차 조회하면 느리므로 스레드풀로 병렬 조회 (개별 실패는 건너뜀).
    """
    print("[미국] FinanceDataReader로 S&P500 후보군 조회 중...")
    candidates = fdr.StockListing("S&P500")
    total = len(candidates)
    print(f"[미국] 후보 {total}종목 — yfinance로 개별 시총/거래량 조회 시작 (병렬 {max_workers}개, 시간이 걸릴 수 있습니다)")

    passed = []
    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_us_stock, row.Symbol, row.Name, getattr(row, "Sector", "")): row.Symbol
            for row in candidates.itertuples(index=False)
        }
        for future in as_completed(futures):
            done += 1
            info = future.result()
            if info is None:
                failed += 1
            elif info["market_cap"] >= min_market_cap_usd and info["avg_volume"] >= min_avg_volume:
                passed.append(info)
            if done % 50 == 0 or done == total:
                print(f"[미국] 진행 {done}/{total} (통과 {len(passed)}, 조회실패/제외 {failed})")

    for item in passed:
        item.pop("avg_volume", None)  # 저장 스키마는 요구사항대로 market_cap/sector만 유지
    passed.sort(key=lambda x: x["market_cap"], reverse=True)
    print(f"[미국] 최종 통과: {len(passed)}종목")
    return passed


# ═════════════════════════════════════════════════════════════════════════
#  저장
# ═════════════════════════════════════════════════════════════════════════
def save_universe(kr_list, us_list, path="universe.json"):
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "kr": kr_list,
        "us": us_list,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"저장 완료 → {path}")
    return data


if __name__ == "__main__":
    kr = screen_kr_universe()
    us = screen_us_universe()

    print("\n" + "=" * 60)
    print(f"결과 요약: 한국 {len(kr)}종목 / 미국 {len(us)}종목 통과")
    print("=" * 60)

    print("\n[한국] 상위 10종목 미리보기")
    for item in kr[:10]:
        print(f"  {item['code']} {item['name']:<12} 시총 {item['market_cap']/1e8:,.0f}억원")

    print("\n[미국] 상위 10종목 미리보기")
    for item in us[:10]:
        print(f"  {item['symbol']:<6} {item['name']:<30} 시총 ${item['market_cap']/1e9:,.1f}B")

    save_universe(kr, us)

"""
==============================================================================
  매매 알림 모듈  (카카오톡 + 텔레그램)
==============================================================================
  자동매매 봇이 매수/매도할 때마다 손안의 폰으로 실시간 리포트를 보냅니다.
  매수/매도 순간마다 → 개별종목 + 수익률 + 현금 + 총평가금액이 전부 찍힘.

  두 채널 다 지원:
    - 텔레그램 : 토큰 만료 없음. 설정 간단. (추천)
    - 카카오톡 : 한국인 친숙. 단 토큰 만료 있음 → 자동갱신 코드 포함.

  ─────────────────────────────────────────────────────────────────────────
  최초 설정 (한 번만)
  ─────────────────────────────────────────────────────────────────────────
  [텔레그램]
    1. 카톡 아닌 텔레그램 앱에서 @BotFather 검색 → /newbot → 봇 토큰 받기
    2. 만든 봇과 아무 메시지나 1:1 대화 시작
    3. https://api.telegram.org/bot<토큰>/getUpdates 접속 → chat.id 확인
    4. 아래 CONFIG에 bot_token, chat_id 입력

  [카카오톡]
    1. developers.kakao.com → 애플리케이션 추가 → REST API 키 확인
    2. 카카오 로그인 활성화, redirect_uri 등록 (예: https://localhost)
    3. scope=talk_message 로 인가코드 발급 → 최초 토큰 발급
    4. 발급된 access_token / refresh_token 을 kakao_token.json 에 저장
       (아래 get_kakao_token_first_time() 안내 참고)
  ─────────────────────────────────────────────────────────────────────────
"""

import requests
import json
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ═════════════════════════════════════════════════════════════════════════
# .env에 값이 있으면 그걸 쓰고, 없으면 아래 기본값(직접 수정용)을 씁니다.
CONFIG = {
    "channel": "telegram",       # "telegram" 또는 "kakao" 또는 "both"

    # ── 텔레그램 ──
    "telegram": {
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "여기에_봇토큰"),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", "여기에_chat_id"),
    },

    # ── 카카오톡 ──
    "kakao": {
        "rest_api_key": os.getenv("KAKAO_REST_API_KEY", "여기에_REST_API_키"),
        "token_file": "kakao_token.json",   # 토큰 저장 파일
    },
}
# ═════════════════════════════════════════════════════════════════════════


def format_won(amount):
    return f"{amount:,.0f}원"


# ─────────────────────────────────────────────────────────────────────────
#  메시지 빌더  (후니님 요청 포맷)
# ─────────────────────────────────────────────────────────────────────────
def build_buy_message(ticker, buy_amount, reason, portfolio, cash):
    """
    portfolio: {티커: {"value": 평가금액}}  현재 보유 종목들
    cash: 현금 잔고
    reason: 전략 매수 사유 (예: "RSI 34.2", "골든크로스 20/60")
    """
    total = sum(p["value"] for p in portfolio.values()) + cash
    lines = ["🟢 매수 체결",
             f"{ticker}  {format_won(buy_amount)} ({reason})",
             "─────────────", "📊 포트폴리오"]
    for t, p in portfolio.items():
        pct = p["value"] / total * 100 if total else 0
        lines.append(f" {t}  {format_won(p['value'])}  ({pct:.0f}%)")
    lines.append(f" 현금  {format_won(cash)}  ({cash/total*100:.0f}%)" if total else "")
    lines.append("─────────────")
    lines.append(f" 총 평가  {format_won(total)}")
    return "\n".join(l for l in lines if l)


def build_sell_message(ticker, ret_pct, profit_won, reason, portfolio, cash, initial):
    total = sum(p["value"] for p in portfolio.values()) + cash
    total_ret = (total / initial - 1) * 100 if initial else 0
    emoji = "📈" if ret_pct >= 0 else "📉"
    lines = ["🔴 매도 체결",
             f"{ticker} 매도 | 수익률 {ret_pct:+.2f}% ({profit_won:+,.0f}원) {emoji}",
             f"사유: {reason}",
             "─────────────", "📊 포트폴리오"]
    if portfolio:
        for t, p in portfolio.items():
            pct = p["value"] / total * 100 if total else 0
            lines.append(f" {t}  {format_won(p['value'])}  ({pct:.0f}%)")
    else:
        lines.append(" (보유 종목 없음 · 전량 현금)")
    lines.append(f" 현금  {format_won(cash)}  ({cash/total*100:.0f}%)" if total else "")
    lines.append("─────────────")
    lines.append(f" 총 평가  {format_won(total)} ({total_ret:+.2f}%)")
    return "\n".join(l for l in lines if l)


def build_daily_summary(date, kr_buy, kr_sell, kr_pnl, us_buy, us_sell, us_pnl,
                         show_us, portfolio, cash, initial):
    """하루 마감 요약 (국내/해외 구분, 저녁 8시 이후 하루 1번만 호출됨)"""
    total = sum(p["value"] for p in portfolio.values()) + cash
    total_ret = (total / initial - 1) * 100 if initial else 0
    lines = [f"📅 {date} 일일 요약",
             "─────────────",
             "🇰🇷 국내",
             f" 매수 {kr_buy}건 · 매도 {kr_sell}건 · 실현손익 {kr_pnl:+,.0f}원"]
    if show_us:
        lines += ["🇺🇸 해외 (원화환산)",
                  f" 매수 {us_buy}건 · 매도 {us_sell}건 · 실현손익 {us_pnl:+,.0f}원"]
    lines += ["─────────────",
              f" 보유 종목  {len(portfolio)}개",
              f" 현금  {format_won(cash)}",
              f" 총 평가  {format_won(total)}",
              f" 누적 수익률  {total_ret:+.2f}%"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
#  텔레그램 전송
# ─────────────────────────────────────────────────────────────────────────
def send_telegram(text):
    tg = CONFIG["telegram"]
    url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": tg["chat_id"], "text": text}, timeout=10)
        if r.status_code == 200:
            return True
        print(f"[텔레그램 실패] {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────
#  카카오톡 전송 (+ 토큰 자동 갱신)
# ─────────────────────────────────────────────────────────────────────────
def _load_kakao_token():
    path = CONFIG["kakao"]["token_file"]
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _save_kakao_token(tok):
    with open(CONFIG["kakao"]["token_file"], "w") as f:
        json.dump(tok, f)


def _refresh_kakao_token():
    """리프레시 토큰으로 액세스 토큰 갱신. 이게 있어야 알림이 안 끊깁니다."""
    tok = _load_kakao_token()
    if not tok or "refresh_token" not in tok:
        print("[카카오] 토큰 파일 없음 — 최초 발급 필요")
        return None
    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": CONFIG["kakao"]["rest_api_key"],
        "refresh_token": tok["refresh_token"],
    }
    try:
        r = requests.post(url, data=data, timeout=10)
        new = r.json()
        if "access_token" in new:
            tok["access_token"] = new["access_token"]
            # 리프레시 토큰도 새로 오면 갱신 (안 오면 기존 유지)
            if "refresh_token" in new:
                tok["refresh_token"] = new["refresh_token"]
            _save_kakao_token(tok)
            return tok["access_token"]
        print(f"[카카오 갱신 실패] {new}")
        return None
    except Exception as e:
        print(f"[카카오 갱신 오류] {e}")
        return None


def send_kakao(text, _retry=True):
    tok = _load_kakao_token()
    if not tok:
        print("[카카오] 토큰 없음 — 최초 설정 필요")
        return False
    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": "https://www.kakao.com"},
    }
    try:
        r = requests.post(url, headers=headers,
                          data={"template_object": json.dumps(template)}, timeout=10)
        if r.status_code == 200:
            return True
        # 401 = 토큰 만료 → 자동 갱신 후 1회 재시도
        if r.status_code == 401 and _retry:
            print("[카카오] 토큰 만료 감지 → 자동 갱신 시도")
            if _refresh_kakao_token():
                return send_kakao(text, _retry=False)
        print(f"[카카오 실패] {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        print(f"[카카오 오류] {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────
#  통합 전송 (봇에서 이 함수만 호출하면 됨)
# ─────────────────────────────────────────────────────────────────────────
def notify(text):
    ch = CONFIG["channel"]
    ok = False
    if ch in ("telegram", "both"):
        ok = send_telegram(text) or ok
    if ch in ("kakao", "both"):
        ok = send_kakao(text) or ok
    return ok


# 봇에서 쓰는 고수준 함수들 ────────────────────────────────────────────────
def notify_buy(ticker, buy_amount, reason, portfolio, cash):
    return notify(build_buy_message(ticker, buy_amount, reason, portfolio, cash))

def notify_sell(ticker, ret_pct, profit_won, reason, portfolio, cash, initial):
    return notify(build_sell_message(ticker, ret_pct, profit_won, reason,
                                     portfolio, cash, initial))

def notify_daily(date, kr_buy, kr_sell, kr_pnl, us_buy, us_sell, us_pnl,
                  show_us, portfolio, cash, initial):
    return notify(build_daily_summary(date, kr_buy, kr_sell, kr_pnl, us_buy, us_sell, us_pnl,
                                      show_us, portfolio, cash, initial))


# ─────────────────────────────────────────────────────────────────────────
#  카카오 최초 토큰 발급 도우미 (한 번만 실행)
# ─────────────────────────────────────────────────────────────────────────
def get_kakao_token_first_time():
    """
    최초 1회만 실행. 인가코드로 access/refresh 토큰을 발급받아 파일로 저장.
    이후엔 send_kakao가 알아서 갱신하므로 다시 안 해도 됩니다.
    """
    key = CONFIG["kakao"]["rest_api_key"]
    redirect = "https://localhost"
    auth_url = (f"https://kauth.kakao.com/oauth/authorize?"
                f"client_id={key}&redirect_uri={redirect}"
                f"&response_type=code&scope=talk_message")
    print("1) 아래 URL을 브라우저에서 열고 로그인/동의하세요:\n", auth_url)
    print("2) 리다이렉트된 주소의 code= 뒤 값을 복사하세요.")
    code = input("인가코드 입력: ").strip()
    r = requests.post("https://kauth.kakao.com/oauth/token", data={
        "grant_type": "authorization_code",
        "client_id": key,
        "redirect_uri": redirect,
        "code": code,
    })
    tok = r.json()
    if "access_token" in tok:
        _save_kakao_token(tok)
        print("✓ 토큰 저장 완료 →", CONFIG["kakao"]["token_file"])
    else:
        print("✗ 발급 실패:", tok)


# ─────────────────────────────────────────────────────────────────────────
#  테스트 실행
# ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 실제 전송 없이 메시지 미리보기
    print("═" * 40)
    print("  메시지 미리보기 (실제 전송 X)")
    print("═" * 40)
    print("\n" + build_buy_message("삼성전자", 300000, "RSI 34.2",
          {"삼성전자": {"value": 300000}}, 700000))
    print("\n" + build_sell_message("삼성전자", 6.80, 20400, "익절",
          {"SK하이닉스": {"value": 300000}}, 720400, 1000000))
    print("\n" + build_daily_summary("2026-08-24", 2, 1, 20400, 1, 0, 5200, True,
          {"SK하이닉스": {"value": 305000}}, 720400, 1000000))
    print("\n" + "═" * 40)
    print("  설정 후 실제 전송 테스트:")
    print('  1) CONFIG에 토큰 입력  2) notify("테스트") 호출')
    print("═" * 40)

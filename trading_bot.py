"""
==============================================================================
  실전 자동매매 봇  (한국투자증권 KIS API)
==============================================================================
  ★ 안전장치 전부 내장 버전 ★

  전략   : strategies.py 플러그인 전략 모듈 사용 (.env의 BOT_STRATEGY로 선택, 기본 rsi)
  안전장치:
    [1] 중복 주문 방지    - 같은 종목 하루 1회 제한
    [2] 서킷브레이커      - 일일 손실 -5% 도달 시 당일 매매 전면 중단
    [3] 주문 재시도       - 네트워크 오류 시 자동 재시도
    [4] 전체 로그         - 모든 주문/에러를 파일에 기록 (추적·세금용)
    [5] 봇 다운 알림      - 예외 발생 시 카톡/텔레그램으로 즉시 통보
    [6] 설정 분리 (.env) - API 키를 코드에 안 박음 (보안 + 상품화 대비)

  ─────────────────────────────────────────────────────────────────────────
  ⚠️  반드시 지킬 것
    - 처음엔 무조건 모의투자(virtual_account=True)로 몇 주 돌려보세요.
    - 실전 전환은 로그를 보고 "의도대로 매매하는지" 확인한 뒤에.
    - 이 봇은 투자 손실을 막아주지 않습니다. 전략 검증(백테스트)이 먼저입니다.
  ─────────────────────────────────────────────────────────────────────────

  설치
    pip install python-kis python-dotenv pandas numpy requests
  실행
    python trading_bot.py          # .env 설정에 따라 모의/실전
==============================================================================
"""

import sys
import os
import json
import time
import logging
from datetime import datetime, date, time as dt_time
from pathlib import Path

if sys.platform == "win32":
    # Windows 콘솔 기본 인코딩(cp949)이 로그 메시지의 이모지(✅🚨 등)를 못 그려
    # StreamHandler가 조용히 로그를 누락시키는 문제 방지
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from pykis.client.exceptions import KisAPIError, KisHTTPError
from pykis.responses.exceptions import KisMarketNotOpenedError

# 알림 모듈 (앞서 만든 notifier.py 를 같은 폴더에 두세요)
try:
    import notifier
    HAS_NOTIFIER = True
except ImportError:
    HAS_NOTIFIER = False
    print("⚠️ notifier.py 없음 — 알림 없이 실행됩니다")

# 뉴스 감성분석 필터 (Claude API로 악재 종목 거르기, 보조 안전장치)
try:
    import news_filter
    HAS_NEWS_FILTER = True
except ImportError:
    HAS_NEWS_FILTER = False
    print("⚠️ news_filter.py 없음 — 뉴스 필터 없이 실행됩니다")

# 매매 전략 모듈 (플러그인 방식, .env의 BOT_STRATEGY로 선택)
from strategies import get_strategy


# ═════════════════════════════════════════════════════════════════════════
#  [6] 설정 분리 — .env 파일에서 로드
# ═════════════════════════════════════════════════════════════════════════
load_dotenv()

SETTINGS = {
    # KIS API (.env 에서)
    "hts_id":     os.getenv("KIS_HTS_ID", ""),        # HTS 아이디
    "app_key":    os.getenv("KIS_APP_KEY", ""),       # 실전 앱키
    "app_secret": os.getenv("KIS_APP_SECRET", ""),    # 실전 시크릿
    "v_app_key":  os.getenv("KIS_V_APP_KEY", ""),     # 모의투자 앱키
    "v_app_secret": os.getenv("KIS_V_APP_SECRET", ""),# 모의투자 시크릿
    "account_no": os.getenv("KIS_ACCOUNT_NO", ""),    # 실전 계좌번호, 예: 12345678-01
    "v_account_no": os.getenv("KIS_V_ACCOUNT_NO", ""),# 모의투자 계좌번호 (실전과 다름)
    "virtual":    os.getenv("KIS_VIRTUAL", "true").lower() == "true",  # 모의투자 여부

    # 전략 (strategies.py 에서 관리 — 이름만 지정. 파라미터는 각 전략 클래스 소속)
    "strategy_name": os.getenv("BOT_STRATEGY", "rsi"),
    "max_positions": 5,  # 한국+미국 합산 총 보유종목수 기준 (대형주 분산이라는 원래 취지 유지)

    # 안전장치
    "daily_loss_limit": -0.05,   # [2] 일일 손실 한도
    "max_retries": 3,            # [3] 주문 재시도 횟수
    "order_budget_ratio": 0.9,   # 현금의 90%까지만 사용 (여유 확보) — 켈리 비중 위에 추가로 곱함

    # 켈리 포지션 사이징 — backtest_pro.py의 kelly_weight()와 동일한 공식 이식
    # (최근 30건 실현손익으로 승률·손익비 계산 → 1/4 켈리, 5~25% 사이로 클리핑)
    "kelly_fraction": float(os.getenv("KELLY_FRACTION", "0.25")),
    "kelly_max_weight": float(os.getenv("KELLY_MAX_WEIGHT", "0.25")),
    "kelly_min_weight": float(os.getenv("KELLY_MIN_WEIGHT", "0.05")),

    # 미국 주식 거래 활성화 — 기본 false. true여도 KIS 앱에서 원화→달러 환전을
    # 미리 해두지 않으면 USD 예수금이 0이라 매수가 자동 스킵된다 (.env.example 참고)
    "enable_us_trading": os.getenv("ENABLE_US_TRADING", "false").lower() == "true",

    # 장후시간외(종가) 매수 — 기본 false. KIS 공식 "장후시간외" 주문조건은 python-kis
    # 라이브러리 단계에서부터 모의투자를 지원하지 않아(ValueError), 실전투자(KIS_VIRTUAL=false)
    # 로 전환하기 전까진 검증이 불가능하다. 켤 경우 15:40~16:00 KST에도 봇이 실행되도록
    # 스케줄러에 트리거를 추가해야 한다 (README 6-1단계 참고). 한국 종목에만 적용된다.
    "enable_closing_trade": os.getenv("ENABLE_CLOSING_TRADE", "false").lower() == "true",

    # 대상 종목 (대형주) — 아래에서 universe.json이 있으면 그 내용으로 덮어씀 (없으면 이 기본값 유지)
    "kr_universe": ["005930", "000660", "035420", "051910", "005380",
                    "005490", "035720", "012330", "068270", "105560"],
    # 미국 유니버스 — universe.json의 "us" 목록으로만 채워짐 (enable_us_trading이 false거나
    # universe.json에 us 목록이 없으면 항상 빈 리스트로 유지 → 미국 관련 로직 전체가 자연히 스킵됨)
    "us_universe": [],
}

# 종목코드 → 종목명 (뉴스 검색·알림 메시지용) — universe.json이 있으면 아래에서 덮어씀
KR_NAMES = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "051910": "LG화학",
    "005380": "현대차",
    "005490": "POSCO홀딩스",
    "035720": "카카오",
    "012330": "현대모비스",
    "068270": "셀트리온",
    "105560": "KB금융",
}

# 미국 종목코드(심볼) → 종목명 — universe.json의 "us" 목록에서 로드 (enable_us_trading=true일 때만)
US_NAMES = {}

# 미국 주식 매수/매도 시 지정가 슬리피지 버퍼 (0.5%)
# python-kis(2.1.6) 기준 미국(NASDAQ/NYSE/AMEX)은 "언제든 체결되는 시장가" 주문이 없고
# LOO/LOC/MOO/MOC(장개시·장마감 조건부) 주문만 지원한다 — 장중 임의 시점엔 쓸 수 없음.
# 그래서 매수는 현재가보다 살짝 높게, 매도는 살짝 낮게 지정가를 걸어 사실상 시장가처럼
# 즉시 체결되게 한다. 값이 너무 크면 불리한 가격에 체결되고, 너무 작으면 체결이 안 될 수 있어
# 대형주 정상 호가 스프레드보다 넉넉한 0.5%로 설정 (필요시 조정 가능하도록 상수로 분리).
US_LIMIT_SLIPPAGE = 0.005

# 장후시간외(종가) 주문 접수 시간대 — KRX 정규장(15:30) 마감 후 15:40~16:00 KST에만
# 그날 종가로 체결되는 장후시간외 주문이 접수된다. 서버 시계가 KST로 맞춰져 있다는 전제
# (서버_무인실행_가이드.md와 동일 전제) — datetime.now()를 그대로 KST로 취급한다.
CLOSING_TRADE_START = dt_time(15, 40)
CLOSING_TRADE_END = dt_time(16, 0)


def _in_closing_trade_window() -> bool:
    return CLOSING_TRADE_START <= datetime.now().time() <= CLOSING_TRADE_END


# 일일 요약 알림 — 스캔마다(5분마다) 보내던 걸 저녁 8시 이후 1회로 제한.
# 20시 정각에 스케줄러가 딱 한 번만 돌 것으로 기대하지만, 혹시 그 시간대에 여러 번
# 실행되거나(재시도 등) 스케줄이 밀려 여러 번 걸치더라도 하루 1번만 보내도록
# logs/daily_summary_sent.json에 마지막으로 보낸 날짜를 기록해 이중 전송을 막는다.
EVENING_SUMMARY_HOUR = 20
DAILY_SUMMARY_SENT_FILE = "logs/daily_summary_sent.json"


def _already_sent_daily_summary_today() -> bool:
    try:
        with open(DAILY_SUMMARY_SENT_FILE, encoding="utf-8") as f:
            return json.load(f).get("date") == str(date.today())
    except Exception:
        return False


def _mark_daily_summary_sent():
    try:
        os.makedirs("logs", exist_ok=True)
        with open(DAILY_SUMMARY_SENT_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": str(date.today())}, f)
    except Exception as e:
        log.warning(f"[일일요약] 전송 기록 저장 실패: {e}")


# ═════════════════════════════════════════════════════════════════════════
#  유니버스(거래대상) 로드 — universe.py로 오프라인 생성한 universe.json이 있으면 사용,
#  없거나 손상됐으면 위 하드코딩 기본 10종목으로 안전하게 폴백 (봇이 절대 죽으면 안 됨)
# ═════════════════════════════════════════════════════════════════════════
def _load_universe():
    universe_file = Path(__file__).resolve().parent / "universe.json"
    if not universe_file.exists():
        print(f"ℹ️ universe.json 없음 — 기본 유니버스({len(SETTINGS['kr_universe'])}종목) 사용 "
              f"(python universe.py 로 생성 가능)")
        return
    try:
        with open(universe_file, encoding="utf-8") as f:
            data = json.load(f)
        kr_list = data.get("kr", [])
        if not kr_list:
            raise ValueError("universe.json의 'kr' 목록이 비어있습니다")

        SETTINGS["kr_universe"] = [item["code"] for item in kr_list]
        KR_NAMES.clear()
        KR_NAMES.update({item["code"]: item["name"] for item in kr_list})
        print(f"ℹ️ universe.json에서 {len(kr_list)}종목 로드 (생성일시: {data.get('generated_at', '알수없음')})")

        # 미국 유니버스 — ENABLE_US_TRADING=true일 때만 로드 (꺼져있으면 아예 안 건드려서
        # 기존 한국전용 동작에 티끌만큼도 영향을 주지 않는다)
        if SETTINGS["enable_us_trading"]:
            us_list = data.get("us", [])
            if us_list:
                SETTINGS["us_universe"] = [item["symbol"] for item in us_list]
                US_NAMES.clear()
                US_NAMES.update({item["symbol"]: item["name"] for item in us_list})
                print(f"🇺🇸 universe.json에서 미국 {len(us_list)}종목 로드")
            else:
                print("🇺🇸 universe.json에 'us' 목록이 없음 — 미국 매매 스킵 (python universe.py로 생성 가능)")
    except Exception as e:
        print(f"⚠️ universe.json 로드 실패 — 기본 유니버스({len(SETTINGS['kr_universe'])}종목)로 폴백: "
              f"{type(e).__name__}: {e}")


_load_universe()

# 대시보드(dashboard.html)가 읽는 상태 파일 — 항상 이 스크립트와 같은 폴더에 저장
STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"


# ═════════════════════════════════════════════════════════════════════════
#  [4] 로깅 설정
# ═════════════════════════════════════════════════════════════════════════
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"logs/bot_{date.today()}.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")


# ═════════════════════════════════════════════════════════════════════════
#  안전장치 클래스들
# ═════════════════════════════════════════════════════════════════════════
class OrderGuard:
    """[1] 중복 주문 방지 — 같은 종목+액션은 하루 1회"""
    def __init__(self, state_file="logs/order_guard.json"):
        self.state_file = state_file
        self.today = str(date.today())
        self.done = self._load()

    def _load(self):
        if os.path.exists(self.state_file):
            with open(self.state_file) as f:
                data = json.load(f)
            if data.get("date") == self.today:
                return set(tuple(x) for x in data.get("orders", []))
        return set()

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump({"date": self.today, "orders": [list(x) for x in self.done]}, f)

    def can_order(self, ticker, action):
        return (ticker, action) not in self.done

    def mark(self, ticker, action):
        self.done.add((ticker, action))
        self._save()


class CircuitBreaker:
    """[2] 서킷브레이커 — 일일 손실 한도 도달 시 당일 매매 중단"""
    def __init__(self, limit):
        self.limit = limit
        self.start_equity = None
        self.halted = False

    def check(self, equity):
        if self.start_equity is None:
            self.start_equity = equity
            log.info(f"[서킷브레이커] 시작 자산 기록: {equity:,.0f}원")
        daily_ret = (equity - self.start_equity) / self.start_equity
        if daily_ret <= self.limit and not self.halted:
            self.halted = True
            log.warning(f"🚨 서킷브레이커 발동! 일일 손실 {daily_ret*100:.2f}% — 당일 매매 중단")
        return not self.halted, daily_ret


class KellyTracker:
    """[켈리 포지션 사이징] backtest_pro.py의 kelly_weight()와 동일한 공식을 실전 봇에 이식.

    - 최근 매도 30건의 실현손익(비율, 0.05 = +5%)으로 승률/평균익절/평균손절을 계산해
      1/4 켈리 비중(w, 0.05~0.25 사이)을 산출한다.
    - 거래 이력이 5건 미만이면(부트스트랩 금지 — 백테스트 데이터로 미리 채우지 않음)
      아직 통계가 부족하니 균등분배(1/max_positions)로 안전하게 시작한다.
    - 한국/미국 매매를 구분하지 않고 하나의 이력으로 "전략 확신도"를 계산한다 — w는
      통화와 무관한 비율(0~1)이다. 이 w를 원화 cash_pool과 USD cash_pool에 각각 곱해
      쓰는 건 호출부(calc_position_budget)의 책임이며, 여기서 통화를 다루지 않는다.
    - logs/kelly_history.json에 영구 저장 — 스케줄러가 스캔마다 프로세스를 재시작해도
      이력이 유지된다. 파일이 없거나 손상돼도 절대 죽지 않고 빈 이력(균등분배)으로 폴백한다.
    """
    def __init__(self, state_file="logs/kelly_history.json",
                 kelly_fraction=0.25, max_weight=0.25, min_weight=0.05):
        self.state_file = state_file
        self.kelly_fraction = kelly_fraction
        self.max_weight = max_weight
        self.min_weight = min_weight
        self.returns = self._load()

    def _load(self):
        if not os.path.exists(self.state_file):
            return []
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            returns = data.get("returns", [])
            if not isinstance(returns, list):
                raise ValueError("returns 필드가 리스트가 아닙니다")
            return [float(r) for r in returns][-30:]
        except Exception as e:
            log.warning(
                f"[켈리] kelly_history.json 로드 실패 — 이력 없이 시작(균등분배로 폴백): "
                f"{type(e).__name__}: {e}"
            )
            return []

    def _save(self):
        try:
            Path(self.state_file).parent.mkdir(exist_ok=True)
            tmp_path = f"{self.state_file}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"returns": self.returns}, f)
            os.replace(tmp_path, self.state_file)
        except Exception as e:
            # 저장 실패해도 매매 로직에 영향 주지 않음 — 다음 실행에서 다시 시도됨
            log.warning(f"[켈리] kelly_history.json 저장 실패: {type(e).__name__}: {e}")

    def record_return(self, ret_ratio):
        """ret_ratio: 실현손익 비율 (0.05 = +5%, -0.03 = -3%). 매도 체결마다
        (한국/미국, 전량/부분체결 무관) 호출해 최근 30건만 유지한다."""
        self.returns.append(float(ret_ratio))
        self.returns = self.returns[-30:]
        self._save()

    def current_weight(self, max_positions):
        """현재 이력으로 켈리 비중(0~1 사이 비율)을 계산. 5건 미만이면 균등분배."""
        n = len(self.returns)
        if n < 5:
            w = 1.0 / max_positions
            log.info(f"[켈리] 매도 이력 {n}건 (5건 미만) — 균등분배 비중 {w:.3f} 사용")
            return w

        wins = [r for r in self.returns if r > 0]
        losses = [r for r in self.returns if r <= 0]
        win_rate = len(wins) / n
        avg_win = float(np.mean(wins)) if wins else 0.05
        avg_loss = float(np.mean(losses)) if losses else -0.05

        if avg_loss == 0 or win_rate == 0:
            w = self.min_weight
        else:
            b = avg_win / abs(avg_loss)
            p, q = win_rate, 1 - win_rate
            k = (p - q / b) * self.kelly_fraction
            w = float(np.clip(k, self.min_weight, self.max_weight))

        log.info(
            f"[켈리] 매도 이력 {n}건, 승률 {win_rate*100:.0f}%, "
            f"평균익절 {avg_win*100:+.2f}%, 평균손절 {avg_loss*100:+.2f}% → 비중 {w:.3f}"
        )
        return w


def calc_position_budget(cash_pool, kelly_tracker, max_positions):
    """[한국/미국 공통 헬퍼] 켈리 비중 기반 매수 예산 계산.
    cash_pool은 호출부가 이미 통화별로 분리해 넘긴 현금(원화 또는 USD)이며,
    이 함수는 그 안에서만 계산하므로 원화/외화가 섞일 여지가 없다.
    최종 예산 = cash_pool * 켈리비중(w) * order_budget_ratio(기존 90% 버퍼 유지)."""
    w = kelly_tracker.current_weight(max_positions)
    return cash_pool * w * SETTINGS["order_budget_ratio"]


def with_retry(func, max_retries=3, desc="주문"):
    """[3] 재시도 래퍼"""
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except KisMarketNotOpenedError:
            # 장이 열려있지 않으면 재시도해도 소용없음 — 즉시 스킵
            log.warning(f"[{desc}] 장이 열려있지 않습니다 — 스킵")
            return None
        except KisAPIError as e:
            log.warning(
                f"[{desc}] 시도 {attempt}/{max_retries} 실패 — "
                f"KIS API 오류 [{e.error_code}] {e.message}"
            )
            time.sleep(1.5 * attempt)
        except KisHTTPError as e:
            log.warning(
                f"[{desc}] 시도 {attempt}/{max_retries} 실패 — "
                f"HTTP 오류 ({e.status_code}) {e.reason}"
            )
            time.sleep(1.5 * attempt)
        except Exception as e:
            log.warning(f"[{desc}] 시도 {attempt}/{max_retries} 실패: {e}")
            time.sleep(1.5 * attempt)
    log.error(f"[{desc}] 최대 재시도 초과 — 건너뜀")
    return None


# ═════════════════════════════════════════════════════════════════════════
#  봇 본체
# ═════════════════════════════════════════════════════════════════════════
class TradingBot:
    def __init__(self):
        self.guard = OrderGuard()
        self.breaker = CircuitBreaker(SETTINGS["daily_loss_limit"])
        self.kelly_tracker = KellyTracker(
            kelly_fraction=SETTINGS["kelly_fraction"],
            max_weight=SETTINGS["kelly_max_weight"],
            min_weight=SETTINGS["kelly_min_weight"],
        )
        self.kis = None
        self.day_stats = {"buy": 0, "sell": 0, "realized_pnl": 0}
        self.day_trades = []  # 대시보드용 — 이번 실행에서 실제 체결된 매수/매도 내역
        # 전략 인스턴스 (이름이 잘못됐으면 get_strategy가 ValueError를 던짐 → 봇 시작 실패로 처리)
        self.strategy = get_strategy(SETTINGS["strategy_name"])
        log.info(f"전략: {self.strategy.name} — {self.strategy.description}")
        self._connect()

    def _connect(self):
        """KIS API 연결 (python-kis 2.x — KisAuth 방식)"""
        from pykis import PyKis, KisAuth
        if not SETTINGS["app_key"]:
            raise RuntimeError(".env에 KIS_APP_KEY가 없습니다. 설정을 확인하세요.")

        # 실전 앱키는 실전 계좌번호와, 모의 앱키는 모의 계좌번호와 짝을 이뤄야 합니다
        # (KisAuth.account는 "그 앱키에 연결된 계좌번호"라 서로 못 바꿔 씁니다).
        real_auth = KisAuth(
            id=SETTINGS["hts_id"],              # HTS 아이디
            appkey=SETTINGS["app_key"],
            secretkey=SETTINGS["app_secret"],   # 실제 파라미터명은 secretkey
            account=SETTINGS["account_no"],
            virtual=False,
        )
        if SETTINGS["virtual"]:
            if not SETTINGS["v_account_no"]:
                raise RuntimeError(
                    ".env에 KIS_V_ACCOUNT_NO(모의투자 계좌번호)가 없습니다. "
                    "모의투자 계좌번호는 실전 계좌번호와 다릅니다 — KIS 모의투자 신청 화면에서 확인하세요."
                )
            virtual_auth = KisAuth(
                id=SETTINGS["hts_id"],
                appkey=SETTINGS["v_app_key"],
                secretkey=SETTINGS["v_app_secret"],
                account=SETTINGS["v_account_no"],
                virtual=True,
            )
            self.kis = PyKis(real_auth, virtual_auth, keep_token=True)
        else:
            self.kis = PyKis(real_auth, keep_token=True)

        mode = "모의투자" if SETTINGS["virtual"] else "🔴 실전투자"
        log.info(f"KIS API 연결 완료 — {mode}")
        if HAS_NOTIFIER:
            notifier.notify(f"🤖 자동매매 봇 시작 ({mode})")

    # ── 시세 ──
    def get_chart(self, ticker, market="KR"):
        """일봉 DataFrame(open/high/low/close/volume) 반환. 지표 계산은 전략 쪽에서 수행."""
        from datetime import timedelta
        stock = self.kis.stock(ticker, market=market)
        # 전략이 요구하는 최소 거래일수(required_days) 기준으로 조회 기간 산정
        # 평일 비율(7/5)로 달력일 환산 + 휴장일 대비 버퍼 60일
        needed_days = int(self.strategy.required_days * 7 / 5) + 60
        start = date.today() - timedelta(days=needed_days)
        # adjust=True → 수정주가 (액면분할/배당 반영, 지표 정확도 필수)
        chart = stock.chart(start=start, period="day", adjust=True).df()
        if chart is None or len(chart) < self.strategy.required_days:
            return None
        return chart

    # ── 계좌 상태 (한국, 원화) ──
    def get_account(self):
        """한국 보유 종목 + 원화 현금 조회. 기존 호출부(뉴스필터/전략연결/대시보드연동/
        실전예외처리)가 그대로 쓰는 시그니처라 절대 바꾸지 않는다."""
        balance = self.kis.account().balance(country="KR")
        deposit = balance.deposit("KRW")
        cash = float(deposit.amount) if deposit else 0.0  # 예수금(현금)
        positions = {}
        for stock in balance.stocks:
            positions[stock.symbol] = {
                "qty": int(stock.qty),
                "value": float(stock.current_amount),  # 현재 평가금액
                "avg_price": float(stock.purchase_price),  # 매입 평단가
                "profit_rate": float(stock.profit_rate),
            }
        return positions, cash

    # ── 계좌 상태 (미국, 달러) — ENABLE_US_TRADING=true일 때만 호출 ──
    def get_usd_account(self):
        """미국 보유 종목 + USD 예수금 조회. get_account()와 절대 합치지 않는다
        (원화 현금과 외화 예수금은 완전히 분리된 예산 — 섞이면 자금관리 사고).

        반환: (positions, usd_cash, usd_krw_rate)
          - positions: {심볼: {"qty","value"(USD),"avg_price"(USD),"profit_rate"}}
          - usd_cash : 미국 매수 예산 계산에만 쓰는 USD 예수금 (원화 cash와 절대 섞지 않음)
          - usd_krw_rate : KIS 기준환율. 대시보드/서킷브레이커의 "원화 환산 총자산" 표시용으로만
            쓰고, 매수 예산 계산에는 쓰지 않는다.
        """
        balance = self.kis.account().balance(country="US")
        deposit = balance.deposit("USD")
        usd_cash = float(deposit.amount) if deposit else 0.0
        usd_krw_rate = float(deposit.exchange_rate) if deposit and deposit.exchange_rate else 0.0
        positions = {}
        for stock in balance.stocks:
            positions[stock.symbol] = {
                "qty": int(stock.qty),
                "value": float(stock.current_amount),      # USD 평가금액
                "avg_price": float(stock.purchase_price),  # USD 매입 평단가
                "profit_rate": float(stock.profit_rate),
            }
        return positions, usd_cash, usd_krw_rate

    def total_equity(self, positions, cash):
        return sum(p["value"] for p in positions.values()) + cash

    def total_equity_krw(self, kr_positions, kr_cash, us_positions=None, usd_cash=0.0, usd_krw_rate=0.0):
        """계좌 전체 자산을 원화 기준으로 환산한 총평가금액 (서킷브레이커·대시보드용).
        KR은 이미 원화라 그대로 더하고, US는 KIS 기준환율로 원화 환산해서 더한다.
        ⚠️ 매수 예산 계산에는 이 값을 쓰지 않는다 — 원화/외화 예산 분리 원칙은 별도로 지킨다.
        us_positions/usd_cash가 모두 비어있으면(=미국 미사용) 기존 total_equity()와 완전히 동일하다."""
        total = self.total_equity(kr_positions, kr_cash)
        if us_positions or usd_cash:
            total += sum(p["value"] for p in (us_positions or {}).values()) * usd_krw_rate
            total += usd_cash * usd_krw_rate
        return total

    def _combined_snapshot(self):
        """알림/대시보드용 — 원화 기준으로 환산한 한국+미국 통합 스냅샷.
        (매수 예산 계산에는 이 함수를 쓰지 않는다. get_account()/get_usd_account()를
        각 시장 예산 계산에 그대로 따로 쓴다 — 원화/외화 예산을 섞지 않기 위함.)

        반환: (portfolio_krw, total_cash_krw, kr_positions, kr_cash, us_positions, usd_cash, usd_rate)
        ENABLE_US_TRADING=false면 us_positions={}, usd_cash=0, usd_rate=0이 되어
        portfolio_krw/total_cash_krw가 기존 KR 전용 결과와 완전히 동일해진다."""
        kr_positions, kr_cash = self.get_account()
        us_positions, usd_cash, usd_rate = {}, 0.0, 0.0
        if SETTINGS["enable_us_trading"]:
            us_positions, usd_cash, usd_rate = self.get_usd_account()

        portfolio = {t: {"value": p["value"]} for t, p in kr_positions.items()}
        portfolio.update({f"🇺🇸{t}": {"value": p["value"] * usd_rate} for t, p in us_positions.items()})
        total_cash = kr_cash + usd_cash * usd_rate
        return portfolio, total_cash, kr_positions, kr_cash, us_positions, usd_cash, usd_rate

    # ── 매수 ──
    def try_buy(self, ticker, df, positions, cash, total_position_count, market="KR"):
        """market: "KR"(원화, 시장가) 또는 "US"(USD, 지정가+슬리피지 버퍼).
        positions/cash는 반드시 해당 market 통화 기준으로 호출부에서 넘겨야 한다
        (원화 cash로 미국 종목을, USD cash로 한국 종목을 사면 안 됨).
        total_position_count는 한국+미국 합산 보유종목수 — max_positions 슬롯 한도는
        시장 공통이지만 예산(budget)은 이 함수에 넘어온 market 고유의 cash에서만 계산한다."""
        # 전략 조건 판단 (strategies.py) — 시장과 무관하게 동일 로직 재사용
        buy_signal, reason = self.strategy.check_buy(df)
        if not buy_signal:
            return

        tag = "🇺🇸 " if market == "US" else ""  # KR은 빈 문자열이라 로그가 기존과 완전히 동일

        # 안전장치: 중복주문
        if not self.guard.can_order(ticker, "BUY"):
            log.info(f"{tag}[{ticker}] 오늘 이미 매수 — 스킵")
            return
        if total_position_count >= SETTINGS["max_positions"]:
            return

        # 예산 확인을 뉴스필터보다 먼저 — 1주도 못 살 현금이면 Claude API를 호출할 필요가 없다
        # (돈이 없어서 어차피 못 사는데 감성분석부터 하면 API 토큰만 낭비됨)
        price = df["close"].iloc[-1]
        # 예산 = cash_pool(market 고유 통화) * 켈리비중 * order_budget_ratio — 한국/미국 공통 헬퍼
        budget = calc_position_budget(cash, self.kelly_tracker, SETTINGS["max_positions"])
        qty = int(budget // price)
        if qty < 1:
            return

        # 장후시간외(종가) 매수 — ENABLE_CLOSING_TRADE=true이고 15:40~16:00 KST일 때만.
        # 미국(해외) 종목엔 이 주문조건이 없어 한국 종목에만 적용한다.
        # 뉴스필터(Claude API)보다 먼저 판단한다 — 모의투자라 어차피 주문이 거부될 걸
        # 알면서 감성분석부터 하면 위의 예산 체크와 같은 이유로 API 토큰만 낭비된다.
        closing = market == "KR" and SETTINGS["enable_closing_trade"] and _in_closing_trade_window()

        if closing and SETTINGS["virtual"]:
            # python-kis 라이브러리가 이 주문조건을 모의투자에서 API 호출 전에 바로 거부한다
            # (ValueError) — 재시도해도 절대 안 되는 실패라 KisMarketNotOpenedError와 같은
            # 방식으로 즉시 스킵한다 (with_retry로 3번 헛되이 재시도하지 않도록).
            log.info(f"[{ticker}] 종가매매는 모의투자 미지원 — 스킵 (실전투자 전환 후 사용 가능)")
            return

        # 안전장치: 뉴스 감성분석 필터 (보조 안전장치, fail-open) — 시장 무관 동일 재사용
        names = KR_NAMES if market == "KR" else US_NAMES
        name = names.get(ticker, ticker)
        if HAS_NEWS_FILTER:
            blocked, news_reason = news_filter.is_blocked(ticker, name, market)
            if blocked:
                log.info(f"🚫 {tag}[{ticker}] {name} 매수 스킵 — 뉴스 감성분석 악재 판정: {news_reason}")
                return

        if closing:
            order_tag = "종가"

            def do_order():
                # 장후시간외 종가 주문은 가격을 지정하지 않는다 — 그날 종가로 자동 체결된다.
                return self.kis.stock(ticker, market="KR").buy(qty=qty, condition="after")
        elif market == "KR":
            order_tag = "매수"

            def do_order():
                return self.kis.stock(ticker, market="KR").buy(qty=qty)
        else:
            order_tag = "매수"
            buy_price = price * (1 + US_LIMIT_SLIPPAGE)

            def do_order():
                # 미국은 "언제든 체결되는 시장가"가 없어(장개시/장마감 조건부 주문만 지원)
                # 현재가+슬리피지 버퍼의 지정가로 사실상 시장가처럼 즉시체결을 노린다.
                return self.kis.stock(ticker, market=None).buy(qty=qty, price=buy_price)

        result = with_retry(do_order, SETTINGS["max_retries"], f"{tag}{ticker} {order_tag}")
        if not result:
            return

        if closing:
            # 장후시간외 주문은 접수 직후엔 아직 체결 전이라(그날 종가 확정 후 순차 체결),
            # 다른 주문처럼 잔고를 바로 재조회해 체결 수량을 대조하면 항상 0으로 잘못 나온다.
            # 그래서 여기선 체결 확인을 생략하고 "접수"로만 기록 — 실제 체결 여부는 다음날
            # 첫 스캔에서 잔고에 반영돼 있을 것이다. 체결가는 장후시간외 특성상 반드시
            # 오늘 종가와 동일하므로(그 외 가격으로는 체결되지 않음) price를 그대로 쓴다.
            self.guard.mark(ticker, "BUY")
            self.day_stats["buy"] += 1
            log.info(f"📗 [{ticker}] 종가매매 매수 주문 접수 — 오늘 종가 {price:,.0f}원 기준, "
                     f"실제 체결은 장마감 후 확정 ({reason})")
            self.day_trades.append({
                "action": "BUY",
                "market": "KR",
                "name": name,
                "price": float(price),
                "qty": int(qty),
                "reason": f"[종가매매 접수, 체결 미확정] {reason}",
                "time": datetime.now().strftime("%H:%M"),
            })
            if HAS_NOTIFIER:
                portfolio, total_cash, _, _, _, _, _ = self._combined_snapshot()
                notifier.notify_buy(f"[종가매매 접수] {ticker}", float(price) * qty, reason, portfolio, total_cash)
            return

        # 주문 성공(예외 없음) ≠ 체결. 잔고를 재조회해 실제 체결 수량을 확인한다.
        # (모의투자는 미체결 조회를 지원하지 않으므로 잔고 대조 방식을 사용)
        portfolio, total_cash, kr_positions, kr_cash, us_positions, usd_cash, usd_rate = self._combined_snapshot()
        pos_lookup, rate = (kr_positions, 1.0) if market == "KR" else (us_positions, usd_rate)
        actual_qty = pos_lookup.get(ticker, {}).get("qty", 0)

        if actual_qty == 0:
            # 주문은 성공했지만 실제 체결이 하나도 안 됨 — 다음 스캔에서 재시도 가능하게
            # guard.mark()도, day_trades 기록도 하지 않는다 (실제 체결이 아니므로).
            log.warning(f"⚠️ {tag}[{ticker}] 매수 주문은 접수됐지만 체결 수량이 0주입니다 — 재시도 대상으로 남김")
            return

        self.guard.mark(ticker, "BUY")
        self.day_stats["buy"] += 1

        if actual_qty < qty:
            log.warning(f"⚠️ {tag}[{ticker}] 부분체결: 요청 {qty}주 중 {actual_qty}주만 체결")
        buy_amount = pos_lookup.get(ticker, {}).get("value", actual_qty * price)
        if market == "KR":
            log.info(f"✅ 매수 {ticker} {actual_qty}주 @ {price:,.0f} ({reason})")
        else:
            log.info(f"🇺🇸 ✅ 매수 {ticker} {actual_qty}주 @ ${price:,.2f} ({reason})")
        self.day_trades.append({
            "action": "BUY",
            "market": market,
            "name": f"{name} (USD)" if market == "US" else name,
            "price": float(price) * rate,   # 대시보드는 항상 원화 기준 — US는 KIS 기준환율로 환산
            "qty": int(actual_qty),
            "reason": reason,
            "time": datetime.now().strftime("%H:%M"),
        })
        if HAS_NOTIFIER:
            # 알림은 항상 한국+미국 통합 스냅샷(원화 환산)을 보여준다 — market="KR"이고
            # enable_us_trading=false면 portfolio/total_cash가 기존 KR 전용 결과와 동일하다.
            notifier.notify_buy(f"{tag}{ticker}", buy_amount * rate, reason, portfolio, total_cash)

    # ── 매도 ──
    def try_sell(self, ticker, df, position, initial_equity, market="KR"):
        """market: "KR"(원화, 시장가 전량매도) 또는 "US"(USD, 지정가+슬리피지 버퍼 전량매도)."""
        entry = position["avg_price"]
        price = df["close"].iloc[-1]

        sell_signal, reason = self.strategy.check_sell(df, entry_price=entry)
        if not sell_signal:
            return
        if not self.guard.can_order(ticker, "SELL"):
            return

        tag = "🇺🇸 " if market == "US" else ""  # KR은 빈 문자열이라 로그가 기존과 완전히 동일

        if market == "KR":
            def do_order():
                return self.kis.stock(ticker, market="KR").sell()  # 전량 매도
        else:
            sell_price = price * (1 - US_LIMIT_SLIPPAGE)

            def do_order():
                # 미국은 시장가가 없어 현재가-슬리피지 버퍼의 지정가로 전량 매도(즉시체결 지향)
                return self.kis.stock(ticker, market=None).sell(price=sell_price)

        result = with_retry(do_order, SETTINGS["max_retries"], f"{tag}{ticker} 매도")
        if not result:
            # 재시도까지 다 실패 — 손절/익절이 실행되지 못한 위험한 상황. 반드시 사람이 알아야 함.
            log.error(f"🚨 {tag}[{ticker}] 매도 실패 — 손절/익절 미실행, 수동 확인 필요 ({reason})")
            if HAS_NOTIFIER:
                notifier.notify(f"🚨 {tag}{ticker} 매도 실패 — 손절/익절 미실행, 수동 확인 필요 ({reason})")
            return

        # 주문 성공(예외 없음) ≠ 전량 체결. 잔고를 재조회해 실제 잔여 수량을 확인한다.
        portfolio, total_cash, kr_positions, kr_cash, us_positions, usd_cash, usd_rate = self._combined_snapshot()
        pos_lookup, rate = (kr_positions, 1.0) if market == "KR" else (us_positions, usd_rate)
        remaining_qty = pos_lookup.get(ticker, {}).get("qty", 0)
        sold_qty = position["qty"] - remaining_qty

        if remaining_qty > 0:
            # 부분체결 — guard.mark()를 호출하지 않아 다음 스캔에서 잔여 수량에 대한
            # 손절/익절 안전장치가 계속 작동하도록 남겨둔다.
            log.warning(
                f"⚠️ {tag}[{ticker}] 부분체결: {position['qty']}주 중 {sold_qty}주만 매도, "
                f"{remaining_qty}주 잔여"
            )
        else:
            self.guard.mark(ticker, "SELL")

        if sold_qty <= 0:
            # 이론상 거의 없지만(체결 0주) 손익 계산이 무의미하므로 조용히 종료
            log.warning(f"⚠️ {tag}[{ticker}] 매도 주문은 접수됐지만 체결 수량이 0주입니다")
            return

        self.day_stats["sell"] += 1
        ret_ratio = (price - entry) / entry
        ret_pct = ret_ratio * 100
        profit = (price - entry) * sold_qty
        # day_stats["realized_pnl"]은 원화 단일 통화 누계이므로 반드시 원화 환산해서 더한다
        # (KR rate=1.0이라 기존 동작과 완전히 동일, US만 KIS 기준환율로 환산됨)
        self.day_stats["realized_pnl"] += profit * rate
        # 켈리 이력 갱신 — 한국/미국, 전량/부분체결 무관 매도 체결마다 기록
        # (수익률 비율은 통화와 무관하므로 rate 환산 없이 그대로 저장)
        self.kelly_tracker.record_return(ret_ratio)
        names = KR_NAMES if market == "KR" else US_NAMES
        name = names.get(ticker, ticker)
        log.info(f"{tag}✅ 매도 {ticker} {sold_qty}주 수익률 {ret_pct:+.2f}% ({reason})")
        self.day_trades.append({
            "action": "SELL",
            "market": market,
            "name": f"{name} (USD)" if market == "US" else name,
            "qty": int(sold_qty),
            "ret": round(ret_pct, 1),
            "profit": round(profit * rate),   # 대시보드는 항상 원화 기준
            "reason": reason,
            "time": datetime.now().strftime("%H:%M"),
        })
        if HAS_NOTIFIER:
            # 알림은 항상 한국+미국 통합 스냅샷(원화 환산)을 보여준다 — market="KR"이고
            # enable_us_trading=false면 portfolio/total_cash가 기존 KR 전용 결과와 동일하다.
            notifier.notify_sell(f"{tag}{ticker}", ret_pct, profit * rate, reason,
                                 portfolio, total_cash, initial_equity)

    # ── 1회 스캔 (장중 주기적 호출) ──
    def run_once(self, initial_equity):
        positions, cash = self.get_account()
        us_positions, usd_cash, usd_rate = {}, 0.0, 0.0
        if SETTINGS["enable_us_trading"]:
            us_positions, usd_cash, usd_rate = self.get_usd_account()
            if usd_cash <= 0:
                log.info("🇺🇸 USD 예수금 0원 — 미국 매수 스킵 (KIS 앱에서 환전 필요)")

        equity = self.total_equity_krw(positions, cash, us_positions, usd_cash, usd_rate)

        # [2] 서킷브레이커 체크 (한국+미국 합산 자산 기준. 미국 미사용이면 기존과 동일)
        can_trade, daily_ret = self.breaker.check(equity)
        if not can_trade:
            log.warning(f"서킷브레이커 정지 상태 — 매매 스킵 (일일 {daily_ret*100:.2f}%)")
            return

        # 보유 종목 매도 판단 — 한국
        for ticker in list(positions.keys()):
            df = with_retry(lambda t=ticker: self.get_chart(t, market="KR"),
                             desc=f"{ticker} 시세")
            if df is not None:
                self.try_sell(ticker, df, positions[ticker], initial_equity, market="KR")

        # 보유 종목 매도 판단 — 미국 (enable_us_trading=true일 때만)
        if SETTINGS["enable_us_trading"]:
            for ticker in list(us_positions.keys()):
                df = with_retry(lambda t=ticker: self.get_chart(t, market=None),
                                 desc=f"🇺🇸 {ticker} 시세")
                if df is not None:
                    self.try_sell(ticker, df, us_positions[ticker], initial_equity, market="US")

        # 신규 매수 판단 — 매도 반영 후 재조회
        positions, cash = self.get_account()
        if SETTINGS["enable_us_trading"]:
            us_positions, usd_cash, usd_rate = self.get_usd_account()

        # 한국 매수 — 반드시 원화 cash에서만 예산 계산
        for ticker in SETTINGS["kr_universe"]:
            if ticker in positions:
                continue
            df = with_retry(lambda t=ticker: self.get_chart(t, market="KR"),
                             desc=f"{ticker} 시세")
            if df is not None:
                self.try_buy(ticker, df, positions, cash, len(positions) + len(us_positions), market="KR")
                positions, cash = self.get_account()

        # 미국 매수 — 반드시 USD cash에서만 예산 계산 (원화와 절대 섞지 않음)
        if SETTINGS["enable_us_trading"]:
            for ticker in SETTINGS["us_universe"]:
                if ticker in us_positions:
                    continue
                df = with_retry(lambda t=ticker: self.get_chart(t, market=None),
                                 desc=f"🇺🇸 {ticker} 시세")
                if df is not None:
                    self.try_buy(ticker, df, us_positions, usd_cash,
                                 len(positions) + len(us_positions), market="US")
                    us_positions, usd_cash, usd_rate = self.get_usd_account()

    # ── 일일 요약 ──
    def send_daily_summary(self, initial_equity):
        """저녁 8시 이후 하루 1번만 보낸다 (스캔마다 보내던 걸 제한).
        반드시 save_state() 이후에 호출해야 한다 — bot_state.json의 누적 거래내역을
        근거로 국내/해외를 나눠 집계하기 때문에, 이 프로세스(이번 스캔)의 매매만 담긴
        self.day_stats가 아니라 그날 전체 스캔이 합쳐진 파일을 읽는다."""
        if datetime.now().hour < EVENING_SUMMARY_HOUR:
            return
        if _already_sent_daily_summary_today():
            return
        if not HAS_NOTIFIER:
            return

        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            log.warning(f"[일일요약] bot_state.json 로드 실패 — 요약 스킵: {e}")
            return

        trades = state.get("trades", [])
        kr_buy = sum(1 for t in trades if t.get("action") == "BUY" and t.get("market", "KR") == "KR")
        kr_sell = sum(1 for t in trades if t.get("action") == "SELL" and t.get("market", "KR") == "KR")
        kr_pnl = sum(t.get("profit", 0) for t in trades if t.get("action") == "SELL" and t.get("market", "KR") == "KR")
        us_buy = sum(1 for t in trades if t.get("action") == "BUY" and t.get("market") == "US")
        us_sell = sum(1 for t in trades if t.get("action") == "SELL" and t.get("market") == "US")
        us_pnl = sum(t.get("profit", 0) for t in trades if t.get("action") == "SELL" and t.get("market") == "US")

        portfolio, total_cash, *_ = self._combined_snapshot()
        notifier.notify_daily(
            str(date.today()),
            kr_buy, kr_sell, kr_pnl,
            us_buy, us_sell, us_pnl,
            SETTINGS["enable_us_trading"],
            portfolio, total_cash, initial_equity,
        )
        _mark_daily_summary_sent()

    # ── 대시보드용 상태 저장 (bot_state.json) ──
    def save_state(self):
        """dashboard.html이 읽는 bot_state.json을 저장.
        실패해도(디스크 오류 등) 매매 로직에 영향을 주면 안 되므로 예외를 여기서 모두 삼킨다.
        """
        try:
            self._save_state_impl()
        except Exception as e:
            log.warning(f"[대시보드] bot_state.json 저장 실패 (매매엔 영향 없음): {type(e).__name__}: {e}")

    def _save_state_impl(self):
        today_str = str(date.today())

        # 스케줄러가 하루에 여러 번 호출하므로, 기존 파일이 있으면 이어서 갱신
        existing = {}
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as e:
                log.warning(f"[대시보드] 기존 bot_state.json 읽기 실패 — 새로 생성: {e}")
                existing = {}
        existing_date = str(existing.get("updated", "")).split(" ")[0] or None
        is_same_day = existing_date == today_str

        positions, cash = self.get_account()
        us_positions, usd_cash, usd_rate = {}, 0.0, 0.0
        if SETTINGS["enable_us_trading"]:
            us_positions, usd_cash, usd_rate = self.get_usd_account()

        # 대시보드는 단일 통화(원화)만 표시하므로 US 값은 KIS 기준환율로 환산해서 넣는다.
        # enable_us_trading=false면 us_positions/usd_cash가 비어있어 기존 KR 전용 값과 동일하다.
        equity = self.total_equity_krw(positions, cash, us_positions, usd_cash, usd_rate)
        display_cash = cash + usd_cash * usd_rate

        # 전체 원금(initial) — 최초 1회만 기록, 이후엔 그대로 유지
        initial = existing.get("initial", equity)

        # 자산 추이(equityCurve) — 일 단위. 같은 날짜의 마지막 값은 덮어쓰고, 날짜가 바뀌면 새로 추가
        curve = list(existing.get("equityCurve", []))
        if is_same_day and curve:
            curve[-1] = equity
        else:
            curve.append(equity)
        curve = curve[-30:]

        # 보유 종목 — 대시보드 HTML 구조는 그대로 두고, 미국 종목은 이름 뒤에 "(USD)"만 붙여
        # 원래 통화를 표시한다 (value/ret은 다른 필드와 동일하게 원화 기준 숫자로 맞춤).
        holdings = [
            {
                "name": KR_NAMES.get(code, code),
                "code": code,
                "qty": p["qty"],
                "value": p["value"],
                "ret": round(p["profit_rate"], 1),
            }
            for code, p in positions.items()
        ] + [
            {
                "name": f"{US_NAMES.get(symbol, symbol)} (USD)",
                "code": symbol,
                "qty": p["qty"],
                "value": p["value"] * usd_rate,
                "ret": round(p["profit_rate"], 1),
            }
            for symbol, p in us_positions.items()
        ]

        # 매매 내역 — 오늘 것만. 같은 날이면 기존 기록(이전 실행분)에 이번 실행분을 합침
        # (guard가 종목+액션당 하루 1회로 막아주므로 중복 체결 걱정 없음)
        prev_trades = existing.get("trades", []) if is_same_day else []
        trades = self.day_trades + prev_trades
        trades.sort(key=lambda t: t.get("time", ""), reverse=True)

        # 오늘 실현손익 — 오늘 체결된 매도 내역 기준 (프로세스가 스캔마다 재시작되므로
        # self.day_stats 대신 위에서 합친 trades를 근거로 계산해야 하루 전체가 정확함)
        today_pnl = sum(t.get("profit", 0) for t in trades if t.get("action") == "SELL")

        # 안전장치 상태 — 실제로 확인 가능한 값만 표시 (지어내지 않음)
        breaker = self.breaker
        if breaker.halted:
            breaker_item = {
                "name": "서킷브레이커", "state": "warn",
                "desc": f"일일 손실 한도 도달로 정지 (기준 {breaker.limit*100:.0f}%)",
            }
        else:
            breaker_item = {
                "name": "서킷브레이커", "state": "on",
                "desc": f"일일 {breaker.limit*100:.0f}% 한도 감시중",
            }

        news_filter_active = HAS_NEWS_FILTER and news_filter.has_valid_key()
        if news_filter_active:
            news_item = {"name": "뉴스 감성필터", "state": "on", "desc": "활성 — 악재 종목 매수 스킵"}
        else:
            news_item = {"name": "뉴스 감성필터", "state": "warn", "desc": "미설정 — fail-open으로 통과 중"}

        safety = [
            {
                "name": "중복주문 방지", "state": "on",
                "desc": f"오늘 {len(self.guard.done)}건 기록됨",
            },
            breaker_item,
            {
                "name": "주문 재시도", "state": "on",
                "desc": f"실패 시 최대 {SETTINGS['max_retries']}회 재시도",
            },
            news_item,
        ]

        if SETTINGS["enable_us_trading"]:
            if usd_cash > 0:
                us_item = {
                    "name": "미국 주식 매매", "state": "on",
                    "desc": f"활성 — USD 예수금 ${usd_cash:,.0f}",
                }
            else:
                us_item = {
                    "name": "미국 주식 매매", "state": "warn",
                    "desc": "USD 예수금 0 — KIS 앱에서 환전 필요",
                }
            safety.append(us_item)

        if SETTINGS["enable_closing_trade"]:
            if SETTINGS["virtual"]:
                closing_item = {
                    "name": "종가매매", "state": "warn",
                    "desc": "모의투자는 미지원 — 실전투자 전환 전까지 항상 스킵됨",
                }
            else:
                closing_item = {
                    "name": "종가매매", "state": "on",
                    "desc": "활성 — 15:40~16:00 KST 장후시간외 매수",
                }
            safety.append(closing_item)

        state = {
            "mode": "모의투자" if SETTINGS["virtual"] else "실전투자",
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "initial": initial,
            "cash": display_cash,
            "equity": equity,
            "todayPnl": today_pnl,
            "equityCurve": curve,
            "holdings": holdings,
            "trades": trades,
            "safety": safety,
        }

        # 원자적 쓰기 — 대시보드가 폴링 중에 쓰다 만 파일을 읽지 않도록
        tmp_path = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, STATE_FILE)
        log.info(f"[대시보드] bot_state.json 저장 완료 ({STATE_FILE})")


# ═════════════════════════════════════════════════════════════════════════
#  메인 — 장중 주기 실행
# ═════════════════════════════════════════════════════════════════════════
def main():
    try:
        bot = TradingBot()
    except Exception as e:
        log.error(f"봇 시작 실패: {e}")
        if HAS_NOTIFIER:
            notifier.notify(f"❌ 봇 시작 실패: {e}")
        return

    # 시작 시점 자산 기록 (일일 수익률·서킷브레이커 기준) — 한국+미국 합산(원화 환산)
    try:
        pos, cash = bot.get_account()
        us_pos, usd_cash, usd_rate = {}, 0.0, 0.0
        if SETTINGS["enable_us_trading"]:
            us_pos, usd_cash, usd_rate = bot.get_usd_account()
        initial_equity = bot.total_equity_krw(pos, cash, us_pos, usd_cash, usd_rate)
    except Exception as e:
        log.error(f"계좌 조회 실패: {e}")
        return

    # [5] 전체를 try로 감싸 — 봇이 죽으면 알림
    try:
        # 장중 N분마다 1회 스캔 (여기선 예시로 1회. 스케줄러가 반복 호출)
        bot.run_once(initial_equity)
        bot.save_state()
        bot.send_daily_summary(initial_equity)
        log.info("스캔 완료")
    except KeyboardInterrupt:
        log.info("사용자 중단")
    except Exception as e:
        log.exception("봇 실행 중 치명적 오류")
        if HAS_NOTIFIER:
            notifier.notify(f"🚨 봇 다운!\n{type(e).__name__}: {str(e)[:150]}")


if __name__ == "__main__":
    main()

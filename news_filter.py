"""
==============================================================================
  뉴스 감성분석 필터  (Claude API)
==============================================================================
  매수 직전, 최근 종목 뉴스 헤드라인(한국: 네이버금융 / 미국: Google News RSS)을
  모아 Claude에게 "호재 / 중립 / 악재" 판단을 맡깁니다.

  ⚠️ 이 필터는 보조 안전장치입니다.
     서킷브레이커·손절 같은 주 리스크 관리를 대체하지 않습니다.
     그래서 아래 상황에선 전부 "막지 않음(fail-open)"으로 처리합니다:
       - .env에 ANTHROPIC_API_KEY가 없을 때
       - 네이버금융 뉴스 페이지를 못 가져오거나 파싱 실패했을 때
       - Claude API 호출이 실패(네트워크/레이트리밋/응답파싱 오류)했을 때
     다만 어떤 이유로 스킵됐는지는 항상 로그에 남깁니다.

  같은 날 같은 종목은 API를 중복 호출하지 않도록
  logs/news_cache_{date}.json 에 결과를 캐싱합니다. (OrderGuard와 동일 패턴)
==============================================================================
"""

import os
import re
import json
import html
import logging
from datetime import date

import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("bot.news_filter")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def has_valid_key():
    """ANTHROPIC_API_KEY가 실제 키처럼 보이는지 확인.
    .env.example의 플레이스홀더("여기에_Anthropic_API_키")를 그대로 두면
    빈 문자열이 아니라서 예전엔 "설정됨"으로 오판했음 — 실제 키는 영문/숫자/기호로만
    된 긴 문자열(sk-ant-...)이라, 한글이 섞여있거나 너무 짧으면 플레이스홀더로 간주."""
    key = ANTHROPIC_API_KEY
    return bool(key) and len(key) > 20 and key.isascii()

# 단순 3지선다 분류 작업이라 가볍고 빠른 모델 사용 (날짜 접미사 없는 최신 haiku 별칭)
ANTHROPIC_MODEL = "claude-haiku-4-5"
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client

NAVER_NEWS_URL = "https://finance.naver.com/item/news_news.naver?code={ticker}"
# Google News RSS — API 키 없이 검색어 기반으로 헤드라인을 받아온다 (미국/해외 종목용)
GOOGLENEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    # 네이버금융 뉴스탭은 원래 iframe으로 로드됨 — Referer 없이 직접 요청하면 빈 목록만 옴
    "Referer": "https://finance.naver.com/item/news.naver",
}

# Railway처럼 실행마다 컨테이너가 새로 뜨는 환경에서는 DATA_DIR을 영구 볼륨 경로로 지정해야
# 캐시가 다음 실행으로 이어진다 (trading_bot.py의 DATA_DIR과 동일한 규칙).
CACHE_DIR = os.path.join(os.getenv("DATA_DIR", "."), "logs")


# ═════════════════════════════════════════════════════════════════════════
#  뉴스 헤드라인 수집 — 한국(네이버금융) / 미국·해외(Google News RSS)
# ═════════════════════════════════════════════════════════════════════════
_TITLE_RE = re.compile(r'<td class="title">\s*<a[^>]*title="([^"]*)"', re.IGNORECASE)
_TITLE_RE_FALLBACK = re.compile(r'<td class="title">\s*<a[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_RSS_ITEM_TITLE_RE = re.compile(r"<item>.*?<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_RSS_SOURCE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]+$")  # Google News 제목 끝의 " - 출처명" 제거


def _clean_text(raw):
    text = html.unescape(raw)
    text = _TAG_RE.sub("", text)
    return " ".join(text.split()).strip()


def _fetch_kr_headlines(ticker, name, max_items):
    """네이버금융 종목뉴스 페이지에서 최근 헤드라인 목록을 뽑아온다."""
    try:
        url = NAVER_NEWS_URL.format(ticker=ticker)
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = "euc-kr"  # 네이버금융 구 페이지 인코딩
        if r.status_code != 200 or not r.text:
            log.warning(f"[뉴스필터] {name}({ticker}) 뉴스 페이지 응답 이상 (status={r.status_code}) — 스킵")
            return []

        titles = _TITLE_RE.findall(r.text)
        if not titles:
            titles = _TITLE_RE_FALLBACK.findall(r.text)

        headlines = []
        seen = set()
        for t in titles:
            text = _clean_text(t)
            if not text or text in seen:
                continue
            seen.add(text)
            headlines.append(text)
            if len(headlines) >= max_items:
                break
        return headlines
    except Exception as e:
        log.warning(f"[뉴스필터] {name}({ticker}) 헤드라인 수집 실패: {type(e).__name__}: {e}")
        return []


def _fetch_us_headlines(ticker, name, max_items):
    """Google News RSS에서 '{종목명} stock' 검색 결과 헤드라인을 뽑아온다.
    네이버금융과 동일하게 API 키 불필요 — 요청/파싱 실패 시 예외 없이 빈 리스트 반환."""
    try:
        query = requests.utils.quote(f"{name} stock")
        url = GOOGLENEWS_RSS_URL.format(query=query)
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200 or not r.text:
            log.warning(f"[뉴스필터] {name}({ticker}) Google News 응답 이상 (status={r.status_code}) — 스킵")
            return []

        titles = _RSS_ITEM_TITLE_RE.findall(r.text)
        headlines = []
        seen = set()
        for t in titles:
            text = _clean_text(t)
            text = _RSS_SOURCE_SUFFIX_RE.sub("", text).strip()  # "... - Reuters" 같은 출처 접미사 제거
            if not text or text in seen:
                continue
            seen.add(text)
            headlines.append(text)
            if len(headlines) >= max_items:
                break
        return headlines
    except Exception as e:
        log.warning(f"[뉴스필터] {name}({ticker}) Google News 헤드라인 수집 실패: {type(e).__name__}: {e}")
        return []


def fetch_headlines(ticker, name, market="KR", max_items=10):
    """종목의 최근 뉴스 헤드라인을 market에 맞는 소스에서 뽑아온다.
    KR=네이버금융(종목코드 기반), US=Google News RSS(종목명 검색 기반).
    페이지 구조가 바뀌거나 요청이 실패해도 절대 예외를 던지지 않고 빈 리스트를 반환한다.
    """
    if market == "US":
        return _fetch_us_headlines(ticker, name, max_items)
    return _fetch_kr_headlines(ticker, name, max_items)


# ═════════════════════════════════════════════════════════════════════════
#  Claude API 감성 분석
# ═════════════════════════════════════════════════════════════════════════
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def check_sentiment(ticker, name, headlines, market="KR"):
    """헤드라인들을 Claude에게 보내 호재/중립/악재 + 이유를 받는다.
    API 키가 없거나 호출/파싱이 실패하면 예외 없이 None을 반환한다 (fail-open).
    반환: {"verdict": "호재"|"중립"|"악재", "reason": "..."} 또는 None
    """
    if not has_valid_key():
        log.info("[뉴스필터] ANTHROPIC_API_KEY 없음/미설정 — 감성분석 스킵 (fail-open)")
        return None
    if not headlines:
        return None

    market_label = "미국(해외)" if market == "US" else "한국"
    headline_text = "\n".join(f"- {h}" for h in headlines)
    prompt = (
        f"다음은 {market_label} 주식 종목 '{name}({ticker})'의 최근 뉴스 헤드라인 목록이다"
        f"(헤드라인이 영어일 수 있다).\n\n"
        f"{headline_text}\n\n"
        "이 헤드라인들을 종합했을 때 이 종목에 대한 시장 분위기를 "
        "'호재', '중립', '악재' 중 하나로만 판단하고, 이유를 한 문장으로 한국어로 설명해줘.\n"
        "반드시 아래 JSON 형식으로만 답해. 다른 말은 절대 붙이지 마.\n"
        '{"verdict": "호재|중립|악재", "reason": "한 문장 이유"}'
    )

    try:
        response = _get_client().messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text:
            log.warning("[뉴스필터] Claude 응답에 텍스트 없음 — fail-open")
            return None

        m = _JSON_RE.search(text)
        if not m:
            log.warning(f"[뉴스필터] Claude 응답 파싱 실패 (JSON 없음): {text[:100]} — fail-open")
            return None
        parsed = json.loads(m.group(0))
        verdict = str(parsed.get("verdict", "")).strip()
        reason = str(parsed.get("reason", "")).strip()
        if verdict not in ("호재", "중립", "악재"):
            log.warning(f"[뉴스필터] Claude 응답값 이상 (verdict={verdict}) — fail-open")
            return None
        return {"verdict": verdict, "reason": reason}
    except anthropic.NotFoundError:
        log.warning(f"[뉴스필터] Claude 모델을 찾을 수 없음 (model={ANTHROPIC_MODEL}) — fail-open")
        return None
    except anthropic.AuthenticationError:
        log.warning("[뉴스필터] Claude API 키 인증 실패 — fail-open")
        return None
    except anthropic.RateLimitError:
        log.warning("[뉴스필터] Claude API 레이트리밋 초과 — fail-open")
        return None
    except anthropic.APIStatusError as e:
        log.warning(f"[뉴스필터] Claude API 응답 실패 (status={e.status_code}) — fail-open")
        return None
    except anthropic.APIConnectionError as e:
        log.warning(f"[뉴스필터] Claude API 연결 실패: {e} — fail-open")
        return None
    except Exception as e:
        log.warning(f"[뉴스필터] Claude API 호출 실패: {type(e).__name__}: {e} — fail-open")
        return None


# ═════════════════════════════════════════════════════════════════════════
#  캐시 (같은 날 같은 종목 중복 호출 방지) — OrderGuard와 동일 패턴
# ═════════════════════════════════════════════════════════════════════════
def _cache_path():
    return os.path.join(CACHE_DIR, f"news_cache_{date.today()}.json")


def _load_cache():
    path = _cache_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"[뉴스필터] 캐시 저장 실패: {e}")


# ═════════════════════════════════════════════════════════════════════════
#  공개 함수 — trading_bot.py에서 이것만 호출
# ═════════════════════════════════════════════════════════════════════════
def is_blocked(ticker, name, market="KR"):
    """뉴스 감성분석 결과 매수를 막아야 하면 (True, 사유), 아니면 (False, 사유)를 반환.
    market: "KR"(네이버금융) 또는 "US"(Google News RSS) — 소스만 다를 뿐 판단 로직은 동일.
    fail-open 원칙: 뉴스를 못 가져오거나 API 키가 없거나 API 호출이 실패하면
    절대 매수를 막지 않는다. 다만 스킵된 이유는 항상 로그로 남긴다.
    """
    cache_key = f"{market}:{ticker}"
    cache = _load_cache()
    if cache_key in cache:
        cached = cache[cache_key]
        return cached.get("blocked", False), cached.get("reason", "")

    headlines = fetch_headlines(ticker, name, market)
    if not headlines:
        reason = "최근 뉴스 헤드라인을 가져오지 못함 — 필터 통과(fail-open)"
        log.info(f"[뉴스필터] {name}({ticker}, {market}): {reason}")
        cache[cache_key] = {"blocked": False, "reason": reason}
        _save_cache(cache)
        return False, reason

    sentiment = check_sentiment(ticker, name, headlines, market)
    if sentiment is None:
        reason = "감성분석 실패/API 미설정 — 필터 통과(fail-open)"
        log.info(f"[뉴스필터] {name}({ticker}, {market}): {reason} (헤드라인 {len(headlines)}건)")
        cache[cache_key] = {"blocked": False, "reason": reason}
        _save_cache(cache)
        return False, reason

    blocked = sentiment["verdict"] == "악재"
    reason = f"{sentiment['verdict']} — {sentiment['reason']}"
    if blocked:
        log.info(f"[뉴스필터] {name}({ticker}, {market}) 매수 차단: {reason} "
                  f"(헤드라인 예: {headlines[0] if headlines else ''})")
    else:
        log.info(f"[뉴스필터] {name}({ticker}, {market}) 통과: {reason}")

    cache[cache_key] = {"blocked": blocked, "reason": reason}
    _save_cache(cache)
    return blocked, reason


# ─────────────────────────────────────────────────────────────────────────
#  단독 테스트 실행
# ─────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    test_cases = [
        ("005930", "삼성전자", "KR"),
        ("AAPL", "Apple", "US"),
    ]

    for test_ticker, test_name, test_market in test_cases:
        print("═" * 60)
        print(f"  1) 헤드라인 수집 테스트 — {test_name}({test_ticker}, {test_market})")
        print("═" * 60)
        hl = fetch_headlines(test_ticker, test_name, test_market)
        if hl:
            for h in hl:
                print(" -", h)
        else:
            print(" (헤드라인 없음 — 네트워크 문제 또는 페이지 구조 변경)")

        print("\n" + "═" * 60)
        print(f"  2) is_blocked() 종합 테스트 — {test_name}({test_ticker}, {test_market})")
        print("═" * 60)
        blocked, reason = is_blocked(test_ticker, test_name, test_market)
        print(f" blocked={blocked}")
        print(f" reason={reason}\n")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
정부기관(과학기술정보통신부/기후에너지환경부/산업통상부) 및
공기업(한국전력공사/한국수자원공사/정보통신산업진흥원/한국지능정보사회진흥원)
보도자료 게시판에서 '오늘' 등록된 글의 제목과 링크를 모아 public/index.html 을 생성한다.

원칙:
- 사이트 접속/파싱에 실패한 경우와, 접속은 됐지만 오늘 글이 정말 없는 경우를
  구분해서 표시한다. (실패를 "오늘 글 없음"으로 오인 표시하지 않는다.)
- 오늘 글이 없더라도 각 기관/기업별로 "더보기"를 펼치면 날짜와 무관하게
  가장 최근 게시물 5건을 확인할 수 있다.
- 오늘 등록된 공기업 글은 상세 페이지 본문을 가져와 Groq API로 2줄 요약해
  사이드바("올라온 자료들")에 카드 형태로 보여준다.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup, NavigableString

KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).date()
TODAY_LABEL = datetime.now(KST).strftime("%Y년 %m월 %d일")
GENERATED_AT_LABEL = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

# 전날 기준
# KST = timezone(timedelta(hours=9))
# TODAY = datetime.now(KST).date() - timedelta(days=1)
# TODAY_LABEL = (datetime.now(KST) - timedelta(days=1)).strftime("%Y년 %m월 %d일")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
TIMEOUT = 20
RETRIES = 3
RETRY_WAIT_SECONDS = 5
RECENT_LIMIT = 5

# 깃허브 액션(해외 클라우드 IP)에서 접속이 막힌 것으로 확인된 도메인 목록.
# 직접 접속이 타임아웃되면, 이 목록에 있는 도메인만 무료 공개 프록시를 거쳐 한 번 더 시도한다.
BLOCKED_FROM_CLOUD_DOMAINS = {
    "msit.go.kr", "www.msit.go.kr",
    "mcee.go.kr", "www.mcee.go.kr",
    "motir.go.kr", "www.motir.go.kr",
}

# 원본 HTML을 그대로 반환하는 "raw" 방식 무료 프록시만 사용 (요약/가공하는 프록시는
# 기존 BeautifulSoup 파싱 로직이 깨지므로 사용 불가). 순서대로 시도해서 먼저 되는 걸 씀.
PROXY_URL_TEMPLATES = [
    "https://api.allorigins.win/raw?url={url}",
    "https://corsproxy.io/?url={url}",
]

GOV_AGENCIES = ["과학기술정보통신부", "기후에너지환경부", "산업통상부", "국가인공지능전략위원회"]
PUBLIC_ENTERPRISES = ["한국전력공사", "한국수자원공사", "정보통신산업진흥원", "한국지능정보사회진흥원"]

CATEGORIES = [
    ("gov", "정부기관", GOV_AGENCIES),
    ("public", "공기업", PUBLIC_ENTERPRISES),
]

BOARD_URL = {
    "과학기술정보통신부": "https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=208&mId=307",
    "기후에너지환경부": "https://mcee.go.kr/home/web/index.do?menuId=10598",
    "산업통상부": "https://www.motir.go.kr/kor/article/ATCL3f49a5a8c",
    "국가인공지능전략위원회": "https://www.aikorea.go.kr/web/board/brdList.do?menu_cd=000012",
    "한국전력공사": "https://www.kepco.co.kr/home/media/newsroom/pr/boardList.do",
    "한국수자원공사": "https://www.kwater.or.kr/news/repoList.do?brdId=KO26&s_mid=36",
    "정보통신산업진흥원": "https://www.nipa.kr/home/4-4-1",
    "한국지능정보사회진흥원": "https://nia.or.kr/site/nia_kor/ex/bbs/List.do?cbIdx=90549",
}

# --- "지자체 관련 기사" (AI데이터센터/AIDC + 지역명) 배너용 설정 ---------------------

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
NAVER_NEWS_QUERIES = ["AI데이터센터", "AI DATA CENTER", "AIDC", "AI팩토리", "AI FACTORY"]
NAVER_DISPLAY = 100
NAVER_MAX_PAGES = 3  # 쿼리당 최대 100 x 3 = 300건 후보 확보
AIDC_TOP_N = 20

# --- "국내 기업 관련 기사" (AI데이터센터/AIDC + 국내 기업명) 배너용 설정 -----------------

COMPANY_NAMES_PATH = os.path.join("data", "company_names.txt")
COMPANY_TOP_N = 20


def load_company_names():
    if not os.path.exists(COMPANY_NAMES_PATH):
        log(f"[WARN] {COMPANY_NAMES_PATH} not found - 국내 기업 목록 없이 진행합니다")
        return []
    with open(COMPANY_NAMES_PATH, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip()]
    # 짧은 이름이 긴 이름의 부분 문자열인 경우(예: "이노텍" vs "LG이노텍")
    # 더 구체적인(긴) 이름이 먼저 매칭되도록 길이 내림차순 정렬
    names.sort(key=len, reverse=True)
    return names


COMPANY_NAMES = load_company_names()


_company_names_norm_cache = None


def find_company_in_text(text: str):
    """대소문자·띄어쓰기 구분 없이 국내 기업명을 찾는다."""
    global _company_names_norm_cache
    if _company_names_norm_cache is None:
        _company_names_norm_cache = [(normalize_for_match(n), n) for n in COMPANY_NAMES]
    norm_text = normalize_for_match(text)
    for norm_name, name in _company_names_norm_cache:
        if norm_name in norm_text:
            return name
    return None


# --- "해외 기업 관련 기사" (AI데이터센터/AIDC/AI팩토리 + 해외 기업명, 네이버 검색) --------

OVERSEAS_NAVER_QUERIES = ["AI데이터센터", "AI DATA CENTER", "AIDC", "AI팩토리", "AI FACTORY"]
OVERSEAS_TOP_N = 20

# 키워드 정규화 매칭용(띄어쓰기/대소문자 무시)
OVERSEAS_KEYWORDS_NORM = ["ai데이터센터", "aidatacenter", "aidc", "ai팩토리", "aifactory"]

OVERSEAS_COMPANY_NAMES_PATH = os.path.join("data", "company_names_overseas.txt")


def load_overseas_company_names():
    if not os.path.exists(OVERSEAS_COMPANY_NAMES_PATH):
        log(f"[WARN] {OVERSEAS_COMPANY_NAMES_PATH} not found - 해외 기업 목록 없이 진행합니다")
        return []
    with open(OVERSEAS_COMPANY_NAMES_PATH, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip()]
    # 짧은 이름이 긴 이름의 부분 문자열인 경우를 대비해 긴 이름을 먼저 매칭
    names.sort(key=len, reverse=True)
    return names


OVERSEAS_COMPANY_NAMES = load_overseas_company_names()


_overseas_company_names_norm_cache = None


def find_overseas_company_in_text(text: str):
    """대소문자·띄어쓰기 구분 없이 해외 기업명을 찾는다."""
    global _overseas_company_names_norm_cache
    if _overseas_company_names_norm_cache is None:
        _overseas_company_names_norm_cache = [(normalize_for_match(n), n) for n in OVERSEAS_COMPANY_NAMES]
    norm_text = normalize_for_match(text)
    for norm_name, name in _overseas_company_names_norm_cache:
        if norm_name in norm_text:
            return name
    return None


def fetch_overseas_company_news(cache: dict):
    """'AI데이터센터'/'AI DATA CENTER'/'AIDC'/'AI팩토리'/'AI FACTORY' + 해외 기업명이
    함께 언급된 기사 상위 20건 (네이버 뉴스 검색). 반환값: (items, ok)."""
    seen_links = set()
    candidates = []
    any_ok = False

    for query in OVERSEAS_NAVER_QUERIES:
        raw_items = fetch_naver_news_raw(query)
        if raw_items is None:
            continue
        any_ok = True

        for it in raw_items:
            link = it.get("originallink") or it.get("link") or ""
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            title = clean_naver_text(it.get("title", ""))
            desc = clean_naver_text(it.get("description", ""))
            if not title:
                continue

            combined_norm = normalize_for_match(title + " " + desc)
            if not any(k in combined_norm for k in OVERSEAS_KEYWORDS_NORM):
                continue

            company = find_overseas_company_in_text(title) or find_overseas_company_in_text(desc)
            if not company:
                continue

            pub_dt = None
            try:
                pub_dt = parsedate_to_datetime(it.get("pubDate", ""))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=KST)
                pub_dt = pub_dt.astimezone(KST)
            except (TypeError, ValueError):
                pub_dt = None

            display_link = it.get("link") or link
            candidates.append({
                "title": title,
                "summary": desc,
                "link": display_link,
                "press": press_name_from_link(link),
                "pub_dt": pub_dt,
                "company": company,
            })

    if not any_ok:
        log("[SUMMARY] OVERSEAS: FETCH FAILED")
        return [], False

    candidates.sort(key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=KST), reverse=True)
    top = candidates[:AIDC_TOP_N]
    maybe_summarize_top_items(cache, top, "AIDC")
    log(f"[SUMMARY] AIDC: {len(candidates)}건 매칭, 상위 {len(top)}건 표시")
    return top, True


REGION_NAMES_PATH = os.path.join("data", "region_names.txt")


def load_region_names():
    if not os.path.exists(REGION_NAMES_PATH):
        log(f"[WARN] {REGION_NAMES_PATH} not found - 지역명 목록 없이 진행합니다")
        return []
    with open(REGION_NAMES_PATH, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip()]
    # 짧은 이름이 긴 이름의 부분 문자열인 경우를 대비해 긴 이름을 먼저 매칭
    names.sort(key=len, reverse=True)
    return names


REGION_NAMES = load_region_names()

# 파일 형식: 한 줄에 "도메인,언론사명"
PRESS_DOMAIN_MAP_PATH = os.path.join("data", "press_domain_map.txt")


def load_press_domain_map():
    if not os.path.exists(PRESS_DOMAIN_MAP_PATH):
        log(f"[WARN] {PRESS_DOMAIN_MAP_PATH} not found - 언론사 매핑 없이 진행합니다")
        return {}
    mapping = {}
    with open(PRESS_DOMAIN_MAP_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "," not in line:
                continue
            domain, name = line.split(",", 1)
            mapping[domain.strip()] = name.strip()
    return mapping


PRESS_DOMAIN_MAP = load_press_domain_map()

# --- 오늘 등록된 공기업 글 AI 요약 (Groq API) 설정 -----------------------------------

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"  # 무료 티어에서 요약 품질 괜찮은 모델
SUMMARY_MAX_BODY_CHARS = 3000  # 본문이 너무 길면 이만큼만 잘라서 요약 요청

# --- 웹페이지 우측 AI 채팅창 (Cloudflare Worker 프록시) 설정 ---------------------------
# 사용자가 "AI 요약하기" 버튼을 누르거나 PDF를 첨부하면 이 Worker를 통해 즉석 요약한다.
AI_CHAT_WORKER_URL = "https://ai-summary.wkddlsrjs.workers.dev"

# 상세 페이지에서 본문을 찾을 때 우선 시도하는 CSS 선택자
_BODY_SELECTORS = [
    "div.view_cont", "div.board_view", "div.bbs_view", "div.view-content",
    "div.viewCont", "div.cont_view", "div.detail_cont", "article",
    "div.content", "td.view_cont",
]


def fetch_detail_text(url: str, label: str) -> str:
    """상세 페이지에서 본문 텍스트를 최대한 추출한다. 실패하면 빈 문자열 반환."""
    resp = fetch(url, f"{label}-detail")
    if resp is None:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    body_el = None
    for sel in _BODY_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 50:
            body_el = el
            break

    text = body_el.get_text(" ", strip=True) if body_el else soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SUMMARY_MAX_BODY_CHARS]


def summarize_with_groq(title: str, body_text: str) -> str:
    """제목+본문을 2줄 정도로 요약. 실패하면 빈 문자열 반환(빈 값이면 화면에서 제목만 표시)."""
    if not GROQ_API_KEY:
        return ""
    if not body_text:
        body_text = title  # 본문 추출 실패 시 제목만이라도 넘김

    prompt = (
        "다음은 보도자료 제목과 본문입니다. 핵심 내용을 한국어로 2줄 이내, "
        "합쳐서 100자 내외로 간결하게 요약해줘. 불필요한 수식어나 설명 없이 "
        "요약 문장만 출력해.\n\n"
        f"제목: {title}\n\n본문: {body_text}"
    )

    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
        return summary
    except Exception as e:
        log(f"[WARN] Groq summarize failed for '{title[:30]}...': {e}")
        return ""


# 외부 뉴스 기사(지자체/국내 기업/해외 기업 탭) 본문 추출 시 우선 시도하는 CSS 선택자.
# 언론사마다 마크업이 제각각이라 흔히 쓰이는 클래스/아이디 패턴을 폭넓게 시도한다.
_ARTICLE_BODY_SELECTORS = [
    "div#articleBody", "div#article-view-content-div", "div.article_body",
    "div.article-body", "div.news_body", "div.article_txt", "div.article-txt",
    "div#article_body", "div#news_body_area", "div.art_body", "article",
    "div.view_cont", "div.content_view",
]


def fetch_article_text(url: str, label: str) -> str:
    """뉴스 기사 상세 페이지에서 본문 텍스트를 최대한 추출한다. 실패하면 빈 문자열 반환."""
    resp = fetch(url, f"{label}-article")
    if resp is None:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
        tag.decompose()

    body_el = None
    for sel in _ARTICLE_BODY_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 80:
            body_el = el
            break

    text = body_el.get_text(" ", strip=True) if body_el else soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SUMMARY_MAX_BODY_CHARS]


def summarize_article_with_groq(title: str, body_text: str) -> str:
    """뉴스 기사 제목+본문을 3~4문장(150자 이상)으로 요약. 실패하면 빈 문자열 반환."""
    if not GROQ_API_KEY:
        return ""
    if not body_text:
        body_text = title  # 본문 추출 실패 시 제목만이라도 넘김

    prompt = (
        "다음은 뉴스 기사 제목과 본문입니다. 핵심 내용을 한국어로 3~4문장, "
        "150자 이상 250자 이내로 요약해줘. 불필요한 수식어나 설명 없이 "
        "요약 문장만 출력해.\n\n"
        f"제목: {title}\n\n본문: {body_text}"
    )

    try:
        resp = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
        return summary
    except Exception as e:
        log(f"[WARN] Groq article summarize failed for '{title[:30]}...': {e}")
        return ""


# --- 요약 캐시: 한 번 생성한 AI 요약은 링크를 키로 저장해두고, 다음 실행부터는
# 재사용한다(같은 기사가 여러 날 "최신 20건"에 계속 걸리는 경우 중복 요약/과금 방지).
# "short"는 "올라온 자료들"(공기업 오늘 글) 2줄 요약, "long"은 지자체/국내·해외 기업
# 기사 탭의 3~4문장 요약을 각각 따로 저장한다.
SUMMARY_CACHE_PATH = os.path.join("data", "summary_cache.json")


def load_summary_cache() -> dict:
    if not os.path.exists(SUMMARY_CACHE_PATH):
        log(f"[INFO] {SUMMARY_CACHE_PATH} 없음 - 빈 캐시로 시작합니다")
        return {"short": {}, "long": {}}
    try:
        with open(SUMMARY_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("short", {})
        data.setdefault("long", {})
        log(f"[INFO] 요약 캐시 로드: short {len(data['short'])}건, long {len(data['long'])}건")
        return data
    except Exception as e:
        log(f"[WARN] {SUMMARY_CACHE_PATH} 읽기 실패, 빈 캐시로 시작합니다: {e}")
        return {"short": {}, "long": {}}


def save_summary_cache(cache: dict) -> None:
    os.makedirs("data", exist_ok=True)
    try:
        with open(SUMMARY_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
        log(f"[INFO] 요약 캐시 저장: short {len(cache['short'])}건, long {len(cache['long'])}건")
    except Exception as e:
        log(f"[WARN] {SUMMARY_CACHE_PATH} 저장 실패: {e}")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _direct_fetch(url: str, label: str):
    """직접 접속 시도. 성공하면 Response, 실패하면 None."""
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            log(f"[OK] {label}: attempt {attempt} -> status {resp.status_code}, "
                f"{len(resp.text)} chars")
            return resp
        except requests.RequestException as e:
            last_err = e
            log(f"[WARN] {label}: attempt {attempt} failed: {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT_SECONDS)
    log(f"[ERROR] {label}: all {RETRIES} direct attempts failed ({last_err})")
    return None


def _proxy_fetch(url: str, label: str):
    """직접 접속이 막힌 도메인용: 무료 공개 프록시를 순서대로 시도한다."""
    encoded = quote(url, safe="")
    for template in PROXY_URL_TEMPLATES:
        proxy_url = template.format(url=encoded)
        proxy_name = proxy_url.split("?")[0]
        try:
            resp = requests.get(proxy_url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            if not resp.text or len(resp.text) < 200:
                log(f"[WARN] {label}: proxy({proxy_name}) 응답이 비정상적으로 짧음, 다음 프록시 시도")
                continue
            resp.encoding = resp.apparent_encoding or "utf-8"
            log(f"[OK] {label}: proxy({proxy_name}) 경유 성공, {len(resp.text)} chars")
            return resp
        except requests.RequestException as e:
            log(f"[WARN] {label}: proxy({proxy_name}) 실패: {e}")
            continue
    log(f"[ERROR] {label}: 모든 프록시 시도 실패")
    return None


def fetch(url: str, label: str):
    """직접 접속을 먼저 시도하고, 실패했는데 해당 도메인이 '클라우드에서 차단된 것으로
    확인된 목록'에 있으면 무료 프록시를 통해 한 번 더 시도한다."""
    resp = _direct_fetch(url, label)
    if resp is not None:
        return resp

    domain = urlparse(url).netloc
    if domain in BLOCKED_FROM_CLOUD_DOMAINS:
        log(f"[INFO] {label}: {domain}은 클라우드 차단 목록에 있어 프록시로 재시도합니다")
        return _proxy_fetch(url, label)

    return None


def parse_date_flexible(text: str):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST).date()
    except (TypeError, ValueError):
        pass
    m = re.search(r"(\d{4})[.\-/년\s](\d{1,2})[.\-/월\s](\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def extract_date_text(text: str) -> str:
    m = re.search(r"\d{4}[.\-]\d{1,2}[.\-]\d{1,2}", text)
    return m.group(0) if m else ""


def fetch_msit():
    """RSS 피드로 수집 (게시판과 동일한 목록을 구조화된 형태로 제공)."""
    url = "https://www.msit.go.kr/user/rss/rss.do?bbsSeqNo=94"
    resp = fetch(url, "MSIT")
    if resp is None:
        return None

    try:
        soup = BeautifulSoup(resp.text, "xml")
    except Exception as e:
        log(f"[ERROR] MSIT: XML parse failed: {e}")
        return None

    items = []
    for item in soup.find_all("item"):
        title = (item.title.get_text(strip=True) if item.title else "")
        link = (item.link.get_text(strip=True) if item.link else "")
        pub_date_raw = (item.pubDate.get_text(strip=True) if item.pubDate else "")
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "date": parse_date_flexible(pub_date_raw),
            "date_raw": pub_date_raw,
        })
    log(f"[INFO] MSIT: {len(items)} items parsed from RSS")
    return items or None


def fetch_mcee():
    url = BOARD_URL["기후에너지환경부"]
    resp = fetch(url, "MCEE")
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.find_all("tr")
    if not rows:
        rows = soup.find_all("li")
    log(f"[INFO] MCEE: {len(rows)} candidate rows found")

    items = []
    seen = set()
    for row in rows:
        a = row.find("a", href=lambda h: h and "boardId=" in h)
        if a is None:
            continue
        m = re.search(r"boardId=(\d+)", a["href"])
        if not m:
            continue
        board_id = m.group(1)
        if board_id in seen:
            continue
        seen.add(board_id)

        title = a.get("title") or a.get_text(strip=True)
        title = title.strip()
        if not title:
            continue

        date_text = extract_date_text(row.get_text(" ", strip=True))

        detail_url = (
            f"https://mcee.go.kr/home/web/board/read.do"
            f"?menuId=10598&boardMasterId=939&boardId={board_id}"
        )
        items.append({
            "title": title,
            "link": detail_url,
            "date": parse_date_flexible(date_text),
            "date_raw": date_text,
        })
    log(f"[INFO] MCEE: {len(items)} items parsed")
    return items or None


def fetch_motir():
    board_code = "ATCL3f49a5a8c"
    url = BOARD_URL["산업통상부"]
    resp = fetch(url, "MOTIR")
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.find_all("tr")
    log(f"[INFO] MOTIR: {len(rows)} candidate rows found")

    id_pattern = re.compile(
        r"article\.view\(\s*['\"]?(\d+)['\"]?\s*\)|" + re.escape(board_code) + r"/(\d+)/view"
    )

    items = []
    seen = set()
    for row in rows:
        row_html = str(row)
        m = id_pattern.search(row_html)
        if not m:
            continue
        article_id = m.group(1) or m.group(2)
        if article_id in seen:
            continue
        seen.add(article_id)

        a = row.find("a")
        title = a.get_text(strip=True) if a else ""
        if not title:
            continue

        date_text = extract_date_text(row.get_text(" ", strip=True))

        detail_url = f"https://www.motir.go.kr/kor/article/{board_code}/{article_id}/view"
        items.append({
            "title": title,
            "link": detail_url,
            "date": parse_date_flexible(date_text),
            "date_raw": date_text,
        })
    log(f"[INFO] MOTIR: {len(items)} items parsed")
    return items or None


# 제목으로 취급하지 않을 게시판 UI 텍스트(페이지네이션, 버튼 등)
_TITLE_BLACKLIST = {"이전", "다음", "처음", "마지막", "목록", "검색", "글쓰기", "인쇄", "공유", "리스트"}

# 실제 게시글 목록 테이블을 찾기 위해 우선순위대로 시도하는 CSS 선택자
_TABLE_SELECTORS = [
    "table.bdListTbl", "table.board_list", "table.bbs_list", "table.tbl_list",
    "table.boardList", ".board-list table", ".bbsList table", ".board_list table",
    "table",
]
_LIST_SELECTORS = [
    "ul.board_list li", "ul.bbs_list li", "div.board_list li", ".board-list li",
]


def fetch_generic_board(url: str, label: str):
    """구조를 미리 알 수 없는 게시판 공용 파서.
    표(table) 기반 목록을 우선 시도하고, 없으면 리스트(ul/li) 형태를 시도한다.
    행 안의 첫 <a href> 를 게시글 링크/제목으로, 행 텍스트에서 날짜 패턴을 찾는다."""
    resp = fetch(url, label)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for sel in _TABLE_SELECTORS:
        table = soup.select_one(sel)
        if table:
            candidate = table.find_all("tr")
            if len(candidate) > 1:
                rows = candidate
                break
    if not rows:
        for sel in _LIST_SELECTORS:
            candidate = soup.select(sel)
            if len(candidate) > 1:
                rows = candidate
                break

    log(f"[INFO] {label}: {len(rows)} candidate rows found")
    if not rows:
        log(f"[ERROR] {label}: 게시판 구조를 인식하지 못했습니다")
        return None

    items = []
    seen = set()
    for row in rows:
        if row.find("th") and not row.find("td"):
            continue  # 헤더 행

        a = row.find("a", href=True)
        if a is None:
            continue
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        title = a.get("title") or a.get_text(strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 4 or title in _TITLE_BLACKLIST:
            continue

        full_link = urljoin(url, href)
        if full_link in seen:
            continue
        seen.add(full_link)

        date_text = extract_date_text(row.get_text(" ", strip=True))
        items.append({
            "title": title,
            "link": full_link,
            "date": parse_date_flexible(date_text),
            "date_raw": date_text,
        })

    log(f"[INFO] {label}: {len(items)} items parsed")
    return items or None


def fetch_kepco():
    """카드형 목록(div.media-list-item). 상세는 JS(fn_Detail)로 폼 제출하는 방식이라
    실제 상세 URL을 GET 파라미터로 구성해서 링크를 만든다."""
    url = BOARD_URL["한국전력공사"]
    resp = fetch(url, "KEPCO")
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("div.media-list-item")
    log(f"[INFO] KEPCO: {len(cards)} candidate rows found")

    pattern = re.compile(r"fn_Detail\('(\d+)','(\d+)'\)")
    items = []
    seen = set()
    for card in cards:
        a = card.find("a", href=True)
        if a is None:
            continue
        m = pattern.search(a["href"])
        if not m:
            continue
        board_mng_no, board_no = m.groups()
        key = (board_mng_no, board_no)
        if key in seen:
            continue
        seen.add(key)

        title_el = card.find("strong", class_="tit")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue

        date_el = card.find("span", class_="date")
        date_text = date_el.get_text(strip=True) if date_el else ""

        detail_url = (
            f"https://www.kepco.co.kr/home/media/newsroom/pr/boardView.do"
            f"?boardMngNo={board_mng_no}&boardNo={board_no}"
        )
        items.append({
            "title": title,
            "link": detail_url,
            "date": parse_date_flexible(date_text),
            "date_raw": date_text,
        })
    log(f"[INFO] KEPCO: {len(items)} items parsed")
    return items or None


def fetch_aikorea():
    url = BOARD_URL["국가인공지능전략위원회"]
    resp = fetch(url, "AIKOREA")
    if resp is None:
        return None

    try:
        data = resp.json()
    except ValueError as e:
        log(f"[ERROR] AIKOREA: JSON parse failed: {e}")
        return None

    rows = data.get("brdList", [])
    log(f"[INFO] AIKOREA: {len(rows)} candidate rows found")

    items = []
    for row in rows:
        num = row.get("num")
        title = (row.get("title") or "").strip()
        if not num or not title:
            continue

        date_text = row.get("write_dt") or row.get("disp_write_dt") or ""

        detail_url = f"https://www.aikorea.go.kr/web/board/brdDetail.do?menu_cd=000012&num={num}"

        items.append({
            "title": title,
            "link": detail_url,
            "date": parse_date_flexible(date_text),
            "date_raw": date_text,
        })

    log(f"[INFO] AIKOREA: {len(items)} items parsed")
    return items or None


def fetch_kwater():
    return fetch_generic_board(BOARD_URL["한국수자원공사"], "K-water")


def fetch_nipa():
    return fetch_generic_board(BOARD_URL["정보통신산업진흥원"], "NIPA")


def fetch_nia():
    """div.board_type01 안의 li 목록. 상세는 JS(doBbsFView)로 폼 제출하는 방식이라
    실제 View.do 상세 URL을 GET 파라미터로 구성해서 링크를 만든다."""
    url = BOARD_URL["한국지능정보사회진흥원"]
    resp = fetch(url, "NIA")
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find("div", class_="board_type01")
    rows = container.find_all("li") if container else []
    log(f"[INFO] NIA: {len(rows)} candidate rows found")

    pattern = re.compile(r"doBbsFView\('(\d+)','(\d+)','(\d+)','(\d+)'\)")
    items = []
    seen = set()
    for row in rows:
        a = row.find("a", href=True)
        if a is None:
            continue
        m = pattern.search(a.get("onclick", ""))
        if not m:
            continue
        cb_idx, bc_idx, _gbn, parent_seq = m.groups()
        key = (cb_idx, bc_idx)
        if key in seen:
            continue
        seen.add(key)

        subject = row.find("span", class_="subject")
        title = ""
        if subject:
            title = "".join(
                c for c in subject.contents if isinstance(c, NavigableString)
            ).strip()
        if not title:
            continue

        date_text = ""
        src = row.find("span", class_="src")
        if src:
            date_text = extract_date_text(src.get_text(" ", strip=True))

        detail_url = (
            f"https://nia.or.kr/site/nia_kor/ex/bbs/View.do"
            f"?cbIdx={cb_idx}&bcIdx={bc_idx}&parentSeq={parent_seq}"
        )
        items.append({
            "title": title,
            "link": detail_url,
            "date": parse_date_flexible(date_text),
            "date_raw": date_text,
        })
    log(f"[INFO] NIA: {len(items)} items parsed")
    return items or None


_TAG_RE = re.compile(r"<[^>]+>")


def clean_naver_text(text: str) -> str:
    return unescape(_TAG_RE.sub("", text or "")).strip()


def normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def find_region_in_title(title: str):
    for name in REGION_NAMES:
        if name in title:
            return name
    return None


def press_name_from_link(link: str) -> str:
    try:
        host = urlparse(link).netloc
    except ValueError:
        return ""
    host = re.sub(r"^www\.", "", host)
    for domain, name in PRESS_DOMAIN_MAP.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def fetch_naver_news_raw(query: str):
    """네이버 뉴스 검색 API 호출. 실패하면 None, 성공하면 items 리스트(빈 리스트 가능)."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        log("[ERROR] NAVER: NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 환경변수가 설정되지 않았습니다")
        return None

    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    all_items = []
    for page in range(NAVER_MAX_PAGES):
        start = page * NAVER_DISPLAY + 1
        params = {"query": query, "display": NAVER_DISPLAY, "start": start, "sort": "date"}

        data = None
        last_err = None
        for attempt in range(1, RETRIES + 1):
            try:
                resp = requests.get(NAVER_NEWS_URL, headers=headers, params=params, timeout=TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                last_err = e
                log(f"[WARN] NAVER[{query}] page{page + 1} attempt {attempt} failed: {e}")
                if attempt < RETRIES:
                    time.sleep(RETRY_WAIT_SECONDS)

        if data is None:
            log(f"[ERROR] NAVER[{query}] page{page + 1}: all attempts failed ({last_err})")
            return all_items if all_items else None

        items = data.get("items", [])
        all_items.extend(items)
        if len(items) < NAVER_DISPLAY:
            break

    log(f"[INFO] NAVER[{query}]: {len(all_items)} raw items fetched")
    return all_items


def fetch_aidc_news(cache: dict):
    """'AI데이터센터'/'AIDC' + 지자체 지역명이 제목에 함께 언급된 기사 상위 20건.
    반환값: (items, ok). ok=False면 API 호출 자체가 실패한 것(진짜로 0건인 것과 구분)."""
    seen_links = set()
    candidates = []
    any_ok = False

    for query in NAVER_NEWS_QUERIES:
        raw_items = fetch_naver_news_raw(query)
        if raw_items is None:
            continue
        any_ok = True

        for it in raw_items:
            link = it.get("originallink") or it.get("link") or ""
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            title = clean_naver_text(it.get("title", ""))
            desc = clean_naver_text(it.get("description", ""))
            if not title:
                continue

            combined_norm = normalize_for_match(title + " " + desc)
            if not any(k in combined_norm for k in ["ai데이터센터", "aidatacenter", "aidc", "ai팩토리", "aifactory"]):
                continue

            region = find_region_in_title(title)
            if not region:
                continue

            pub_dt = None
            try:
                pub_dt = parsedate_to_datetime(it.get("pubDate", ""))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=KST)
                pub_dt = pub_dt.astimezone(KST)
            except (TypeError, ValueError):
                pub_dt = None

            display_link = it.get("link") or link
            candidates.append({
                "title": title,
                "summary": desc,
                "link": display_link,
                "press": press_name_from_link(link),
                "pub_dt": pub_dt,
                "region": region,
            })

    if not any_ok:
        log("[SUMMARY] AIDC: FETCH FAILED")
        return [], False

    candidates.sort(key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=KST), reverse=True)
    top = candidates[:COMPANY_TOP_N]
    maybe_summarize_top_items(cache, top, "LISTED")
    log(f"[SUMMARY] LISTED: {len(candidates)}건 매칭, 상위 {len(top)}건 표시")
    return top, True


def fetch_listed_company_news(cache: dict):
    """'AI데이터센터'/'AIDC' + 국내 기업명이 함께 언급된 기사 상위 20건.
    반환값: (items, ok). ok=False면 API 호출 자체가 실패한 것(진짜로 0건인 것과 구분)."""
    seen_links = set()
    candidates = []
    any_ok = False

    for query in NAVER_NEWS_QUERIES:
        raw_items = fetch_naver_news_raw(query)
        if raw_items is None:
            continue
        any_ok = True

        for it in raw_items:
            link = it.get("originallink") or it.get("link") or ""
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            title = clean_naver_text(it.get("title", ""))
            desc = clean_naver_text(it.get("description", ""))
            if not title:
                continue

            combined_norm = normalize_for_match(title + " " + desc)
            if not any(k in combined_norm for k in ["ai데이터센터", "aidatacenter", "aidc", "ai팩토리", "aifactory"]):
                continue

            company = find_company_in_text(title) or find_company_in_text(desc)
            if not company:
                continue

            pub_dt = None
            try:
                pub_dt = parsedate_to_datetime(it.get("pubDate", ""))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=KST)
                pub_dt = pub_dt.astimezone(KST)
            except (TypeError, ValueError):
                pub_dt = None

            display_link = it.get("link") or link
            candidates.append({
                "title": title,
                "summary": desc,
                "link": display_link,
                "press": press_name_from_link(link),
                "pub_dt": pub_dt,
                "company": company,
            })

    if not any_ok:
        log("[SUMMARY] LISTED: FETCH FAILED")
        return [], False

    candidates.sort(key=lambda x: x["pub_dt"] or datetime.min.replace(tzinfo=KST), reverse=True)
    top = candidates[:OVERSEAS_TOP_N]
    maybe_summarize_top_items(cache, top, "OVERSEAS")
    log(f"[SUMMARY] OVERSEAS: {len(candidates)}건 매칭, 상위 {len(top)}건 표시")
    return top, True


GOV_FETCHERS = {
    "과학기술정보통신부": fetch_msit,
    "기후에너지환경부": fetch_mcee,
    "산업통상부": fetch_motir,
    "국가인공지능전략위원회": fetch_aikorea,
}

PUBLIC_FETCHERS = {
    "한국전력공사": fetch_kepco,
    "한국수자원공사": fetch_kwater,
    "정보통신산업진흥원": fetch_nipa,
    "한국지능정보사회진흥원": fetch_nia,
}

FETCHERS = {**GOV_FETCHERS, **PUBLIC_FETCHERS}
# 테스트 중에는 False로 두면 Groq API 호출(본문 다운로드 + 요약)을 전부 건너뛰어서
# 실행 시간이 크게 줄어듭니다. 나중에 실제 배포할 때 True로 바꾸세요.
ENABLE_AI_SUMMARIES = os.environ.get("ENABLE_AI_SUMMARIES", "false").lower() == "true"


def maybe_summarize_top_items(cache: dict, top: list, label: str) -> None:
    """ENABLE_AI_SUMMARIES가 False면 요약을 건너뛰고, True일 때만 기존처럼 동작한다."""
    if not ENABLE_AI_SUMMARIES:
        for it in top:
            it["ai_summary"] = ""
        return

    new_summaries = 0
    for it in top:
        cached = cache["long"].get(it["link"])
        if cached:
            it["ai_summary"] = cached
            continue
        article_text = fetch_article_text(it["link"], label)
        it["ai_summary"] = summarize_article_with_groq(it["title"], article_text)
        if it["ai_summary"]:
            cache["long"][it["link"]] = it["ai_summary"]
            new_summaries += 1
    log(f"[SUMMARY] {label}: 신규 요약 {new_summaries}건")

def render_html(today_items: dict, recent_items: dict, fetch_failed: set,
                 aidc_items: list, aidc_ok: bool,
                 listed_items: list, listed_ok: bool,
                 overseas_items: list, overseas_ok: bool) -> str:
    def date_label(item: dict) -> str:
        if item.get("date"):
            return item["date"].strftime("%Y-%m-%d")
        return item.get("date_raw") or "-"

    def item_li(item: dict) -> str:
        return (
            f'<li><a href="{escape(item["link"])}" target="_blank" rel="noopener">'
            f'{escape(item["title"])}</a><span class="date">{escape(date_label(item))}</span></li>'
        )

    def org_section(org: str) -> str:
        today = today_items.get(org, [])
        recent = recent_items.get(org, [])

        if org in fetch_failed:
            body = '<p class="msg fail">사이트 접속에 실패하여 확인하지 못했습니다. 다음 자동 실행에서 다시 시도합니다.</p>'
        elif today:
            body = "<ul>" + "\n".join(item_li(it) for it in today) + "</ul>"
        else:
            body = '<p class="msg empty">등록된 보도자료 없음</p>'

        more_html = ""
        if org not in fetch_failed and recent:
            more_html = f"""
      <details class="more">
        <summary>더보기 (최근 {len(recent)}건)</summary>
        <ul>{"".join(item_li(it) for it in recent)}</ul>
      </details>"""

        return f"""
    <section class="agency">
      <h2><a href="{escape(BOARD_URL[org])}" target="_blank" rel="noopener">{escape(org)}</a></h2>
      {body}{more_html}
    </section>"""

    def news_li(item: dict, tag_key: str) -> str:
        pub_label = item["pub_dt"].strftime("%Y-%m-%d %H:%M") if item.get("pub_dt") else "-"
        ai_summary = item.get("ai_summary")
        ai_summary_html = ""
        if ai_summary:
            ai_summary_html = f"""
        <details class="ai-summary">
          <summary>AI 요약보기</summary>
          <p>{escape(ai_summary)}</p>
        </details>"""
        return f"""
      <li class="news-item">
        <a class="news-title" href="{escape(item['link'])}" target="_blank" rel="noopener">{escape(item['title'])}</a>
        <p class="news-summary">{escape(item['summary'])}</p>
        <p class="news-meta">
          <span class="press">{escape(item['press'])}</span> ·
          <span class="pubdate">{escape(pub_label)}</span> ·
          <span class="region-tag">{escape(item[tag_key])}</span> ·
          <button type="button" class="ai-chat-btn" onclick="aiSummarizeUrl('{escape(item['link'])}')">AI 요약하기</button>
        </p>{ai_summary_html}
      </li>"""

    def aidc_panel() -> str:
        if not aidc_ok:
            body = '<p class="msg fail">뉴스 검색에 실패하여 확인하지 못했습니다. 다음 자동 실행에서 다시 시도합니다.</p>'
        elif aidc_items:
            body = "<ul class=\"news-list\">" + "".join(news_li(it, "region") for it in aidc_items) + "</ul>"
        else:
            body = '<p class="msg empty">조건에 맞는 기사가 없습니다</p>'
        return f"""
    <section class="agency">
      <h2>AI데이터센터(AIDC) · 지자체 언급 기사</h2>
      <p class="section-desc">네이버 뉴스에서 국내 지역명이 언급된 기사 중 최신 {AIDC_TOP_N}건</p>
      {body}
    </section>"""

    def listed_panel() -> str:
        if not listed_ok:
            body = '<p class="msg fail">뉴스 검색에 실패하여 확인하지 못했습니다. 다음 자동 실행에서 다시 시도합니다.</p>'
        elif listed_items:
            body = "<ul class=\"news-list\">" + "".join(news_li(it, "company") for it in listed_items) + "</ul>"
        else:
            body = '<p class="msg empty">조건에 맞는 기사가 없습니다</p>'
        return f"""
    <section class="agency">
      <h2>AI데이터센터(AIDC) · 국내 기업 언급 기사</h2>
      <p class="section-desc">네이버 뉴스에서 국내 기업명이 언급된 기사 중 최신 {COMPANY_TOP_N}건</p>
      {body}
    </section>"""

    def overseas_panel() -> str:
        if not overseas_ok:
            body = '<p class="msg fail">뉴스 검색에 실패하여 확인하지 못했습니다. 다음 자동 실행에서 다시 시도합니다.</p>'
        elif overseas_items:
            body = "<ul class=\"news-list\">" + "".join(news_li(it, "company") for it in overseas_items) + "</ul>"
        else:
            body = '<p class="msg empty">조건에 맞는 기사가 없습니다</p>'
        return f"""
    <section class="agency">
      <h2>AI데이터센터(AIDC) · 해외 기업 언급 기사</h2>
      <p class="section-desc">네이버 뉴스에서 해외 기업명이 언급된 기사 중 최신 {OVERSEAS_TOP_N}건</p>
      {body}
    </section>"""

    def highlights_html() -> str:
        rows = []
        for org in GOV_AGENCIES + PUBLIC_ENTERPRISES:
            for it in today_items.get(org, []):
                rows.append((org, it))

        if not rows:
            body = '<p class="msg empty">등록된 자료가 아직 없습니다.</p>'
        else:
            cards = []
            for org, it in rows:
                summary_text = it.get("summary")
                summary_html = ""
                if summary_text:
                    summary_html = f"""
        <details class="ai-summary">
          <summary>AI 요약보기</summary>
          <p>{escape(summary_text)}</p>
        </details>"""
                cards.append(f"""
      <div class="highlight-card">
        <div class="highlight-company">{escape(org)}</div>
        <a class="highlight-title" href="{escape(it['link'])}" target="_blank" rel="noopener">{escape(it['title'])}</a>{summary_html}
      </div>""")
            body = "".join(cards)

        return f"""
  <section class="highlights">
    <h2>올라온 자료들</h2>
    {body}
  </section>"""

    tab_buttons = []
    tab_panels = []
    for i, (key, label, orgs) in enumerate(CATEGORIES):
        active = " active" if i == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn{active}" data-tab="{key}" onclick="showTab(\'{key}\')">{escape(label)}</button>'
        )
        sections_html = "\n".join(org_section(org) for org in orgs)
        tab_panels.append(f'<div id="tab-{key}" class="tab-panel{active}">{sections_html}\n    </div>')

    tab_buttons.append(
        '<button class="tab-btn" data-tab="local" onclick="showTab(\'local\')">지자체 관련 기사</button>'
    )
    tab_panels.append(f'<div id="tab-local" class="tab-panel">{aidc_panel()}\n    </div>')

    tab_buttons.append(
        '<button class="tab-btn" data-tab="listed" onclick="showTab(\'listed\')">국내 기업 관련 기사</button>'
    )
    tab_panels.append(f'<div id="tab-listed" class="tab-panel">{listed_panel()}\n    </div>')

    tab_buttons.append(
        '<button class="tab-btn" data-tab="overseas" onclick="showTab(\'overseas\')">해외 기업 관련 기사</button>'
    )
    tab_panels.append(f'<div id="tab-overseas" class="tab-panel">{overseas_panel()}\n    </div>')

    tab_buttons_html = "\n    ".join(tab_buttons)
    tab_panels_html = "\n  ".join(tab_panels)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>기관 및 업체별 AI Data Center 일일 동향</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
          max-width: 1080px; margin: 0 auto; padding: 32px 16px 60px;
          background: #f7f7f5; color: #222; }}
  header {{
    position: relative;
    background: #fff;
    border: 1px solid #e3e2dc;
    border-left: 6px solid #185fa5;
    border-radius: 10px;
    padding: 20px 140px 20px 30px;
    margin-bottom: 24px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.05);
  }}
  header .header-text {{ min-width: 0; }}
  header h1 {{
    font-size: 30pt;
    font-weight: 800;
    color: #185fa5;
    margin: 0 0 8px 0;
    letter-spacing: -0.5px;
  }}
  header h1 .ai-dc-hl {{
    text-decoration: underline;
    text-underline-offset: 3px;
  }}
  header p {{
    color: #666;
    font-size: 14px;
    margin: 0;
    font-weight: 500;
    line-height: 1.5;
  }}
  header .header-logo {{
    position: absolute;
    right: 24px;
    bottom: 16px;
  }}
  header .header-logo img {{
    height: 22px;
    width: auto;
    display: block;
  }}
  @media (max-width: 520px) {{
    header {{ padding-right: 30px; }}
    header .header-logo {{ position: static; margin-top: 12px; text-align: right; }}
  }}
  .layout {{ display: flex; align-items: flex-start; gap: 20px; margin-top: 20px; }}
  .sidebar {{ width: 260px; flex-shrink: 0; position: sticky; top: 20px; height: max-content; }}
  .main {{ flex: 1; min-width: 0; }}
  section.highlights {{
    background: #fff;
    border: 1px solid #e3e2dc;
    border-radius: 10px;
    padding: 4px 16px 16px;
    max-height: calc(100vh - 40px);
    overflow-y: auto;
  }}
  .highlight-card {{ border: 1px solid #e3e2dc; border-radius: 8px;
                      padding: 12px 14px; margin-bottom: 10px; }}
  .highlight-card:last-child {{ margin-bottom: 0; }}
  .highlight-company {{ font-size: 13px; font-weight: 700; color: #185fa5; margin-bottom: 6px; }}
  .highlight-title {{ display: block; color: #222; text-decoration: none; font-size: 13px;
                       line-height: 1.4; }}
  .highlight-title:hover {{ text-decoration: underline; color: #185fa5; }}
  .ai-summary {{ margin-top: 8px; }}
  .ai-summary summary {{ font-size: 12px; color: #185fa5; cursor: pointer; list-style: none; }}
  .ai-summary summary::-webkit-details-marker {{ display: none; }}
  .ai-summary summary::before {{ content: "▸ "; }}
  .ai-summary[open] summary::before {{ content: "▾ "; }}
  .ai-summary p {{ font-size: 13px; color: #333; line-height: 1.5; margin: 6px 0 0; }}
  @media (max-width: 760px) {{
    .layout {{ flex-direction: column; }}
    .sidebar {{ display: none; }}
  }}
  .cat-badge {{ font-size: 11px; font-weight: 700; color: #fff; padding: 2px 8px;
                border-radius: 10px; white-space: nowrap; }}
  .cat-badge.cat-gov {{ background: #185fa5; }}
  .cat-badge.cat-public {{ background: #1f8a4c; }}
  .cat-badge.cat-local {{ background: #c2703d; }}
  .cat-badge.cat-listed {{ background: #7b4fa6; }}
  .cat-badge.cat-overseas {{ background: #0f8a8a; }}
  .tab-banner {{ display: flex; gap: 8px; margin: 20px 0 24px; }}
  .tab-btn {{ flex: 1; padding: 12px 0; border: 1px solid #d8d6cf; border-radius: 10px;
              background: #fff; color: #444; font-size: 15px; font-weight: 600;
              cursor: pointer; }}
  .tab-btn.active {{ background: #185fa5; border-color: #185fa5; color: #fff; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  section.agency {{ background: #fff; border: 1px solid #e3e2dc; border-radius: 10px;
                     margin-bottom: 20px; padding: 4px 20px 16px; }}
  section.agency h2 {{ font-size: 17px; padding: 10px 0; }}
  section.agency h2 a {{ color: #185fa5; text-decoration: none; }}
  section.agency h2 a:hover {{ text-decoration: underline; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li {{ padding: 8px 0; border-top: 1px solid #eee; display: flex; justify-content: space-between;
        align-items: baseline; gap: 12px; }}
  li:first-child {{ border-top: none; }}
  li a {{ color: #222; text-decoration: none; font-size: 14px; line-height: 1.5; }}
  li a:hover {{ text-decoration: underline; color: #185fa5; }}
  li .date {{ font-size: 12px; color: #888; white-space: nowrap; }}
  .msg {{ font-size: 13px; padding: 12px 0; }}
  .msg.empty {{ color: #888; }}
  .msg.fail {{ color: #b3401f; }}
  details.more {{ margin-top: 8px; }}
  details.more summary {{ font-size: 13px; color: #185fa5; cursor: pointer; padding: 8px 0; }}
  details.more ul {{ padding-top: 4px; }}
  .section-desc {{ font-size: 12px; color: #888; margin: -6px 0 12px; }}
  ul.news-list {{ display: block; }}
  li.news-item {{ display: block; padding: 14px 0; }}
  li.news-item .news-title {{ display: block; font-size: 15px; font-weight: 600; color: #185fa5; }}
  li.news-item .news-title:hover {{ text-decoration: underline; }}
  li.news-item .news-summary {{ font-size: 13px; color: #555; margin: 6px 0; line-height: 1.5; }}
  li.news-item .news-meta {{ font-size: 12px; color: #888; margin: 0; }}
  li.news-item .region-tag {{ color: #185fa5; font-weight: 600; }}
  footer {{ color: #999; font-size: 12px; text-align: center; margin-top: 32px; }}
  /* --- AI 채팅창 --- */
  .ai-chat-btn {{ font-size: 11px; color: #fff; background: #185fa5; border: none;
                   border-radius: 10px; padding: 2px 10px; cursor: pointer; vertical-align: middle; }}
  .ai-chat-btn:hover {{ background: #124a80; }}
  #aiChatToggle {{ position: fixed; right: 20px; bottom: 20px; z-index: 1000;
                    background: #185fa5; color: #fff; border: none; border-radius: 50%;
                    width: 56px; height: 56px; font-size: 22px; cursor: pointer;
                    box-shadow: 0 4px 12px rgba(0,0,0,0.25); }}
  #aiChatPanel {{ position: fixed; right: 20px; bottom: 88px; z-index: 1000;
                   width: 360px; max-width: calc(100vw - 40px); height: 480px;
                   background: #fff; border: 1px solid #d8d6cf; border-radius: 14px;
                   box-shadow: 0 8px 30px rgba(0,0,0,0.2); display: none;
                   flex-direction: column; overflow: hidden; }}
  #aiChatPanel.open {{ display: flex; }}
  .ai-chat-header {{ background: #185fa5; color: #fff; padding: 12px 16px;
                      font-size: 14px; font-weight: 700; display: flex;
                      justify-content: space-between; align-items: center; }}
  .ai-chat-header button {{ background: none; border: none; color: #fff;
                             font-size: 16px; cursor: pointer; }}
  #aiChatMessages {{ flex: 1; overflow-y: auto; padding: 12px; font-size: 13px; }}
  .ai-msg {{ margin-bottom: 10px; line-height: 1.5; white-space: pre-wrap;
              word-break: break-all; }}
  .ai-msg.user {{ text-align: right; }}
  .ai-msg.user .bubble {{ display: inline-block; background: #185fa5; color: #fff;
                           border-radius: 12px 12px 2px 12px; padding: 8px 12px;
                           max-width: 85%; text-align: left; }}
  .ai-msg.bot .bubble {{ display: inline-block; background: #f0f0ee; color: #222;
                          border-radius: 12px 12px 12px 2px; padding: 8px 12px;
                          max-width: 85%; }}
  .ai-chat-input {{ border-top: 1px solid #eee; padding: 10px; display: flex;
                     gap: 6px; align-items: center; }}
  .ai-chat-input input[type="text"] {{ flex: 1; border: 1px solid #d8d6cf;
                                        border-radius: 8px; padding: 8px 10px;
                                        font-size: 13px; min-width: 0; }}
  .ai-chat-input button {{ background: #185fa5; color: #fff; border: none;
                            border-radius: 8px; padding: 8px 12px; font-size: 13px;
                            cursor: pointer; white-space: nowrap; }}
  .ai-chat-input label {{ background: #f0f0ee; color: #444; border-radius: 8px;
                           padding: 8px 10px; font-size: 13px; cursor: pointer;
                           white-space: nowrap; }}
  .ai-chat-input input[type="file"] {{ display: none; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #17181a; color: #e8e8e6; }}
    .tab-btn {{ background: #232527; border-color: #33353a; color: #ccc; }}
    section.agency {{ background: #232527; border-color: #33353a; }}
    li {{ border-top-color: #33353a; }}
    li a {{ color: #e8e8e6; }}
    li .date {{ color: #999; }}
    .msg.empty {{ color: #9a9a9a; }}
    li.news-item .news-summary {{ color: #aaa; }}
    li.news-item .news-meta {{ color: #999; }}
    section.highlights {{ background: #232527; border-color: #33353a; }}
    .highlight-card {{ border-color: #33353a; }}
    .highlight-title {{ color: #e8e8e6; }}
    .ai-summary p {{ color: #ccc; }}
    header {{ background: #232527; border-color: #33353a; border-left-color: #3a82ce; box-shadow: none; }}
    header h1 {{ color: #e8e8e6; }}
    header p {{ color: #9a9a9a; }}
    #aiChatPanel {{ background: #232527; border-color: #33353a; }}
    .ai-msg.bot .bubble {{ background: #2e3033; color: #e8e8e6; }}
    .ai-chat-input {{ border-top-color: #33353a; }}
    .ai-chat-input input[type="text"] {{ background: #17181a; border-color: #33353a; color: #e8e8e6; }}
    .ai-chat-input label {{ background: #2e3033; color: #ccc; }}
  }}
</style>
</head>
<body>
  <header>
    <div class="header-text">
      <h1>기관 및 업체별 <span class="ai-dc-hl">AI Data Center</span> 일일 동향</h1>
      <p>{TODAY_LABEL} 기준 <br>· 마지막 업데이트: {GENERATED_AT_LABEL} <br>· 매일 자동 업데이트(오후 3시~4시)</p>
    </div>
    <div class="header-logo">
      <img src="assets/sunjin_logo.png" alt="SUNJIN ENGINEERING & ARCHITECTURE">
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar">
      {highlights_html()}
    </aside>
    <div class="main">
      <div class="tab-banner">
        {tab_buttons_html}
      </div>
      {tab_panels_html}
    </div>
  </div>


  <script>
    function showTab(tab) {{
      // 1. 기존 탭/버튼 활성 상태 초기화
      document.querySelectorAll('.tab-panel').forEach(function (el) {{ el.classList.remove('active'); }});
      document.querySelectorAll('.tab-btn').forEach(function (el) {{ el.classList.remove('active'); }});

      // 2. 선택한 탭/버튼 활성화
      document.getElementById('tab-' + tab).classList.add('active');
      document.querySelector('.tab-btn[data-tab="' + tab + '"]').classList.add('active');
    }}
  </script>

  <!-- ===== AI 채팅창 ===== -->
  <button id="aiChatToggle" onclick="toggleAiChat()" title="AI 요약 채팅">🤖</button>
  <div id="aiChatPanel">
    <div class="ai-chat-header">
      <span>AI 요약 도우미</span>
      <button type="button" onclick="toggleAiChat()">✕</button>
    </div>
    <div id="aiChatMessages">
      <div class="ai-msg bot"><span class="bubble">안녕하세요! 기사 옆 "AI 요약하기" 버튼을 누르거나, 링크를 붙여넣거나, PDF 파일을 첨부하면 요약해드려요.</span></div>
    </div>
    <div class="ai-chat-input">
      <label for="aiPdfInput">📎 PDF</label>
      <input type="file" id="aiPdfInput" accept="application/pdf" onchange="aiHandlePdf(this)">
      <input type="text" id="aiChatText" placeholder="기사 링크 붙여넣기" onkeydown="if(event.key==='Enter')aiSendText()">
      <button type="button" onclick="aiSendText()">전송</button>
    </div>
  </div>

  <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
  <script>
    const AI_WORKER_URL = "{AI_CHAT_WORKER_URL}";
    if (window.pdfjsLib) {{
      pdfjsLib.GlobalWorkerOptions.workerSrc =
        "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
    }}

    function toggleAiChat() {{
      document.getElementById('aiChatPanel').classList.toggle('open');
    }}

    function aiAddMsg(role, text) {{
      const box = document.getElementById('aiChatMessages');
      const div = document.createElement('div');
      div.className = 'ai-msg ' + role;
      const bubble = document.createElement('span');
      bubble.className = 'bubble';
      bubble.textContent = text;
      div.appendChild(bubble);
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
      return bubble;
    }}

    async function aiCallWorker(payload, loadingBubble) {{
      try {{
        const resp = await fetch(AI_WORKER_URL, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const data = await resp.json();
        if (data.summary) {{
          loadingBubble.textContent = data.summary;
        }} else {{
          loadingBubble.textContent = '요약에 실패했어요. 잠시 후 다시 시도해주세요.';
          console.error('AI worker error:', data);
        }}
      }} catch (e) {{
        loadingBubble.textContent = '요약 서버에 연결하지 못했어요.';
        console.error(e);
      }}
    }}

    // 기사 옆 "AI 요약하기" 버튼 → 링크 자동 전송
    function aiSummarizeUrl(url) {{
      const panel = document.getElementById('aiChatPanel');
      if (!panel.classList.contains('open')) panel.classList.add('open');
      aiAddMsg('user', url);
      const loading = aiAddMsg('bot', '기사를 읽고 요약하는 중...');
      aiCallWorker({{ url: url }}, loading);
    }}

    // 입력창 전송 (링크 또는 일반 텍스트)
    function aiSendText() {{
      const input = document.getElementById('aiChatText');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      aiAddMsg('user', text);
      const loading = aiAddMsg('bot', '요약하는 중...');
      if (/^https?:\\/\\//i.test(text)) {{
        aiCallWorker({{ url: text }}, loading);
      }} else {{
        aiCallWorker({{ text: text }}, loading);
      }}
    }}

    // PDF 첨부 → 브라우저 안에서 텍스트 추출(pdf.js) → Worker로 전송
    async function aiHandlePdf(inputEl) {{
      const file = inputEl.files && inputEl.files[0];
      if (!file) return;
      inputEl.value = '';
      const panel = document.getElementById('aiChatPanel');
      if (!panel.classList.contains('open')) panel.classList.add('open');
      aiAddMsg('user', '📄 ' + file.name);
      const loading = aiAddMsg('bot', 'PDF에서 텍스트를 추출하는 중...');
      try {{
        const buf = await file.arrayBuffer();
        const pdf = await pdfjsLib.getDocument({{ data: buf }}).promise;
        let fullText = '';
        const maxPages = Math.min(pdf.numPages, 20);
        for (let i = 1; i <= maxPages; i++) {{
          const page = await pdf.getPage(i);
          const content = await page.getTextContent();
          fullText += content.items.map(it => it.str).join(' ') + '\\n';
          if (fullText.length > 12000) break;
        }}
        fullText = fullText.replace(/\\s+/g, ' ').trim();
        if (!fullText) {{
          loading.textContent = 'PDF에서 텍스트를 찾지 못했어요. 스캔 이미지 PDF일 수 있어요.';
          return;
        }}
        loading.textContent = '요약하는 중...';
        aiCallWorker({{ text: fullText.slice(0, 12000) }}, loading);
      }} catch (e) {{
        loading.textContent = 'PDF를 읽는 데 실패했어요.';
        console.error(e);
      }}
    }}
  </script>
</body>
</html>
"""


def main() -> None:
    cache = load_summary_cache()

    today_items = {}
    recent_items = {}
    fetch_failed = set()

    for org, fetcher in FETCHERS.items():
        items = fetcher()
        if items is None:
            fetch_failed.add(org)
            log(f"[SUMMARY] {org}: FETCH FAILED")
            continue
        matched = [it for it in items if it["date"] == TODAY]
        if ENABLE_AI_SUMMARIES:
            for it in matched:
                cached = cache["short"].get(it["link"])
                if cached:
                    it["summary"] = cached
                    continue
                body_text = fetch_detail_text(it["link"], org)
                it["summary"] = summarize_with_groq(it["title"], body_text)
                if it["summary"]:
                    cache["short"][it["link"]] = it["summary"]
        else:
            for it in matched:
                it["summary"] = ""
        today_items[org] = matched
        matched_links = {it["link"] for it in matched}
        remaining = [it for it in items if it["link"] not in matched_links]
        recent_items[org] = remaining[:RECENT_LIMIT]
        log(f"[SUMMARY] {org}: {len(matched)} item(s) today (of {len(items)} total parsed)")

    aidc_items, aidc_ok = fetch_aidc_news(cache)
    listed_items, listed_ok = fetch_listed_company_news(cache)
    overseas_items, overseas_ok = fetch_overseas_company_news(cache)

    save_summary_cache(cache)

    html = render_html(today_items, recent_items, fetch_failed,
                        aidc_items, aidc_ok, listed_items, listed_ok,
                        overseas_items, overseas_ok)

    os.makedirs("public", exist_ok=True)
    with open("public/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    log("[INFO] public/index.html written")


if __name__ == "__main__":
    main()

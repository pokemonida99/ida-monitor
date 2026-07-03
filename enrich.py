#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析新聞的原始媒體與記者。

Google News RSS 的「來源」常是轉載平台（Yahoo、LINE TODAY…），
本模組把 Google News 連結解碼成原始網址，再從文章頁面抽取：
  1. 原始媒體（報社/通訊社，例如中央社、工商時報）
  2. 記者姓名
"""
import json
import re
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# 台灣主要媒體網域對照
DOMAIN_MEDIA = {
    "cna.com.tw": "中央社",
    "ctee.com.tw": "工商時報",
    "chinatimes.com": "中時新聞網",
    "udn.com": "聯合報",
    "money.udn.com": "經濟日報",
    "ltn.com.tw": "自由時報",
    "appledaily.com": "蘋果日報",
    "setn.com": "三立新聞",
    "ettoday.net": "ETtoday",
    "tvbs.com.tw": "TVBS",
    "ftvnews.com.tw": "民視新聞",
    "pts.org.tw": "公視",
    "cts.com.tw": "華視",
    "ttv.com.tw": "台視",
    "storm.mg": "風傳媒",
    "nownews.com": "NOWnews",
    "newtalk.tw": "新頭殼",
    "upmedia.mg": "上報",
    "cmmedia.com.tw": "信傳媒",
    "wealth.com.tw": "財訊",
    "businesstoday.com.tw": "今周刊",
    "cw.com.tw": "天下雜誌",
    "bnext.com.tw": "數位時代",
    "technews.tw": "科技新報",
    "digitimes.com.tw": "DIGITIMES",
    "moneydj.com": "MoneyDJ",
    "cnyes.com": "鉅亨網",
    "wantrich.chinatimes.com": "旺得富",
    "taiwannews.com.tw": "Taiwan News",
    "rti.org.tw": "央廣",
    "taisounds.com": "太報",
    "tw.news.yahoo.com": None,   # 轉載平台：要從頁面找原始媒體
    "today.line.me": None,
    "news.pchome.com.tw": None,
    "match.net.tw": None,
    "yamnews.yam.com": None,
    "n.yam.com": None,
    "moea.gov.tw": "經濟部",
    "ida.gov.tw": "產業發展署",
}

# 平台頁面上「原始媒體」的標記
PROVIDER_PATTERNS = [
    r'"provider"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]{2,20})"',
    r'class="caas-attr-provider"[^>]*>([^<]{2,20})<',
    r'data-ylk="[^"]*sec:attribution[^"]*"[^>]*>([^<]{2,20})<',
    r'"publisher"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]{2,20})"',
    r'本文由《?([一-鿿A-Za-z]{2,12})》?(?:授權|提供)',
]

REPORTER_PATTERNS = [
    r"（中央社記者([一-鿿]{2,4})[^）]{0,25}電）",
    r"[（(【]?\s*記者\s*((?:[一-鿿]{2,4}\s*[、，,]?\s*)+)[／/╱｜|]\s*[^）)】\s]{0,14}報導",
    r"【記者([一-鿿]{2,4})[／/╱][^】]{0,14}】",
    r'"author"\s*:\s*\{[^}]*?"name"\s*:\s*"([一-鿿]{2,4})"',
    r'"author"\s*:\s*\[?\s*\{[^}]*?"name"\s*:\s*"([一-鿿]{2,4})"',
    r'<meta\s+name="author"\s+content="([一-鿿]{2,4})"',
]

# 抽到這些字樣代表不是人名
NOT_A_NAME = ("新聞", "報導", "編輯", "中心", "小組", "整理", "團隊", "媒體",
              "轉載", "資訊", "財經", "快訊", "綜合", "國際", "中央社")

# 同一媒體的不同寫法／子頻道，統一名稱以正確計算媒體家數
OUTLET_ALIASES = {
    "NOWNEWS今日新聞": "NOWnews",
    "NOWnews今日新聞": "NOWnews",
    "自由財經": "自由時報",
    "自由藝文網": "自由時報",
    "自由電子報": "自由時報",
    "中華新聞雲": "中華日報",
    "聯合新聞網": "聯合報",
    "UDN": "聯合報",
    "udn 產經": "聯合報",
    "Yahoo奇摩股市": "Yahoo新聞",
    "工商時報 CTEE": "工商時報",
}


def normalize_outlet(name):
    if not name:
        return name
    name = name.strip()
    return OUTLET_ALIASES.get(name, name)


def resolve_gnews_link(gnews_url: str, timeout: int = 20) -> str:
    """把 Google News RSS 連結解碼成原始文章網址；失敗時回傳原連結。"""
    m = re.search(r"articles/([^?/]+)", gnews_url)
    if not m:
        return gnews_url
    token = m.group(1)
    try:
        req = urllib.request.Request(gnews_url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", errors="ignore")
        sg = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sg and ts):
            return gnews_url
        payload = [
            "Fbv4je",
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
            'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            f'"{token}",{ts.group(1)},"{sg.group(1)}"]',
        ]
        body = "f.req=" + urllib.parse.quote(json.dumps([[payload]]))
        req = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data=body.encode(),
            headers={**UA, "Content-Type":
                     "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", errors="ignore")
        m2 = (re.search(r'\\"(https?://[^\\"]+)\\"', text)
              or re.search(r'"(https?://(?!news\.google)[^"]+)"', text))
        return m2.group(1) if m2 else gnews_url
    except Exception:
        return gnews_url


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def _clean_names(raw: str):
    names = []
    for name in re.split(r"[、，,\s]+", raw):
        name = name.strip()
        if (2 <= len(name) <= 4 and not any(w in name for w in NOT_A_NAME)
                and name not in names):
            names.append(name)
    return names


def enrich_article(gnews_url: str, rss_source: str):
    """回傳 (resolved_url, outlet, reporter)。任何一步失敗都回退到已知資訊。"""
    url = resolve_gnews_link(gnews_url)
    domain = _domain(url)

    outlet = None
    for d, name in DOMAIN_MEDIA.items():
        if domain == d or domain.endswith("." + d) or d.endswith(domain):
            outlet = name
            break

    html = ""
    if url != gnews_url:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", errors="ignore")
        except Exception:
            html = ""

    # 轉載平台（或未知網域）：從頁面找原始媒體
    if not outlet and html:
        for p in PROVIDER_PATTERNS:
            m = re.search(p, html)
            if m:
                outlet = m.group(1).strip()
                break
        if not outlet and re.search(r"（中央社記者", html):
            outlet = "中央社"

    reporters = []
    if html:
        for p in REPORTER_PATTERNS:
            for m in re.finditer(p, html[:60000]):
                for name in _clean_names(m.group(1)):
                    if name not in reporters:
                        reporters.append(name)
            if reporters:
                break

    return (url, normalize_outlet(outlet or rss_source or None),
            "、".join(reporters[:3]) or None)

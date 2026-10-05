import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "arena.json"
GOAL = 5.0
STOP = -3.0
LOOKBACK_TRADING_DAYS = 60
YAHOO_RANGE = "9mo"
REQUEST_TIMEOUT = 20

EXTRA_CLOSED = {"2026-10-05", "2026-12-31"}

UNIVERSE = [
    {"name": "삼성전자", "code": "005930", "symbol": "005930.KS"},
    {"name": "SK하이닉스", "code": "000660", "symbol": "000660.KS"},
    {"name": "현대차", "code": "005380", "symbol": "005380.KS"},
    {"name": "기아", "code": "000270", "symbol": "000270.KS"},
    {"name": "LG전자", "code": "066570", "symbol": "066570.KS"},
    {"name": "NAVER", "code": "035420", "symbol": "035420.KS"},
    {"name": "카카오", "code": "035720", "symbol": "035720.KS"},
    {"name": "삼성바이오로직스", "code": "207940", "symbol": "207940.KS"},
    {"name": "셀트리온", "code": "068270", "symbol": "068270.KS"},
    {"name": "POSCO홀딩스", "code": "005490", "symbol": "005490.KS"},
    {"name": "KB금융", "code": "105560", "symbol": "105560.KS"},
    {"name": "신한지주", "code": "055550", "symbol": "055550.KS"},
    {"name": "하나금융지주", "code": "086790", "symbol": "086790.KS"},
    {"name": "현대모비스", "code": "012330", "symbol": "012330.KS"},
    {"name": "HD현대중공업", "code": "329180", "symbol": "329180.KS"},
    {"name": "한화오션", "code": "042660", "symbol": "042660.KS"},
    {"name": "HD현대일렉트릭", "code": "267260", "symbol": "267260.KS"},
    {"name": "두산에너빌리티", "code": "034020", "symbol": "034020.KS"},
    {"name": "한미반도체", "code": "042700", "symbol": "042700.KS"},
    {"name": "LG에너지솔루션", "code": "373220", "symbol": "373220.KS"},
    {"name": "삼성SDI", "code": "006400", "symbol": "006400.KS"},
    {"name": "LG화학", "code": "051910", "symbol": "051910.KS"},
    {"name": "에코프로비엠", "code": "247540", "symbol": "247540.KQ"},
    {"name": "HMM", "code": "011200", "symbol": "011200.KS"},
    {"name": "크래프톤", "code": "259960", "symbol": "259960.KS"},
]

NPS_WATCHLIST = {
    "005930": 1.00, "000660": 1.00, "035420": 0.95, "105560": 0.95,
    "055550": 0.90, "086790": 0.90, "012330": 0.90, "005490": 0.90,
    "207940": 0.85, "068270": 0.85, "005380": 0.85, "000270": 0.80,
    "066570": 0.80, "051910": 0.75, "006400": 0.75,
}

NEWS_KEYWORDS = [
    "policy", "government", "regulation", "tariff", "defense", "shipbuilding", "nuclear",
    "semiconductor", "ai", "battery", "order", "contract", "export", "subsidy", "investment",
    "정책", "정부", "규제", "관세", "방산", "조선", "원전", "반도체", "인공지능", "배터리",
    "수주", "계약", "수출", "지원", "투자", "증설", "실적", "가이던스",
]

TRADERS = {
    "chart": {"name": "윤서진", "role": "차트 & 수급"},
    "policy": {"name": "박도현", "role": "뉴스 & 정책"},
    "nps": {"name": "김하린", "role": "NPS FLOW"},
}


def kst_now():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))


def http_json(url, timeout=REQUEST_TIMEOUT):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 TRADER-ARENA"})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


def yahoo_history(symbol):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(symbol)}"
        f"?interval=1d&range={YAHOO_RANGE}&events=history&includeAdjustedClose=true"
    )
    obj = http_json(url)
    result = (obj.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError("Yahoo history unavailable")
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators", {})
    quote = (indicators.get("quote") or [{}])[0]
    adj = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []
    closes = adj if adj else (quote.get("close") or [])
    volumes = quote.get("volume") or []
    rows = []
    for i, ts in enumerate(timestamps):
        if i >= len(closes):
            break
        close = closes[i]
        if close is None:
            continue
        vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        rows.append({"date": dt, "close": float(close), "volume": float(vol or 0)})
    if len(rows) < 45:
        raise RuntimeError(f"only {len(rows)} daily bars")
    return rows


def yahoo_quote(symbol):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(symbol)}"
        f"?interval=1m&range=1d&events=history"
    )
    obj = http_json(url)
    result = (obj.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError("Yahoo quote unavailable")
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if price is None:
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        price = next((float(v) for v in reversed(closes) if v is not None), None)
    if price is None:
        raise RuntimeError("price unavailable")
    return float(price), ts


def yahoo_news(query):
    url = (
        "https://query2.finance.yahoo.com/v1/finance/search?"
        f"q={quote_plus(query)}&quotesCount=0&newsCount=8&enableFuzzyQuery=false"
    )
    try:
        obj = http_json(url, timeout=12)
        return obj.get("news") or []
    except Exception:
        return []


def ret(closes, n):
    if len(closes) <= n:
        return 0.0
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


def sma(values, n):
    if len(values) < n:
        return values[-1]
    return sum(values[-n:]) / n


def stdev_pct(values):
    if len(values) < 3:
        return 0.0
    rs = [
        (values[i] / values[i - 1] - 1.0) * 100.0
        for i in range(1, len(values)) if values[i - 1] != 0
    ]
    if len(rs) < 2:
        return 0.0
    mean = sum(rs) / len(rs)
    return math.sqrt(sum((x - mean) ** 2 for x in rs) / (len(rs) - 1))


def position_in_range(closes, n=60):
    window = closes[-n:]
    lo, hi = min(window), max(window)
    return 50.0 if hi == lo else (window[-1] - lo) / (hi - lo) * 100.0


def volume_ratio(volumes, short=5, long=20):
    if len(volumes) < long + short:
        return 1.0
    s = sum(volumes[-short:]) / short
    l = sum(volumes[-long:-short]) / max(1, long - short)
    return s / l if l else 1.0


def build_metrics(rows):
    closes = [r["close"] for r in rows]
    volumes = [r["volume"] for r in rows]
    return {
        "bars": len(rows),
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "ret_5d": ret(closes, 5),
        "ret_20d": ret(closes, 20),
        "ret_60d": ret(closes, 60),
        "sma20": sma(closes, 20),
        "sma60": sma(closes, 60),
        "price": closes[-1],
        "position60": position_in_range(closes, 60),
        "vol_ratio": volume_ratio(volumes),
        "volatility20": stdev_pct(closes[-21:]),
        "recent_high20": max(closes[-20:]),
        "recent_low20": min(closes[-20:]),
    }


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def chart_score(m):
    score = 50.0
    score += max(-20, min(20, m["ret_20d"] * 1.8))
    score += max(-15, min(15, m["ret_60d"] * 0.9))
    score += 14 if m["price"] > m["sma20"] > m["sma60"] else 0
    score += 7 if m["ret_5d"] > 0 else max(-7, m["ret_5d"])
    score += max(-8, min(12, (m["vol_ratio"] - 1) * 20))
    score += 8 if m["position60"] >= 70 else 0
    score -= max(0, m["volatility20"] - 4) * 1.5
    return round(clamp(score), 1)


def policy_score(m, news_score):
    score = 45.0
    score += max(-15, min(18, m["ret_20d"] * 1.5))
    score += max(-10, min(14, (m["ret_20d"] - m["ret_60d"] / 3) * 2.0))
    score += max(-5, min(16, (m["vol_ratio"] - 1) * 28))
    score += 10 if m["price"] >= m["recent_high20"] * 0.98 else 0
    score += news_score
    score -= max(0, m["volatility20"] - 5) * 1.2
    return round(clamp(score), 1)


def nps_score(m, nps_weight):
    score = 40.0
    score += nps_weight * 28
    score += max(-12, min(15, m["ret_60d"] * 0.75))
    score += max(-10, min(15, m["ret_20d"] * 1.1))
    score += 8 if m["price"] > m["sma60"] else -6
    score += max(-6, min(8, (m["vol_ratio"] - 1) * 14))
    score -= max(0, m["volatility20"] - 4) * 1.0
    return round(clamp(score), 1)


def news_score_for(name, news):
    if not news:
        return 0.0, 0
    hits = 0
    for item in news:
        title = str(item.get("title") or "").lower()
        hits += sum(1 for k in NEWS_KEYWORDS if k.lower() in title)
    return round(min(16.0, hits * 2.0), 1), len(news)


def choose_unique(score_lists):
    selected = {}
    used = set()
    for trader_id in ("chart", "policy", "nps"):
        for item in score_lists[trader_id]:
            if item["code"] not in used:
                selected[trader_id] = item
                used.add(item["code"])
                break
    return selected


def select_daily_picks():
    metrics_by_code = {}
    for item in UNIVERSE:
        try:
            rows = yahoo_history(item["symbol"])
            metrics_by_code[item["code"]] = {**item, "metrics": build_metrics(rows)}
        except Exception as e:
            print(f"SKIP {item['name']} {item['code']}: {e}")
        time.sleep(0.10)

    if len(metrics_by_code) < 8:
        raise RuntimeError(f"Too few candidates with 60-day data: {len(metrics_by_code)}")

    news_candidates = sorted(
        metrics_by_code.values(),
        key=lambda x: x["metrics"]["ret_20d"] + (x["metrics"]["vol_ratio"] - 1) * 8,
        reverse=True,
    )[:10]
    news_bonus = {}
    for item in news_candidates:
        news_bonus[item["code"]] = news_score_for(item["name"], yahoo_news(item["name"]))
        time.sleep(0.10)

    score_lists = {"chart": [], "policy": [], "nps": []}
    for item in metrics_by_code.values():
        m = item["metrics"]
        p_news, news_count = news_bonus.get(item["code"], (0.0, 0))
        base = {
            "name": item["name"], "code": item["code"], "symbol": item["symbol"],
            "metrics": m, "news_bonus": p_news, "news_count": news_count,
            "nps_watch_weight": NPS_WATCHLIST.get(item["code"], 0.0),
        }
        score_lists["chart"].append({
            **base,
            "score": chart_score(m),
            "strategy_reason": (
                f"{LOOKBACK_TRADING_DAYS}거래일 관찰 · 20일 {m['ret_20d']:+.1f}% · 60일 {m['ret_60d']:+.1f}% · "
                f"5일 {m['ret_5d']:+.1f}% · 거래량 {m['vol_ratio']:.1f}배 · "
                f"20/60일선 {'정배열' if m['price'] > m['sma20'] > m['sma60'] else '혼조'}"
            ),
        })
        score_lists["policy"].append({
            **base,
            "score": policy_score(m, p_news),
            "strategy_reason": (
                f"{LOOKBACK_TRADING_DAYS}거래일 관찰 · 20일 {m['ret_20d']:+.1f}% · 5일 {m['ret_5d']:+.1f}% · "
                f"거래량 {m['vol_ratio']:.1f}배 · 촉매 보너스 {p_news:.1f}점 · 최근 뉴스 {news_count}건"
            ),
        })
        score_lists["nps"].append({
            **base,
            "score": nps_score(m, NPS_WATCHLIST.get(item["code"], 0.0)),
            "strategy_reason": (
                f"{LOOKBACK_TRADING_DAYS}거래일 관찰 · NPS 공개정보 추적가중치 {NPS_WATCHLIST.get(item['code'], 0.0):.2f} · "
                f"20일 {m['ret_20d']:+.1f}% · 60일 {m['ret_60d']:+.1f}% · 거래량 {m['vol_ratio']:.1f}배"
            ),
        })

    for scores in score_lists.values():
        scores.sort(key=lambda x: (x["score"], x["metrics"]["ret_20d"]), reverse=True)

    selected = choose_unique(score_lists)
    output = []
    for trader_id in ("chart", "policy", "nps"):
        item = selected[trader_id]
        output.append({
            "trader_id": trader_id,
            "trader_name": TRADERS[trader_id]["name"],
            "role": TRADERS[trader_id]["role"],
            "name": item["name"], "code": item["code"],
            "score": item["score"], "reason": item["strategy_reason"],
            "entry": None, "price": None, "return": None,
            "status": "WAITING_ENTRY", "target_pct": GOAL, "stop_pct": STOP,
            "quote_source": "Yahoo Finance KSE delayed", "quote_timestamp": None,
            "history_window": f"최근 {LOOKBACK_TRADING_DAYS}거래일",
            "training_metrics": item["metrics"],
            "news_count": item["news_count"], "nps_watch_weight": item["nps_watch_weight"],
        })
    return output, {
        "lookback_trading_days": LOOKBACK_TRADING_DAYS,
        "data_range": YAHOO_RANGE,
        "selection_method": "rule-based historical scoring",
        "selection_generated_at": kst_now().isoformat(),
        "selection_universe_size": len(metrics_by_code),
    }


def load_state():
    if DATA.exists():
        try:
            return json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "session_date": None, "status": "WAITING_SESSION", "goal_pct": GOAL, "stop_pct": STOP,
        "updated_at": None, "picks": [], "history": [], "selection_meta": {},
        "last_error": None, "server": "GitHub Actions · Yahoo Finance KSE delayed data",
    }


def save_state(obj):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def is_closed_day(dt):
    date_str = dt.strftime("%Y-%m-%d")
    if dt.weekday() >= 5 or date_str in EXTRA_CLOSED:
        return True
    try:
        import holidays
        kr = holidays.KR(years=[dt.year])
        if dt.date() in kr:
            return True
    except Exception:
        pass
    return False


def maybe_open_new_session(obj, kst):
    date_str = kst.strftime("%Y-%m-%d")
    hm = int(kst.strftime("%H%M"))
    if hm < 845 or is_closed_day(kst) or obj.get("session_date") == date_str:
        return obj
    picks, meta = select_daily_picks()
    # Keep a generous history window in a single JSON file for the static site.
    obj = {
        "session_date": date_str, "status": "WAITING_ENTRY", "goal_pct": GOAL, "stop_pct": STOP,
        "updated_at": None, "picks": picks, "history": obj.get("history", [])[-120:],
        "selection_meta": meta, "last_error": None,
        "server": "GitHub Actions · Yahoo Finance KSE delayed data",
    }
    return obj


def history_record_for(obj):
    return {
        "session_date": obj.get("session_date"),
        "result_pct": round(sum(float(p.get("return") or 0.0) for p in obj.get("picks", [])) / max(1, len(obj.get("picks", []))), 4),
        "selection_generated_at": obj.get("selection_meta", {}).get("selection_generated_at"),
        "picks": [
            {
                "trader_id": p.get("trader_id"),
                "trader_name": p.get("trader_name"),
                "role": p.get("role"),
                "name": p.get("name"),
                "code": p.get("code"),
                "entry": p.get("entry"),
                "exit_price": p.get("price"),
                "return": p.get("return"),
                "status": p.get("status"),
                "score": p.get("score"),
                "reason": p.get("reason"),
                "training_metrics": p.get("training_metrics", {}),
                "news_count": p.get("news_count", 0),
                "nps_watch_weight": p.get("nps_watch_weight", 0.0),
            }
            for p in obj.get("picks", [])
        ],
    }


def update_session(obj, kst):
    hm = int(kst.strftime("%H%M"))
    if not obj.get("picks"):
        return obj

    if 900 <= hm < 1530:
        running_any = False
        for p in obj["picks"]:
            try:
                symbol = next(x["symbol"] for x in UNIVERSE if x["code"] == p["code"])
                price, ts = yahoo_quote(symbol)
                p["price"] = price
                p["quote_timestamp"] = ts
                if p["entry"] is None:
                    p["entry"] = price
                    p["return"] = 0.0
                    p["status"] = "RUNNING"
                elif p["status"] == "RUNNING":
                    r = (price / p["entry"] - 1.0) * 100.0
                    if r >= GOAL:
                        p["return"] = GOAL; p["status"] = "TARGET_HIT"
                    elif r <= STOP:
                        p["return"] = STOP; p["status"] = "STOPPED"
                    else:
                        p["return"] = r
                if p["status"] == "RUNNING":
                    running_any = True
            except Exception as e:
                p["error"] = str(e)
        obj["status"] = "RUNNING" if running_any else "DONE"

    elif hm >= 1530:
        for p in obj["picks"]:
            try:
                symbol = next(x["symbol"] for x in UNIVERSE if x["code"] == p["code"])
                price, ts = yahoo_quote(symbol)
                p["price"] = price
                p["quote_timestamp"] = ts
                if p["entry"] is not None and p["status"] == "RUNNING":
                    p["return"] = (price / p["entry"] - 1.0) * 100.0
                    p["status"] = "DONE"
            except Exception as e:
                p["error"] = str(e)
        obj["status"] = "DONE"
        history = obj.setdefault("history", [])
        if not any(h.get("session_date") == obj.get("session_date") for h in history):
            history.append(history_record_for(obj))
            obj["history"] = history[-120:]
    return obj


def main():
    obj = load_state()
    kst = kst_now()
    if is_closed_day(kst):
        obj["last_error"] = None
        obj["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(obj)
        print(f"Closed day: {kst.date()} — no new session")
        return
    try:
        obj = maybe_open_new_session(obj, kst)
        obj = update_session(obj, kst)
        obj["updated_at"] = datetime.now(timezone.utc).isoformat()
        obj["last_error"] = None
        save_state(obj)
        print(f"Updated TRADER ARENA: {obj.get('session_date')} {obj.get('status')}")
    except Exception as e:
        obj["updated_at"] = datetime.now(timezone.utc).isoformat()
        obj["last_error"] = str(e)
        save_state(obj)
        print(f"ERROR: {e}")
        raise


if __name__ == "__main__":
    main()

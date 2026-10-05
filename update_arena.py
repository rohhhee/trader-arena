import json, os, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'arena.json'
GOAL = 5.0
STOP = -3.0
HOLIDAYS = {"2026-10-05", "2026-10-09"}


def kst_now():
    # KST = UTC+9 without external dependencies
    return datetime.now(timezone.utc).astimezone(timezone.utc).replace(hour=0) if False else datetime.now(timezone.utc)


def yahoo(code):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{code}.KS?interval=1m&range=1d&events=history'
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(req, timeout=15) as r:
        obj = json.load(r)
    result = obj['chart']['result'][0]
    meta = result.get('meta', {})
    price = meta.get('regularMarketPrice')
    ts = meta.get('regularMarketTime')
    if price is None:
        closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', [])
        price = next((float(x) for x in reversed(closes) if x is not None), None)
    if price is None:
        raise RuntimeError(f'{code}: price unavailable')
    return float(price), ts


def save(obj):
    DATA.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    obj = json.loads(DATA.read_text(encoding='utf-8'))
    now = datetime.now(timezone.utc)
    kst = now.timestamp() + 9*3600
    k = datetime.fromtimestamp(kst, tz=timezone.utc)
    date_str = k.strftime('%Y-%m-%d')
    hm = int(k.strftime('%H%M'))

    # Weekend/holiday: nothing to do.
    if k.weekday() >= 5 or date_str in HOLIDAYS:
        return

    # Use the configured session. Before 08:50 KST, keep it untouched.
    if hm < 850:
        return

    # Create a new day automatically after the previous day is finished.
    if obj.get('session_date') != date_str and hm >= 850:
        # Simple daily strategy selection based on prior configured candidates.
        # This is intentionally deterministic for a prototype.
        picks = [
            {"trader_id":"chart","trader_name":"윤서진","role":"차트 & 수급","name":"SK하이닉스","code":"000660","score":72,"reason":"5일·20일 모멘텀 + 거래량 단기 추세 전략"},
            {"trader_id":"policy","trader_name":"박도현","role":"뉴스 & 정책","name":"한화오션","code":"042660","score":70,"reason":"정책·산업 촉매 + 단기 모멘텀 전략"},
            {"trader_id":"nps","trader_name":"김하린","role":"NPS FLOW","name":"삼성전자","code":"005930","score":68,"reason":"국민연금 공개 보유정보 + 단기 모멘텀 전략","nps_hold_pct":7.9},
        ]
        obj = {"session_date":date_str,"status":"WAITING_ENTRY","goal_pct":GOAL,"stop_pct":STOP,"updated_at":None,"picks":[],"history":obj.get('history',[]) }
        for p in picks:
            p.update({"entry":None,"price":None,"return":None,"status":"WAITING_ENTRY","quote_source":"Yahoo Finance KSE delayed"})
        obj['picks'] = picks

    # Before market open, don't lock entry. During the day, use latest available quote.
    if 900 <= hm < 1530:
        any_running = False
        for p in obj['picks']:
            price, ts = yahoo(p['code'])
            p['price'] = price
            p['quote_timestamp'] = ts
            if p['entry'] is None:
                p['entry'] = price
                p['return'] = 0.0
                p['status'] = 'RUNNING'
            elif p['status'] in ('RUNNING', 'WAITING_ENTRY'):
                p['return'] = (price / p['entry'] - 1) * 100
                if p['return'] >= GOAL:
                    p['return'] = GOAL
                    p['status'] = 'TARGET_HIT'
                elif p['return'] <= STOP:
                    p['return'] = STOP
                    p['status'] = 'STOPPED'
                else:
                    p['status'] = 'RUNNING'
            if p['status'] == 'RUNNING': any_running = True
        obj['status'] = 'RUNNING' if any_running else 'DONE'

    elif hm >= 1530:
        # Finalize at last available delayed quote.
        results=[]
        for p in obj['picks']:
            try:
                price, ts = yahoo(p['code'])
                p['price']=price; p['quote_timestamp']=ts
                if p['entry'] is not None and p['status']=='RUNNING':
                    p['return']=(price/p['entry']-1)*100
                    p['status']='DONE'
            except Exception:
                pass
            results.append(p['return'] if p['return'] is not None else 0.0)
        obj['status']='DONE'
        avg = sum(results)/len(results) if results else 0.0
        hist = obj.setdefault('history', [])
        if not any(h.get('session_date') == date_str for h in hist):
            hist.append({"session_date":date_str,"result_pct":avg,"picks":[{"name":p['name'],"return":p['return']} for p in obj['picks']]})
            obj['history']=hist[-30:]

    obj['updated_at'] = datetime.now(timezone.utc).isoformat()
    save(obj)

if __name__ == '__main__':
    main()

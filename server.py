import json
import math
import threading
from datetime import datetime, date, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
STATE_FILE = DATA_DIR / 'daily_state.json'
HISTORY_FILE = DATA_DIR / 'daily_history.json'
PORT = 8001
POLL_SECONDS = 15
KST = ZoneInfo('Asia/Seoul')
YAHOO_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36'
GOAL_PCT = 5.0
STOP_PCT = -3.0
ENTRY_START = dtime(9, 0)
EXIT_TIME = dtime(15, 20)  # paper-trading exit target; uses latest delayed quote

TRADERS = {
    'chart': {
        'id': 'chart', 'name': '윤서진', 'title': '차트 & 수급',
        'philosophy': '추세가 살아있는 종목만 짧게 친다.',
        'desc': '5일·20일 모멘텀, 거래량, 최근 고점 대비 위치를 이용한 단기 돌파/눌림 전략.'
    },
    'policy': {
        'id': 'policy', 'name': '박도현', 'title': '뉴스 & 정책',
        'philosophy': '기대의 변화가 가장 빠른 곳을 찾는다.',
        'desc': '정책·산업 이벤트와 단기 수급을 결합해 촉매가 있는 종목을 선택하는 전략.'
    },
    'nps': {
        'id': 'nps', 'name': '김하린', 'title': 'NPS FLOW',
        'philosophy': '국민연금 공개 보유정보에서 단기 후보를 찾는다.',
        'desc': '국민연금의 최신 공개 지분정보를 유니버스로 삼고 모멘텀·거래량을 보조 신호로 쓰는 전략.'
    }
}

# Latest public NPS large-holding information used by this prototype.
# These are NOT real-time NPS orders.
NPS_UNIVERSE = [
    {'code': '005930', 'name': '삼성전자', 'nps_hold_pct': 7.9},
    {'code': '000660', 'name': 'SK하이닉스', 'nps_hold_pct': 8.1},
    {'code': '035420', 'name': 'NAVER', 'nps_hold_pct': 7.0},
    {'code': '035720', 'name': '카카오', 'nps_hold_pct': 6.0},
]

UNIVERSE = [
    # Chart / momentum candidates
    ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('042700', '한미반도체'),
    ('012450', '한화에어로스페이스'), ('034730', 'SK'), ('009540', 'HD한국조선해양'),
    # Policy / catalyst candidates
    ('042660', '한화오션'), ('012450', '한화에어로스페이스'), ('047810', '한국항공우주'),
    ('010620', '현대미포조선'), ('329180', 'HD현대중공업'), ('272210', '한화시스템'),
    # NPS candidates
    *[(x['code'], x['name']) for x in NPS_UNIVERSE],
]

# Current session catalyst weights. Kept separate so they can be updated daily without touching code.
POLICY_CATALYSTS = {
    '042660': 9.0,  # shipbuilding/defense rotation candidate
    '012450': 8.5,
    '047810': 7.5,
    '272210': 7.5,
    '329180': 7.0,
    '009540': 6.5,
    '005930': 6.0,  # 3Q guidance/earnings week catalyst
    '000660': 5.5,
}

HOLIDAYS_2026 = {
    date(2026, 10, 5),  # Korean substitute holiday
    date(2026, 10, 9),  # Hangul Day
}

lock = threading.Lock()
quote_cache = {}
history_cache = {}
last_error = None


def now_kst():
    return datetime.now(KST)


def next_trading_day(d: date) -> date:
    x = d
    while x.weekday() >= 5 or x in HOLIDAYS_2026:
        x += timedelta(days=1)
    return x


def previous_trading_day(d: date) -> date:
    x = d
    while x.weekday() >= 5 or x in HOLIDAYS_2026:
        x -= timedelta(days=1)
    return x


def current_session_date() -> date:
    """Return the trade date for the active daily contest.
    Before the next trading day opens, use that next date. During/after a trading day, use today.
    """
    n = now_kst()
    if n.date().weekday() >= 5 or n.date() in HOLIDAYS_2026:
        return next_trading_day(n.date())
    if n.time() < ENTRY_START:
        return n.date()
    return n.date()


def market_is_open_for_session(session: date) -> bool:
    n = now_kst()
    return n.date() == session and ENTRY_START <= n.time() < dtime(15, 30)


def is_after_exit(session: date) -> bool:
    n = now_kst()
    return n.date() == session and n.time() >= dtime(15, 30)


def norm(code):
    s = ''.join(c for c in str(code) if c.isdigit())
    return s[-6:].zfill(6) if s else ''


def yahoo_raw(code, range_='3mo', interval='1d'):
    ticker = f'{norm(code)}.KS'
    params = {'interval': interval, 'range': range_, 'events': 'history'}
    r = requests.get(YAHOO_URL.format(ticker=ticker), params=params, headers={'User-Agent': UA}, timeout=10)
    r.raise_for_status()
    body = r.json()
    result = body['chart']['result'][0]
    return result


def yahoo_quote(code):
    result = yahoo_raw(code, '5d', '1d')
    meta = result.get('meta', {})
    price = meta.get('regularMarketPrice')
    previous = meta.get('previousClose') or meta.get('chartPreviousClose')
    ts = meta.get('regularMarketTime')
    if price is None:
        q = (result.get('indicators', {}).get('quote') or [{}])[0]
        closes = q.get('close') or []
        price = next((float(v) for v in reversed(closes) if v is not None), None)
    if price is None:
        raise RuntimeError('Yahoo에서 현재 가격을 받지 못했습니다.')
    price = float(price)
    previous = float(previous) if previous is not None else 0.0
    return {
        'code': norm(code), 'price': price, 'prev_close': previous,
        'change_pct': ((price / previous) - 1) * 100 if previous else 0.0,
        'timestamp': datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat(),
        'source': 'Yahoo Finance · KSE 지연 시세'
    }


def history_stats(code):
    code = norm(code)
    if code in history_cache:
        return history_cache[code]
    result = yahoo_raw(code, '3mo', '1d')
    q = (result.get('indicators', {}).get('quote') or [{}])[0]
    closes = [float(x) for x in (q.get('close') or []) if x is not None]
    volumes = [float(x) for x in (q.get('volume') or []) if x is not None]
    if len(closes) < 21:
        raise RuntimeError(f'{code}: 과거 시세 데이터 부족')
    last = closes[-1]
    c5 = closes[-6]
    c20 = closes[-21]
    ret5 = (last / c5 - 1) * 100
    ret20 = (last / c20 - 1) * 100
    vol20 = sum(volumes[-20:]) / max(1, len(volumes[-20:]))
    vol_ratio = (volumes[-1] / vol20) if vol20 else 1.0
    high20 = max(closes[-20:])
    low20 = min(closes[-20:])
    position = ((last - low20) / (high20 - low20)) * 100 if high20 > low20 else 50
    daily_returns = []
    for i in range(max(1, len(closes)-20), len(closes)):
        if closes[i-1]:
            daily_returns.append((closes[i] / closes[i-1] - 1) * 100)
    vol = math.sqrt(sum(x*x for x in daily_returns) / max(1, len(daily_returns)))
    out = {'ret5': ret5, 'ret20': ret20, 'vol_ratio': vol_ratio, 'position20': position, 'risk': vol}
    history_cache[code] = out
    return out


def clamp(x, a=0, b=100):
    return max(a, min(b, x))


def pick_chart(used):
    cands = []
    for code, name in UNIVERSE[:6]:
        if code in used:
            continue
        try:
            s = history_stats(code)
            score = clamp(50 + s['ret5']*3 + s['ret20']*1.2 + (s['vol_ratio']-1)*12 - max(0, s['position20']-92)*2)
            cands.append((score, code, name, s, '5일/20일 모멘텀과 거래량을 우선해 단기 추세가 가장 강한 후보를 선택'))
        except Exception:
            continue
    if not cands:
        return {'code': '000660', 'name': 'SK하이닉스', 'score': 72, 'reason': '반도체 모멘텀 후보'}
    score, code, name, s, reason = max(cands, key=lambda x: x[0])
    return {'code': code, 'name': name, 'score': round(score, 1), 'reason': reason, 'stats': s}


def pick_policy(used):
    cands = []
    for code, name in UNIVERSE[6:12]:
        if code in used:
            continue
        try:
            s = history_stats(code)
            catalyst = POLICY_CATALYSTS.get(code, 4.0)
            score = clamp(45 + s['ret5']*2.2 + s['ret20']*0.7 + catalyst*4 + (s['vol_ratio']-1)*8)
            cands.append((score, code, name, s, catalyst))
        except Exception:
            continue
    if not cands:
        return {'code': '042660', 'name': '한화오션', 'score': 70, 'reason': '조선·방산 정책/산업 촉매 후보'}
    score, code, name, s, catalyst = max(cands, key=lambda x: x[0])
    return {'code': code, 'name': name, 'score': round(score, 1), 'reason': f'정책·산업 촉매 {catalyst:.1f}/10 + 단기 수급/모멘텀', 'stats': s}


def pick_nps(used):
    cands = []
    for item in NPS_UNIVERSE:
        if item['code'] in used:
            continue
        try:
            s = history_stats(item['code'])
            score = clamp(40 + item['nps_hold_pct']*3 + s['ret5']*1.5 + s['ret20']*0.5 + (s['vol_ratio']-1)*5)
            cands.append((score, item, s))
        except Exception:
            continue
    if not cands:
        item = NPS_UNIVERSE[0]
        return {'code': item['code'], 'name': item['name'], 'nps_hold_pct': item['nps_hold_pct'], 'score': 72, 'reason': '국민연금 공개 지분 우선 후보'}
    score, item, s = max(cands, key=lambda x: x[0])
    return {'code': item['code'], 'name': item['name'], 'nps_hold_pct': item['nps_hold_pct'], 'score': round(score, 1), 'reason': f'국민연금 공개 지분 {item["nps_hold_pct"]:.1f}% + 단기 모멘텀', 'stats': s}


def load_json(path, default):
    DATA_DIR.mkdir(exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return json.loads(json.dumps(default))


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def new_state(session_date: date):
    # Today's selected candidates are designed for the next market open when the market is closed.
    # Seed today's picks from current market context; on normal days, scoring can replace them.
    if session_date == date(2026, 10, 6):
        selections = [
            {'trader_id': 'chart', 'code': '000660', 'name': 'SK하이닉스', 'score': 77, 'reason': 'AI·반도체 모멘텀을 차트/거래량으로 추적. 20거래일 강세 뒤 5거래일 숨고르기라 시초 수급 확인형 진입.', 'nps_hold_pct': 8.1},
            {'trader_id': 'policy', 'code': '042660', 'name': '한화오션', 'score': 71, 'reason': '조선·방산 순환매를 정책/산업 촉매 관점에서 추적. 최근 단기 하락 후 재료 재점화 여부를 확인.', 'nps_hold_pct': 0},
            {'trader_id': 'nps', 'code': '005930', 'name': '삼성전자', 'score': 82, 'reason': '국민연금 최신 공개 대량보유 정보에서 지분율 7.9%로 확인된 대형주. 실적 이벤트 주간의 단기 수급을 추적.', 'nps_hold_pct': 7.9},
        ]
    else:
        used = set()
        a = pick_chart(used); used.add(a['code'])
        b = pick_policy(used); used.add(b['code'])
        c = pick_nps(used); used.add(c['code'])
        selections = [
            {'trader_id': 'chart', **a},
            {'trader_id': 'policy', **b},
            {'trader_id': 'nps', **c},
        ]
    return {
        'session_date': session_date.isoformat(),
        'selected_at': now_kst().isoformat(),
        'goal_pct': GOAL_PCT,
        'stop_pct': STOP_PCT,
        'status': 'WAITING_ENTRY',
        'picks': [
            {**x, 'entry': None, 'exit': None, 'exit_reason': None, 'entry_time': None, 'exit_time': None, 'return': None, 'status': 'WAITING_ENTRY'}
            for x in selections
        ]
    }


def load_state():
    session = current_session_date()
    state = load_json(STATE_FILE, {})
    if state.get('session_date') != session.isoformat():
        state = new_state(session)
        save_json(STATE_FILE, state)
        history_cache.clear()
    return state


def save_state(state):
    save_json(STATE_FILE, state)


def load_history():
    return load_json(HISTORY_FILE, [])


def push_history_if_needed(state):
    if state.get('status') not in ('DONE', 'STOPPED'):
        return
    hist = load_history()
    sid = state['session_date']
    if any(x.get('session_date') == sid for x in hist):
        return
    total = sum((p.get('return') or 0) for p in state['picks']) / max(1, len(state['picks']))
    hist.append({
        'session_date': sid,
        'result_pct': total,
        'picks': [
            {'trader_id': p['trader_id'], 'name': p['name'], 'code': p['code'], 'return': p.get('return'), 'exit_reason': p.get('exit_reason')}
            for p in state['picks']
        ]
    })
    hist = sorted(hist, key=lambda x: x['session_date'], reverse=True)[:30]
    save_json(HISTORY_FILE, hist)


def update_state_market(state):
    """Refresh prices and progress each one-day paper position."""
    global last_error
    session = date.fromisoformat(state['session_date'])
    changed = False
    for p in state['picks']:
        try:
            q = yahoo_quote(p['code'])
            p['price'] = q['price']
            p['quote_timestamp'] = q['timestamp']
            p['quote_source'] = q['source']
            p['error'] = None
            if state['status'] == 'WAITING_ENTRY' and market_is_open_for_session(session):
                p['entry'] = q['price']
                p['entry_time'] = now_kst().isoformat()
                p['status'] = 'RUNNING'
                changed = True
            if p.get('entry') is not None and p.get('status') in ('RUNNING', 'WAITING_ENTRY'):
                r = (q['price'] / p['entry'] - 1) * 100 if p['entry'] else 0
                p['return'] = r
                if r >= GOAL_PCT:
                    p['exit'] = p['entry'] * (1 + GOAL_PCT/100)
                    p['return'] = GOAL_PCT
                    p['exit_reason'] = '목표 +5% 도달'
                    p['exit_time'] = now_kst().isoformat()
                    p['status'] = 'TARGET_HIT'
                    changed = True
                elif r <= STOP_PCT:
                    p['exit'] = p['entry'] * (1 + STOP_PCT/100)
                    p['return'] = STOP_PCT
                    p['exit_reason'] = '손절 -3% 도달'
                    p['exit_time'] = now_kst().isoformat()
                    p['status'] = 'STOPPED'
                    changed = True
                elif is_after_exit(session):
                    p['exit'] = q['price']
                    p['exit_reason'] = '장 마감 청산'
                    p['exit_time'] = now_kst().isoformat()
                    p['status'] = 'CLOSED'
                    changed = True
        except Exception as exc:
            p['error'] = str(exc)
            last_error = str(exc)
    if all(p.get('status') in ('CLOSED', 'TARGET_HIT', 'STOPPED') for p in state['picks']):
        state['status'] = 'DONE' if any(p.get('status') == 'CLOSED' for p in state['picks']) else 'STOPPED'
        changed = True
        push_history_if_needed(state)
    elif any(p.get('status') == 'RUNNING' for p in state['picks']):
        state['status'] = 'RUNNING'
    if changed:
        save_state(state)
    return state


def public_payload():
    state = load_state()
    state = update_state_market(state)
    rows = []
    for p in state['picks']:
        r = p.get('return')
        progress = ((r or 0) / GOAL_PCT) * 100 if r is not None else 0
        rows.append({
            **p,
            'trader_name': TRADERS[p['trader_id']]['name'],
            'role': TRADERS[p['trader_id']]['title'],
            'target_pct': GOAL_PCT,
            'stop_pct': STOP_PCT,
            'progress': progress,
        })
    rank_rows = sorted(rows, key=lambda x: (-999 if x['return'] is None else x['return']), reverse=True)
    for i, p in enumerate(rank_rows, 1):
        p['rank'] = i
    hist = load_history()
    return {
        'session_date': state['session_date'],
        'selected_at': state['selected_at'],
        'status': state['status'],
        'goal_pct': GOAL_PCT,
        'stop_pct': STOP_PCT,
        'picks': rank_rows,
        'history': hist[:10],
        'updated_at': now_kst().isoformat(),
        'server': 'DAILY PAPER TRADING · Yahoo Finance KSE delayed data',
        'last_error': last_error,
        'rules': {
            'entry': '거래일 09:00 이후 첫 정상 시세를 가상 진입가로 고정',
            'target': '+5% 도달 시 목표 청산',
            'stop': '-3% 도달 시 손절 청산',
            'end': '15:20까지 목표/손절 미달성 시 최신 지연시세로 장마감 청산'
        },
        'next_market_day': next_trading_day(now_kst().date()).isoformat()
    }


class Handler(BaseHTTPRequestHandler):
    def send_data(self, code, body, content_type='application/json; charset=utf-8'):
        raw = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health':
            self.send_data(200, json.dumps({'ok': True, 'mode': 'daily-paper-yahoo-delayed'}))
            return
        if path == '/api/arena':
            self.send_data(200, json.dumps(public_payload(), ensure_ascii=False))
            return
        if path == '/api/config':
            self.send_data(200, json.dumps({'provider': 'Yahoo Finance', 'delayed': True, 'poll_seconds': POLL_SECONDS, 'goal_pct': GOAL_PCT, 'stop_pct': STOP_PCT}, ensure_ascii=False))
            return
        if path in ('/', '/trader-arena.html'):
            f = BASE_DIR / 'trader-arena.html'
            self.send_data(200, f.read_bytes(), 'text/html; charset=utf-8')
            return
        self.send_data(404, 'Not found', 'text/plain; charset=utf-8')

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/new-session':
            session = current_session_date()
            state = new_state(session)
            save_state(state)
            self.send_data(200, json.dumps({'ok': True, 'session_date': session.isoformat()}, ensure_ascii=False))
            return
        if path == '/api/reset-today':
            state = load_state()
            for p in state['picks']:
                p.update({'entry': None, 'exit': None, 'exit_reason': None, 'entry_time': None, 'exit_time': None, 'return': None, 'status': 'WAITING_ENTRY'})
            state['status'] = 'WAITING_ENTRY'
            save_state(state)
            self.send_data(200, json.dumps({'ok': True}, ensure_ascii=False))
            return
        self.send_data(404, 'Not found', 'text/plain; charset=utf-8')

    def log_message(self, fmt, *args):
        print(f'[{now_kst().strftime("%H:%M:%S")}] {self.address_string()} - {fmt % args}', flush=True)


def main():
    DATA_DIR.mkdir(exist_ok=True)
    # Create the first daily session immediately.
    load_state()
    print('=' * 70)
    print('TRADER ARENA · DAILY PAPER TRADING')
    print(f'주소: http://127.0.0.1:{PORT}')
    print('목표: +5% · 허용 손실: -3% · 당일 진입/당일 청산')
    print('시세: Yahoo Finance KSE · 지연 시세')
    print('오늘 선정 → 다음 거래일 첫 시세를 진입가로 기록')
    print('이 창을 닫으면 서버가 멈춥니다.')
    print('=' * 70)
    httpd = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n서버를 종료합니다.')
    finally:
        httpd.server_close()

if __name__ == '__main__':
    main()

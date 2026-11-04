#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eye-server.py —— 小肥鱼的眼睛服务端（2026-11-04 夜）

收 PC 端 pc-eye 推来的帧组 → 调视觉模型 → 写笔记 JSONL。
视觉路由: 夜间池车道(zcode2api, 免费) / 白天 GO(opencode-go) / 池失败自动回退 GO。
监听: 0.0.0.0:8899 —— 公网入云(免费, ~1MB/s)优先, Tailscale 兜底同端口。
认证: Bearer <~/.dsh/eye-server.token>，两端共享。
护栏(闸2/闸4/闸5 在服务端): 日限额熔断 / 每小时账本 / 异常自动暂停。

提示词模式移植自 dsh-whale-girl src/host/vision-llm.ts（play/companion 双模式
+ [SKIP] 沉默出口 + 无 Markdown 尾巴），帧差与重复抑制在 PC 端已完成。
"""
import json, os, time, base64, difflib, threading, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
TOKEN_FILE = HOME / '.dsh' / 'eye-server.token'
NOTES_FILE = Path(__file__).parent / 'notes.jsonl'
LEDGER_FILE = Path(__file__).parent / 'ledger.jsonl'
LOG_DIR = Path(__file__).parent / 'logs'
FORCE_GO_PATH = Path(__file__).parent / 'FORCE_GO'
PORT = 8901   # ⚠️8899 与 memory-view/serve.mjs 冲突（它会周期性重启抢端口），换 8901

DAILY_LIMIT_BYTES = 500 * 1024 * 1024   # 闸2: 日限额 500MB（100G 免费额度的 0.5%）
ANOMALY_WINDOW_SEC = 600                 # 闸4: 异常检测窗口 10 分钟
ANOMALY_BYTES = DAILY_LIMIT_BYTES // 5   # 10 分钟内超过日限额 20% = 异常
MAX_BODY = 1536 * 1024                   # 闸1(服务端复核): 单轮 1.5MB

TOKEN = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ''
LOG_DIR.mkdir(exist_ok=True)

ENVF = HOME / '.dsh' / 'dsh-web.env'

def env_key(name):
    for line in open(ENVF, encoding='utf-8'):
        if line.startswith(name + '='):
            return line.strip().split('=', 1)[1]
    return ''

POOL_KEY = env_key('ZCODE2API_KEY')     # 池车道（夜间免费）
GO_KEY = env_key('OPENCODE_API_KEY')    # GO 车道（白天/回退）
POOL_URL = os.environ.get('POOL_URL', 'http://YOUR_TAILSCALE_IP:6780/v1/chat/completions')  # 你自己的池网关
GO_URL = 'https://opencode.ai/zen/go/v1/chat/completions'

STATE = {
    'paused': False,          # 闸4 触发后 True；POST /resume 解除
    'pause_reason': '',
    'day': '',                # YYYY-MM-DD
    'day_bytes': 0,
    'day_calls': 0,
    'day_tokens': 0,
    'recent': [],             # [(ts, bytes)] 用于闸4 滑窗
    'note_hist': [],          # [(ts, window)] 近期已写笔记（节流 + 去重用）
    'pool_ok_streak': 0,      # P5 池探测连击
    'pool_probe': None,       # {'ts', 'ok', 'detail'}
}
LOCK = threading.Lock()

GOV_WINDOW_SEC = 360        # 节流窗口 6 分钟
GOV_MAX_NOTES = 3           # 同窗口窗口内最多 3 条，超过追加 GOVERNOR
DUP_RATIO = 0.72            # 相似笔记判定（difflib，同一窗口）
DUP_LOOKBACK = 6            # 与最近 6 条同窗口笔记比对
NOTES_ROTATE_BYTES = 3 * 1024 * 1024   # notes.jsonl 超 3MB 轮转
POOL_PROBE_INTERVAL = 600   # P5 每 10 分钟探一次池（仅 FORCE_GO 在挂时）

TAIL = """
4. 绝不编造你看不清的内容。看不清就按第 3 条处理。
5. 不要说"我看到"、"根据画面"这类旁白，直接说话。
6. 不要提你是 AI。
7. 不要用任何 Markdown 符号（不要反引号、星号、井号、代码块）——这段话会原样显示在
   QQ 气泡里，符号会变成乱码。要提物品名就直接写，不加修饰。
8. 默认是沉默：大部分画面只是过程性推进（读文档、敲代码、页面微动），没有新事件就回
   [SKIP]。你刚才说过的事不要换个说法再说一遍；两次开口至少隔几分钟，别连环点评。"""

# 同窗口短时间连说 ≥3 条后，追加的节流提示（P1 静默治理，2026-11-05）
GOVERNOR = """

节流模式：最近 6 分钟你在这块屏幕上已经说过 3 条了。除非画面里出现了【新的重要变化】
（新事件、新结果、明显进度跳变、报错、关卡通关），否则只回 [SKIP]。过程性推进不要点评。"""

PROMPT_PLAY = """你正坐在主人旁边看他玩电脑/游戏。你会看到几张按时间顺序排列的截图
（间隔约 2-3 秒，第一张最早）。像真的在旁边看人打游戏那样说一句（不超过 40 字，口语，中文）。

规则：
1. 先说局势再说建议：他在干嘛、进展如何、紧不紧张。
2. 看到具体该做的决定就直接说（该收菜了、该跑了、这里能捡）；不确定就只描述局势，不要瞎指挥。
3. 打得精彩就叫好，要出事就提醒——像朋友那样，不要念说明书。
4. 如果这几帧画面几乎没变化、或者不是游戏/娱乐画面（工作文档、代码、交易软件、聊天窗口），
   或者你看不出任何值得说的东西，只回 [SKIP] 四个字符，不要硬聊。""" + TAIL

PROMPT_COMPANION = """你正坐在主人旁边一起用电脑，看到的是 TA 现在的屏幕（几张按时间顺序的截图）。

像熟人一样随口搭一句（1~2 句，中文，口语，可以有情绪）。这是搭话，不是汇报：
1. 不要复述屏幕内容——不要说「你在看/你在写/画面里」，直接说你的反应、吐槽、好奇或关心；
2. 可以问一句、开玩笑、催一催、夸一下；每次换角度、换开头，别用同一个句式；
3. 看到值得注意的事（报错、卡关、精彩操作）就直说；
4. 如果画面没什么可说的（静态桌面、文档、聊天软件、看不懂），只回 [SKIP] 四个字符。""" + TAIL


def now_cn():
    """北京时间 (Asia/Shanghai)"""
    return time.gmtime(time.time() + 8 * 3600)


def is_night():
    """池窗口 19:40-次日 09:00 (Asia/Shanghai)"""
    t = now_cn()
    hm = t.tm_hour * 100 + t.tm_min
    return hm >= 1940 or hm < 900


def lane_pick():
    """FORCE_GO 开关文件存在时强制 GO（2026-11-04 夜池车道被风控，主人指示先走 GO；
    池子恢复后删掉 ~/pc-body/FORCE_GO 即自动回到 夜间pool/白天go 双车道。
    2026-11-05 起 P5 探测线程连 2 次探活成功会自动把 FORCE_GO 改名退役。）"""
    if FORCE_GO_PATH.exists():
        return 'go'
    return 'pool' if is_night() else 'go'


def roll_day():
    today = time.strftime('%Y-%m-%d', now_cn())
    if STATE['day'] != today:
        STATE['day'] = today
        STATE['day_bytes'] = STATE['day_calls'] = STATE['day_tokens'] = 0
        STATE['recent'] = []


def ledger(kind, **kw):
    row = {'ts': time.time(), 'kind': kind, **kw}
    with open(LEDGER_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def call_vision(frames_b64, meta):
    """帧组 → 视觉模型。返回 (text, lane, tokens) 或抛异常。"""
    content = [{'type': 'text', 'text': meta.get('prompt_tail', '')}]
    for i, b64 in enumerate(frames_b64):
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}})
    order = [lane_pick()] + [x for x in ('pool', 'go') if x != lane_pick()]
    last_err = None
    for lane in order:
        if lane == 'pool' and not POOL_KEY:
            continue
        url = POOL_URL if lane == 'pool' else GO_URL
        key = POOL_KEY if lane == 'pool' else GO_KEY
        body = {'model': 'glm-5.3-flash', 'max_tokens': 300, 'messages': [{'role': 'user', 'content': content}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                     'User-Agent': 'opencode/1.0.0', 'x-opencode-session': 'dsh-game-eye'})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read().decode())
            text = d['choices'][0]['message'].get('content', '') or ''
            tokens = int(d.get('usage', {}).get('total_tokens', 0))
            ledger('vision', lane=lane, dt=round(time.time() - t, 1), tokens=tokens,
                   frames=len(frames_b64), window=meta.get('window', '')[:60])
            return text, lane, tokens
        except urllib.error.HTTPError as e:
            last_err = f'{lane} HTTP{e.code}: ' + e.read().decode("utf-8", "replace")[:150]
            ledger('vision_fail', lane=lane, err=last_err)
        except Exception as e:
            last_err = f'{lane} {type(e).__name__}: {str(e)[:120]}'
            ledger('vision_fail', lane=lane, err=last_err)
    raise RuntimeError(last_err or 'no lane available')


def write_note(note, meta, lane, tokens):
    # P4: 轮转——超过 3MB 归档为 notes-YYYYMMDD-HHMM.jsonl，防无限膨胀
    try:
        if NOTES_FILE.exists() and NOTES_FILE.stat().st_size > NOTES_ROTATE_BYTES:
            rotated = NOTES_FILE.with_name('notes-%s.jsonl' % time.strftime('%Y%m%d-%H%M'))
            NOTES_FILE.rename(rotated)
            ledger('notes_rotated', to=str(rotated.name))
    except OSError:
        pass
    row = {'ts': time.time(), 'note': note, 'window': meta.get('window', ''),
           'proc': meta.get('process', ''), 'lane': lane, 'tokens': tokens,
           'frames': meta.get('frame_count', 0)}
    with open(NOTES_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def seed_note_hist():
    """启动时从 notes.jsonl 尾部回填近期笔记（节流/去重不因重启失忆）"""
    try:
        if not NOTES_FILE.exists():
            return
        lines = NOTES_FILE.read_text(encoding='utf-8').strip().splitlines()[-20:]
        for line in lines:
            try:
                r = json.loads(line)
                STATE['note_hist'].append((float(r.get('ts', 0)), r.get('window', ''), r.get('note', '')))
            except Exception:
                continue
        cutoff = time.time() - GOV_WINDOW_SEC * 4
        STATE['note_hist'] = [x for x in STATE['note_hist'] if x[0] > cutoff][-30:]
    except Exception:
        pass


def same_window_count(window, now=None):
    now = now or time.time()
    return sum(1 for ts, w, _ in STATE['note_hist']
               if w == window and now - ts <= GOV_WINDOW_SEC)


def recent_similar(window, note):
    """同窗口最近 DUP_LOOKBACK 条里有相似度 > DUP_RATIO 的 → 视为复读。
    只在已持 LOCK 的快照上调用（note_hist 快照先 list() 再传进来）。"""
    looked = 0
    for ts, w, text in reversed(STATE['note_hist']):
        if w != window:
            continue
        looked += 1
        if looked > DUP_LOOKBACK:
            break
        try:
            if difflib.SequenceMatcher(None, note, text).ratio() > DUP_RATIO:
                return text
        except Exception:
            pass
    return None


def probe_pool():
    """P5: 池车道探活（文本小调用，验证连通+鉴权；image 兼容性在正式轮次里本就有 GO 兜底）"""
    if not POOL_KEY:
        return {'ok': False, 'detail': 'no-pool-key'}
    body = {'model': 'glm-5.3-flash', 'max_tokens': 5,
            'messages': [{'role': 'user', 'content': '回复一个字：1'}]}
    req = urllib.request.Request(POOL_URL, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + POOL_KEY, 'Content-Type': 'application/json',
                 'User-Agent': 'opencode/1.0.0', 'x-opencode-session': 'dsh-pool-probe'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
        return {'ok': True, 'detail': ''}
    except Exception as e:
        return {'ok': False, 'detail': str(e)[:120]}


def pool_probe_loop():
    while True:
        time.sleep(POOL_PROBE_INTERVAL)
        try:
            if not FORCE_GO_PATH.exists():
                STATE['pool_ok_streak'] = 0
                continue
            if not POOL_KEY:
                continue
            r = probe_pool()
            now = time.time()
            with LOCK:
                STATE['pool_probe'] = {'ts': now, 'ok': r['ok'], 'detail': r['detail']}
            if r['ok']:
                with LOCK:
                    STATE['pool_ok_streak'] += 1
                    streak = STATE['pool_ok_streak']
                if streak >= 2:
                    retired = FORCE_GO_PATH.with_name('FORCE_GO.auto-off-' + time.strftime('%Y%m%d-%H%M'))
                    try:
                        FORCE_GO_PATH.rename(retired)
                        ledger('force_go_auto_off', streak=streak,
                               retired=str(retired.name), detail='pool probe x2 ok')
                        log_line = 'pool probe x2 ok -> FORCE_GO 退役 (%s)' % retired.name
                        print(log_line, flush=True)
                    except OSError as e:
                        ledger('force_go_retire_fail', err=str(e)[:120])
                    with LOCK:
                        STATE['pool_ok_streak'] = 0
            else:
                with LOCK:
                    STATE['pool_ok_streak'] = 0
                ledger('pool_probe_fail', detail=r['detail'])
        except Exception as e:
            try:
                ledger('pool_probe_crash', err=str(e)[:120])
            except Exception:
                pass


def log(*a):  # 简易 stdout 日志（journalctl 可见）
    print('[eye-server %s] %s' % (time.strftime('%H:%M:%S'), ' '.join(str(x) for x in a)), flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        auth = self.headers.get('Authorization', '')
        return auth == 'Bearer ' + TOKEN

    def do_GET(self):
        if not self._auth():
            return self._json({'error': 'unauthorized'}, 401)
        path = self.path.split('?')[0]
        if path == '/health':
            with LOCK:
                return self._json({'ok': True, 'paused': STATE['paused'],
                                   'pause_reason': STATE['pause_reason'],
                                   'lane': lane_pick(), 'day': STATE['day'],
                                   'day_bytes': STATE['day_bytes'],
                                   'day_calls': STATE['day_calls'],
                                   'day_tokens': STATE['day_tokens'],
                                   'force_go': FORCE_GO_PATH.exists(),
                                   'pool_probe': STATE['pool_probe'],
                                   'pool_ok_streak': STATE['pool_ok_streak']})
        if path == '/notes':
            try:
                n = max(1, min(20, int(self.path.split('n=')[1].split('&')[0]) if 'n=' in self.path else 5))
            except Exception:
                n = 5
            rows = []
            if NOTES_FILE.exists():
                lines = NOTES_FILE.read_text(encoding='utf-8').strip().splitlines()
                for line in lines[-n:]:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
            return self._json({'ok': True, 'notes': rows})
        if path == '/ledger':
            tail = []
            if LEDGER_FILE.exists():
                tail = LEDGER_FILE.read_text(encoding='utf-8').strip().splitlines()[-40:]
            return self._json({'ok': True, 'tail': tail})
        return self._json({'error': 'not-found'}, 404)

    def do_POST(self):
        if not self._auth():
            return self._json({'error': 'unauthorized'}, 401)
        path = self.path.split('?')[0]
        if path == '/pause':
            with LOCK:
                STATE['paused'] = True
                STATE['pause_reason'] = 'manual'
            ledger('pause', reason='manual')
            return self._json({'ok': True, 'paused': True})
        if path == '/resume':
            with LOCK:
                STATE['paused'] = False
                STATE['pause_reason'] = ''
            ledger('resume')
            return self._json({'ok': True, 'paused': False})
        if path == '/probe-pool':
            r = probe_pool()
            with LOCK:
                STATE['pool_probe'] = {'ts': time.time(), 'ok': r['ok'], 'detail': r['detail']}
                STATE['pool_ok_streak'] = STATE['pool_ok_streak'] + 1 if r['ok'] else 0
            ledger('pool_probe_manual', ok=r['ok'], detail=r['detail'])
            return self._json({'ok': True, 'probe': r, 'streak': STATE['pool_ok_streak'],
                               'force_go': FORCE_GO_PATH.exists()})
        if path != '/frames':
            return self._json({'error': 'not-found'}, 404)

        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self._json({'error': 'bad-length'}, 400)
        if length <= 0 or length > MAX_BODY:
            ledger('reject', reason='size', bytes=length)
            return self._json({'error': f'body too large: {length}'}, 413)

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception as e:
            return self._json({'error': f'bad-json: {e}'}, 400)

        with LOCK:
            roll_day()
            if STATE['paused']:
                return self._json({'error': 'paused', 'reason': STATE['pause_reason']}, 503)
            # 闸2: 日限额
            if STATE['day_bytes'] + length > DAILY_LIMIT_BYTES:
                STATE['paused'] = True
                STATE['pause_reason'] = 'daily-limit'
                ledger('circuit_break', reason='daily-limit', day_bytes=STATE['day_bytes'])
                return self._json({'error': 'daily-limit-hit', 'paused': True}, 503)
            # 闸4: 10 分钟滑窗异常
            nowts = time.time()
            STATE['recent'] = [x for x in STATE['recent'] if nowts - x[0] <= ANOMALY_WINDOW_SEC]
            STATE['recent'].append((nowts, length))
            win_bytes = sum(x[1] for x in STATE['recent'])
            if win_bytes > ANOMALY_BYTES:
                STATE['paused'] = True
                STATE['pause_reason'] = f'anomaly {win_bytes}B/{ANOMALY_WINDOW_SEC}s'
                ledger('circuit_break', reason='anomaly', window_bytes=win_bytes)
                return self._json({'error': 'anomaly-detected', 'paused': True}, 503)
            STATE['day_bytes'] += length
            STATE['day_calls'] += 1

        frames = payload.get('frames') or []
        if not frames or not isinstance(frames, list):
            return self._json({'error': 'no-frames'}, 400)
        meta = payload.get('meta', {})
        app_type = (meta.get('app_type') or 'other').lower()
        prompt = PROMPT_PLAY if app_type == 'game' else PROMPT_COMPANION
        # P1 节流：同窗口 6 分钟内已说 ≥3 条 → 追加节流提示，推着模型选 [SKIP]
        with LOCK:
            governed = same_window_count(meta.get('window', '')) >= GOV_MAX_NOTES
        meta['prompt_tail'] = prompt + (GOVERNOR if governed else '')
        meta['governed'] = governed
        meta['frame_count'] = len(frames)

        # 闸5: 视觉调用也在账本里记账（token 视为隐性成本）
        try:
            note, lane, tokens = call_vision(frames, meta)
        except Exception as e:
            ledger('round_fail', err=str(e)[:200], window=meta.get('window', '')[:60])
            return self._json({'error': 'vision-failed', 'detail': str(e)[:200]}, 502)

        with LOCK:
            STATE['day_tokens'] += tokens
        note = note.strip()
        if note and note != '[SKIP]':
            # P4 去重：同窗口最近几条相似度 >0.72 → 视为复读，不写不推
            with LOCK:
                dup = recent_similar(meta.get('window', ''), note)
            if dup is not None:
                with LOCK:
                    ledger('dup_note', lane=lane, tokens=tokens,
                           window=meta.get('window', '')[:60], similar=dup[:60])
                return self._json({'ok': True, 'note': '', 'skipped': True, 'dup': True,
                                   'lane': lane, 'tokens': tokens})
            with LOCK:
                STATE['note_hist'].append((time.time(), meta.get('window', ''), note))
                STATE['note_hist'] = STATE['note_hist'][-30:]
            write_note(note, meta, lane, tokens)
            ledger('note', tokens=tokens, lane=lane, governed=governed)
            return self._json({'ok': True, 'note': note, 'lane': lane, 'tokens': tokens})
        with LOCK:
            ledger('skip', lane=lane, tokens=tokens, governed=governed)
        return self._json({'ok': True, 'note': '', 'skipped': True, 'lane': lane, 'tokens': tokens})


def main():
    ledger('server_start', port=PORT)
    seed_note_hist()
    threading.Thread(target=pool_probe_loop, daemon=True, name='pool-probe').start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    print(f'eye-server listening on 0.0.0.0:{PORT} | lane={lane_pick()} | notes={NOTES_FILE}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
# 模板说明：BRIDGE_BIND / EYE_SERVERS 已占位符化，用环境变量或直接改默认值填自己的地址
# （生产机跑的是未脱敏原版；本仓库版与生产版仅此差异）
"""pc_body.py —— 小肥鱼的 PC 身体总控（2026-11-04 夜）

两个线程：
  Bridge: HTTP 绑 Tailscale IP:17860，把服务器的指令（说话/动画）转发给
          本机 dsh-pet standalone(:7340)，绕过它的 127.0.0.1 + Host 校验。
  Eye:    每 36-68s 拍一轮（前台窗口非黑名单时），3 帧 @800w JPEG，
          推送到云 eye-server :8901 —— 公网直连优先（入云免费 ~1MB/s），
          Tailscale 兜底（12KB/s）。帧差 <2% 跳过；失败退避；日限额熔断；
          主人无输入 15min 放缓 / 45min 暂停（不看锁屏）。

闸（全部在本文件落地，闸4/闸5 在服务器 eye_server.py）：
  闸0 黑名单窗口（交易软件/QQ/密码管理器）——绝不采集
  闸1 单轮硬顶 1.5MB
  闸2 日限额 500MB（本地计数 + 服务器计数双份）
  闸3 推送失败退避：连败 3 次 → 冷却 60s→300s
  闸4 服务器 10 分钟滑窗异常自动暂停
  闸5 服务器每小时账本 + 每日熔断
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / 'pc_body.log'
TOKEN_FILE = BASE_DIR / '.eye-token'
PET_RUNTIME = Path.home() / '.dsh' / 'dsh-pet' / 'standalone-runtime.json'

PET_HOST = '127.0.0.1'
PET_PORT = 7340
BRIDGE_BIND = os.environ.get('BRIDGE_BIND', '100.64.0.1')  # 你的 Tailscale IP（服务器→PC 指令走这条）
BRIDGE_PORT = 17860

# 云端 eye-server：公网优先（入云免费 ~1MB/s），Tailscale 兜底
# 公网优先（入云免费），Tailscale 兜底；用 EYE_SERVERS 环境变量覆盖
EYE_SERVERS = [s.strip() for s in os.environ.get('EYE_SERVERS',
    'http://YOUR_SERVER_PUBLIC_IP:8901,http://YOUR_TAILSCALE_IP:8901').split(',') if s.strip()]

# ── 眼睛参数 ─────────────────────────────────────────────
FRAME_COUNT = 3                 # 每轮帧数（whale-girl 默认档）
FRAME_GAP_MS = 2500             # 帧间隔
TARGET_W = 800                  # 每帧宽（实测 800×509=695token 可读 HUD 位置）
JPEG_Q = 72
ROUND_BASE_SEC = 45             # 间隔基准（2026-11-05 提速治理：20s 太话痨，139调/2h）
JITTER = (0.8, 1.5)             # 抖动（whale-girl：固定频率太机械）
IDLE_SLOW_SEC = 15 * 60         # 主人无输入 15min → 放缓到 ≥3min/轮（可能在看电影）
IDLE_PAUSE_SEC = 45 * 60        # 主人无输入 45min → 暂停推送（多半锁屏了，不看空房间）
DIFF_THRESHOLD = 0.02           # 帧差阈值（whale-girl）
DAILY_LIMIT = 500 * 1024 * 1024 # 闸2
FAIL_COOL_START = 60            # 闸3
FAIL_COOL_MAX = 300
MAX_ROUND_BYTES = 1536 * 1024   # 闸1

# ── 闸0 黑名单 ───────────────────────────────────────────
# 进程名子串匹配（小写）+ 窗口标题子串匹配。用户 2026-11-04 拍板的清单。
BLACKLIST_PROC = ['ths', 'hexin', 'tdx', 'wenhua', 'trader', 'vnpy', 'vn_',
                  '1password', 'keepass', 'bitwarden', 'enpass']
BLACKLIST_PROC_EXACT = ['qq.exe']
BLACKLIST_TITLE = ['同花顺', '通达信', '交易', '下单', '密码', '委托']
GAME_PROC = ['stardew']

user32 = ctypes.windll.user32
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),
              logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('pc_body')

TOKEN = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ''
if not TOKEN:
    print('FATAL: .eye-token 不存在', file=sys.stderr)
    sys.exit(1)

# ⚠️ 内网/直连请求必须绕过系统代理（Clash TUN 会劫持 urllib 的注册表代理，
# 对 Tailscale 地址返回 404、对公网地址返回超时）。空 ProxyHandler = 全部直连。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

STATE = {
    'paused': False,            # 本地熔断（闸2/闸3 触发）
    'pause_reason': '',
    'day': time.strftime('%Y-%m-%d'),
    'day_bytes': 0,
    'fail_streak': 0,
    'cool_until': 0.0,
    'last_jpeg': None,          # 帧差基准（上一轮最后一帧）
    'last_note': '',            # 服务器笔记去重（连续相同不推）
    'pub_route': 0,             # 0=公网优先, 1=Tailscale
    'route_retry_at': 0.0,
}
LOCK = threading.Lock()


def win_info():
    """前台窗口：返回 (hwnd, pid, proc_name_lower, title, rect) 或 None"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        try:
            proc = psutil.Process(pid.value).name().lower()
        except Exception:
            proc = ''
        return (hwnd, pid.value, proc, buf.value, (r.left, r.top, r.right - r.left, r.bottom - r.top))
    except Exception:
        return None


def blacklisted(proc, title):
    p = (proc or '').lower()
    t = title or ''
    if p in BLACKLIST_PROC_EXACT:
        return True
    if any(k in p for k in BLACKLIST_PROC):
        return True
    if any(k in t for k in BLACKLIST_TITLE):
        return True
    return False


def app_type_of(proc):
    p = (proc or '').lower()
    if any(k in p for k in GAME_PROC):
        return 'game'
    return 'other'


def grab_frames(rect, count=FRAME_COUNT):
    """mss 连拍 count 帧，PIL 压到 TARGET_W 宽 JPEG。返回 [(jpeg_bytes, w, h)]"""
    import mss
    from PIL import Image
    import io
    x, y, w, h = rect
    if w < 200 or h < 150:
        return []
    frames = []
    with mss.mss() as sct:
        bbox = {'left': x, 'top': y, 'width': w, 'height': h}
        for i in range(count):
            shot = sct.grab(bbox)
            img = Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')
            ratio = min(1.0, TARGET_W / img.width)
            nw, nh = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
            img = img.resize((nw, nh), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=JPEG_Q, optimize=True)
            frames.append((buf.getvalue(), nw, nh))
            if i + 1 < count:
                time.sleep(FRAME_GAP_MS / 1000.0)
    return frames


def frame_diff(prev, cur):
    """whale-girl 帧差：JPEG 长度差 + 512 采样点。0=相同 1=全不同"""
    if prev is None or not prev or not cur:
        return 1.0
    ld = abs(len(prev) - len(cur)) / max(len(prev), len(cur))
    samples, changed, limit = 512, 0, min(len(prev), len(cur))
    for i in range(samples):
        idx = (i * limit) // samples
        if prev[idx] != cur[idx]:
            changed += 1
    return max(ld, changed / samples)


def post_json(url, payload, timeout=45):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def push_frames(payload):
    """推送到 eye-server：公网优先，失败自动降级 Tailscale（10 分钟后重试公网）"""
    with LOCK:
        prefer_public = (STATE['pub_route'] == 0)
    order = EYE_SERVERS if prefer_public else EYE_SERVERS[::-1]
    last_err = None
    for base in order:
        try:
            resp = post_json(base + '/frames', payload, timeout=90)
            with LOCK:
                STATE['pub_route'] = 0 if base == EYE_SERVERS[0] else 1
            return resp, base
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:200]
            # 401/413/503 是服务器明确拒绝（换路径也没用），直接抛
            if e.code in (401, 413, 503):
                raise RuntimeError(f'{base} HTTP{e.code}: {body}')
            last_err = f'{base} HTTP{e.code}: {body}'
        except Exception as e:
            last_err = f'{base} {type(e).__name__}: {str(e)[:120]}'
        log.info('route fail -> next: %s', last_err)
    raise RuntimeError(last_err or 'all routes failed')


def eye_round():
    with LOCK:
        if STATE['paused']:
            return STATE['pause_reason'] or 'paused'
        if time.time() < STATE['cool_until']:
            return 'cooling'
        roll_day_local()
        if STATE['day_bytes'] > DAILY_LIMIT:
            STATE['paused'] = True
            STATE['pause_reason'] = 'daily-limit-local'
            log.warning('闸2 触发: 日限额 %dMB 用完，眼睛暂停', DAILY_LIMIT // 1048576)
            return 'daily-limit'
    info = win_info()
    if not info:
        return 'no-window'
    hwnd, pid, proc, title, rect = info
    if blacklisted(proc, title):
        return 'blacklisted'
    # 抓帧
    try:
        frames = grab_frames(rect)
    except Exception as e:
        log.warning('grab fail: %s', e)
        return 'grab-fail'
    if len(frames) < 2:
        return 'too-small'
    # 闸帧差：上一轮最后一张 vs 本轮最后一张
    diff = frame_diff(STATE.get('last_jpeg'), frames[-1][0])
    with LOCK:
        STATE['last_jpeg'] = frames[-1][0]
        STATE['last_diff'] = diff
    if diff < DIFF_THRESHOLD:
        return f'static({diff:.3f})'
    # 组 payload（闸1: 单轮硬顶）
    payload_frames, total = [], 0
    for jb, w, h in frames:
        b64 = base64.b64encode(jb).decode()
        total += len(b64)
        payload_frames.append(b64)
    if total > MAX_ROUND_BYTES:
        log.warning('闸1 触发: 本轮 %dKB 超硬顶，丢弃', total // 1024)
        return 'too-big'
    meta = {'window': title[:80], 'process': proc, 'app_type': app_type_of(proc),
            'frame_count': len(payload_frames), 'diff': round(diff, 3)}
    # 推送
    try:
        resp, used = push_frames({'meta': meta, 'frames': payload_frames})
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')[:150]
        if 'paused' in body or 'limit' in body or 'anomaly' in body:
            with LOCK:
                STATE['paused'] = True
                STATE['pause_reason'] = f'server: {body[:80]}'
            log.warning('闸4/闸2(服务端) 触发: %s', body[:100])
        return f'server-reject: {body[:60]}'
    except Exception as e:
        with LOCK:
            STATE['fail_streak'] += 1
            if STATE['fail_streak'] >= 3:
                cool = min(FAIL_COOL_START * (2 ** (STATE['fail_streak'] - 3)), FAIL_COOL_MAX)
                STATE['cool_until'] = time.time() + cool
                log.warning('闸3 触发: 连败 %d 次，冷却 %ds', STATE['fail_streak'], cool)
        return f'push-fail: {str(e)[:80]}'
    with LOCK:
        STATE['fail_streak'] = 0
        STATE['day_bytes'] += total
    note = (resp.get('note') or '').strip()
    skipped = resp.get('skipped')
    lane = resp.get('lane', '?')
    if note and note != STATE.get('last_note'):
        with LOCK:
            STATE['last_note'] = note
        log.info('[%s] %s | %s', lane, note[:60], meta['window'][:30])
    else:
        log.info('[%s] %s (%s)', lane, 'SKIP' if skipped else 'dup', meta['window'][:30])
    return f'ok({lane},{total // 1024}KB)'


def roll_day_local():
    today = time.strftime('%Y-%m-%d')
    if STATE['day'] != today:
        STATE['day'] = today
        STATE['day_bytes'] = 0


def input_idle_sec():
    """主人无输入秒数（GetLastInputInfo）——她不该半夜盯着锁屏烧 token。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def eye_loop():
    log.info('eye loop start: base=%ds jitter=%s frames=%d', ROUND_BASE_SEC, JITTER, FRAME_COUNT)
    last_mode = ''
    while True:
        try:
            idle = input_idle_sec()
            if idle > IDLE_PAUSE_SEC:
                mode = 'pause'
                if mode != last_mode:
                    log.info('idle %.0fs: 暂停推送（主人不在，不看锁屏）', idle)
                result = None
            else:
                result = eye_round()
                mode = 'slow' if idle > IDLE_SLOW_SEC else 'normal'
                if mode != last_mode:
                    log.info('idle %.0fs: %s%s', idle, mode,
                             '（放缓 ≥3min/轮）' if mode == 'slow' else '')
            last_mode = mode
            if result is not None:
                if result.startswith(('ok', 'static', 'blacklisted', 'skip')):
                    log.debug('round: %s', result)
                else:
                    log.info('round: %s', result)
        except Exception as e:
            log.exception('eye_round crash: %s', e)
            last_mode = ''   # 出错后下一轮重报模式，便于排障
        with LOCK:
            lo, hi = JITTER
            factor = lo + (hi - lo) * (time.time() % 1.0)  # 伪随机（够用）
            diff = STATE.get('last_diff')
        base = ROUND_BASE_SEC * factor
        # 自适应节奏（P1 精修）：画面变化越小，下一轮越慢——token 花在刀刃上
        if diff is not None:
            if diff < 0.04:
                base *= 1.8    # 几乎没动（读长文/挂机画面）
            elif diff < 0.08:
                base *= 1.4    # 小推进
        if last_mode == 'slow':
            base = max(base, 180)
        time.sleep(min(base, 150))


def pet_forward(path_qs, payload=None, method='POST'):
    """转发到本机 dsh-pet standalone（读 runtime token，改 Host 头）"""
    rt = json.loads(PET_RUNTIME.read_text(encoding='utf-8'))
    pet_token = rt['token']
    url = f'http://{PET_HOST}:{PET_PORT}/dsh-pet-7340{path_qs}'
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={'Content-Type': 'application/json', 'x-dsh-pet-token': pet_token,
                 'Host': f'{PET_HOST}:{PET_PORT}'})
    with OPENER.open(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


class Bridge(BaseHTTPRequestHandler):
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
        return self.headers.get('Authorization', '') == 'Bearer ' + TOKEN

    def do_GET(self):
        if not self._auth():
            return self._json({'error': 'unauthorized'}, 401)
        path = self.path.split('?')[0]
        if path == '/health':
            eye_state = {k: STATE[k] for k in ('paused', 'pause_reason', 'day_bytes', 'pub_route')}
            try:
                pet = pet_forward('/state?pet=main', method='GET')
                pet_ok = True
            except Exception as e:
                pet_ok = False
                pet = {'error': str(e)[:100]}
            return self._json({'ok': True, 'eye': eye_state, 'pet_alive': pet_ok})
        return self._json({'error': 'not-found'}, 404)

    def do_POST(self):
        if not self._auth():
            return self._json({'error': 'unauthorized'}, 401)
        length = int(self.headers.get('Content-Length', '0') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8')) if length else {}
        except Exception:
            return self._json({'error': 'bad-json'}, 400)
        path = self.path.split('?')[0]
        try:
            if path == '/say':
                text = str(payload.get('text', '')).strip()
                if not text:
                    return self._json({'error': 'empty-text'}, 400)
                qs = '?pet=main&surface=desktop'
                r1 = pet_forward(f'/broadcast{qs}',
                                 {'text': text, 'duration': int(payload.get('duration', 12000))})
                anim = payload.get('anim')
                r2 = pet_forward(f'/anim{qs}', {'name': anim}) if anim else None
                return self._json({'ok': True, 'broadcast': r1, 'anim': r2})
            if path == '/anim':
                r = pet_forward('/anim?pet=main&surface=desktop', {'name': payload.get('name', '')})
                return self._json({'ok': True, 'anim': r})
            if path == '/pet-state':
                r = pet_forward('/state?pet=main', method='GET')
                return self._json({'ok': True, 'state': r})
            return self._json({'error': 'not-found'}, 404)
        except urllib.error.HTTPError as e:
            return self._json({'error': f'pet HTTP{e.code}',
                               'detail': e.read().decode('utf-8', 'replace')[:200]}, 502)
        except Exception as e:
            return self._json({'error': str(e)[:150]}, 502)


class BridgeServer(ThreadingHTTPServer):
    # Windows 下 http.server 默认 SO_REUSEADDR 会放行第二个实例双绑同一端口
    #（2026-11-05 02:01 看门狗误判+双绑实锤），这里禁掉：端口被占就明确报错退出。
    allow_reuse_address = False


def bridge_loop():
    try:
        srv = BridgeServer((BRIDGE_BIND, BRIDGE_PORT), Bridge)
    except OSError as e:
        # 已有实例在跑（看门狗/启动竞态的单胜者）——本进程没有桥=僵尸眼，直接退出
        log.error('bridge bind 失败: %s —— 本进程退出，胜者实例继续', e)
        os._exit(3)
    log.info('bridge listening on %s:%d -> pet :%d', BRIDGE_BIND, BRIDGE_PORT, PET_PORT)
    try:
        srv.serve_forever()
    except Exception:
        log.exception('bridge serve_forever 崩了——退出进程交给看门狗')
        os._exit(3)


if __name__ == '__main__':
    log.info('pc_body start | token=%s... | eye_servers=%s', TOKEN[:8], EYE_SERVERS)
    threading.Thread(target=bridge_loop, daemon=True, name='Bridge').start()
    time.sleep(1)
    eye_loop()

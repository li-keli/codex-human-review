#!/usr/bin/env python3
"""本地富文本复核服务及 Codex 调用入口，仅使用 Python 标准库。"""
import copy
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import uuid
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = frozenset(('answered', 'paused', 'timed_out', 'completed'))


def notify_macos(question, thread_id=None):
    """通知点击仅打开可信任务链接；文案通过 argv 传递，不执行 shell。"""
    if sys.platform != 'darwin':
        return {'status': 'unsupported', 'message': '当前系统不支持 macOS 通知'}
    try:
        thread_id = str(uuid.UUID(thread_id or ''))
    except (ValueError, TypeError, AttributeError):
        return {'status': 'failed', 'message': '缺少有效的 Codex 任务 ID，未发送无法返回任务的通知'}
    binary = shutil.which('terminal-notifier')
    if not binary:
        binary = next((str(p) for p in (Path('/opt/homebrew/bin/terminal-notifier'),
                                       Path('/usr/local/bin/terminal-notifier')) if p.is_file()), None)
    if not binary:
        return {'status': 'failed', 'message': '请先安装通知依赖：brew install terminal-notifier'}
    target = f'codex://threads/{thread_id}'
    title = '人工复核 · 等待你的决定'
    message = f"第 {question.get('round', 1)} 轮 · 剩余 {question.get('remaining', 1)} 项\n{question['question'][:180]}\n点击返回 Codex 审核任务。"
    try:
        result = subprocess.run([binary, '-title', title, '-message', message,
                                 '-sound', 'Glass', '-group', f'codex-human-review-{thread_id}',
                                 '-open', target], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {'status': 'failed', 'message': result.stderr.strip()[:300] or '系统通知请求失败'}
        return {'status': 'submitted', 'target': target,
                'message': '已向 macOS 提交通知请求；点击返回对应 Codex 任务'}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'status': 'failed', 'message': str(error)[:300]}


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def validate(q):
    if not isinstance(q, dict):
        raise ValueError('题目必须为对象')
    for key in ('id', 'question'):
        if not isinstance(q.get(key), str) or not q[key].strip():
            raise ValueError(f'缺少 {key}')
    if q.get('type', 'single') not in ('single', 'multi', 'text'):
        raise ValueError('type 仅支持 single/multi/text')
    options = q.get('options', [])
    if not isinstance(options, list):
        raise ValueError('options 必须为数组')
    if q.get('type') != 'text' and not options:
        raise ValueError('选择题必须包含选项')
    ids = set()
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get('id'), str) or not option.get('id') or not isinstance(option.get('label'), str) or not option.get('label'):
            raise ValueError('选项必须包含 id 和 label')
        if option['id'] in ids:
            raise ValueError('选项 id 重复')
        ids.add(option['id'])
    if not isinstance(q.get('remaining', 1), int) or q.get('remaining', 1) < 1:
        raise ValueError('remaining 必须为正整数')
    if 'table' in q:
        table = q['table']
        if not isinstance(table, dict) or not isinstance(table.get('headers'), list) or not isinstance(table.get('rows'), list):
            raise ValueError('table 必须包含 headers 和 rows 数组')
        if not table['headers'] or not all(isinstance(x, str) for x in table['headers']):
            raise ValueError('表头必须为非空字符串数组')
        if not all(isinstance(row, list) and len(row) == len(table['headers']) and all(isinstance(x, str) for x in row) for row in table['rows']):
            raise ValueError('表格行必须与表头列数一致')
    return q


class BoundedHTTPServer(ThreadingHTTPServer):
    # 读请求头也受超时和并发限制，鉴权前不能无限占用线程。
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(5)
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Review:
    def __init__(self, directory, idle_seconds=600, now=time.time, notifier=notify_macos):
        self.path = directory / 'state.json'
        self.activity_path = directory / 'activity.json'
        self.notifying = set()
        self.stopping = False
        self.lock = threading.Condition()
        self.now = now
        self.idle_seconds = idle_seconds
        self.notifier = notifier
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'revision': 0, 'status': 'idle', 'question': None, 'answers': []}
        self.data.setdefault('asks', [])
        self.data.setdefault('version', 0)
        if self.activity_path.exists():
            activity = json.loads(self.activity_path.read_text())
            if activity.get('revision') == self.data['revision'] and self.data['status'] == 'pending':
                self.data['deadline'] = max(self.data.get('deadline', 0), activity['deadline'])
        self.data['idle_seconds'] = idle_seconds
        if self.data['status'] == 'pending':
            self.data.setdefault('deadline', self.now() + idle_seconds)

    def expire(self):
        with self.lock:
            if self.data['status'] == 'pending' and self.now() >= self.data['deadline']:
                d = copy.deepcopy(self.data)
                d.update(status='timed_out', timeout_reason='长时间无操作，尚未抉择，等待用户处理。')
                for ask in d['asks']:
                    if ask['status'] == 'pending':
                        ask['status'] = 'cancelled'
                d['version'] += 1
                save(self.path, d)
                self.data = d
                self.lock.notify_all()
                return True
            return False

    def prepare_stop(self, revision):
        with self.lock:
            self.expire()
            if self.data['status'] not in TERMINAL or revision != self.data['revision']:
                raise ValueError('仅能停止指定版本的已结束题目，待答时不能停止')
            self.stopping = True
            self.lock.notify_all()
            return {'stopping': True, 'revision': revision, 'status': self.data['status']}

    def has_event(self):
        return self.data['status'] != 'pending' or any(a['status'] == 'pending' for a in self.data['asks'])

    def snapshot(self, since=None):
        with self.lock:
            self.expire()
            d = self.data
            if since is not None and str(d['version']) == since:
                return {'unchanged': True, 'version': d['version'], 'deadline': d.get('deadline')}
            result = copy.deepcopy(d)
            if since is not None:
                result['answers'] = result['answers'][-20:]
                result['asks'] = [a for a in result['asks'] if a['revision'] == d['revision']]
            return result

    def notify(self):
        with self.lock:
            if self.stopping:
                raise ValueError('服务正在停止，请启动新服务')
            self.expire()
            d = self.data
            revision = d['revision']
            if d['status'] != 'pending':
                raise ValueError('当前题已结束，不再发送通知')
            if d.get('notification', {}).get('revision') == revision or revision in self.notifying:
                return copy.deepcopy(d)
            self.notifying.add(revision)
            question = copy.deepcopy(d['question'])
        try:
            result = self.notifier(question)
            with self.lock:
                self.expire()
                if self.data['revision'] == revision and self.data['status'] == 'pending':
                    d = copy.deepcopy(self.data)
                    d['notification'] = {'revision': revision, **result}
                    d['version'] += 1
                    save(self.path, d)
                    self.data = d
                return copy.deepcopy(self.data)
        finally:
            with self.lock:
                self.notifying.discard(revision)

    def update(self, action, body):
        if action == 'notify':
            return self.notify()
        with self.lock:
            if self.stopping:
                raise ValueError('服务正在停止，请启动新服务')
            self.expire()
            if action == 'activity':
                if self.data['status'] != 'pending' or body.get('revision') != self.data['revision']:
                    raise ValueError('当前题已结束，不能重置计时')
                deadline = self.now() + self.idle_seconds
                save(self.activity_path, {'revision': self.data['revision'], 'deadline': deadline})
                self.data['deadline'] = deadline
                return {'revision': self.data['revision'], 'deadline': deadline}
            d = copy.deepcopy(self.data)
            if action == 'publish':
                q = validate(body)
                if d['status'] == 'pending':
                    raise ValueError('当前题尚未回答，请先处理或关闭，不能覆盖')
                d.update(question=q, status='pending', revision=d['revision'] + 1,
                         deadline=self.now() + self.idle_seconds)
            elif action == 'ask':
                if d['status'] != 'pending' or body.get('revision') != d['revision']:
                    raise ValueError('当前题已结束或更新')
                option = next((o for o in d['question'].get('options', []) if o['id'] == body.get('option_id')), None)
                prompt = body.get('prompt')
                if option is None or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 6000:
                    raise ValueError('请选择有效选项并填写追问（最多 6000 字）')
                if any(a['status'] == 'pending' for a in d['asks']):
                    raise ValueError('上一条追问仍在等待回答，请稍候')
                d['asks'].append({'id': secrets.token_hex(12), 'revision': d['revision'],
                                  'question_id': d['question']['id'], 'option_id': option['id'],
                                  'option_label': option['label'], 'prompt': prompt.strip(),
                                  'status': 'pending', 'response': None, 'time': time.time()})
                d['deadline'] = self.now() + self.idle_seconds
            elif action == 'respond':
                ask = next((a for a in d['asks'] if a['id'] == body.get('ask_id')), None)
                response = body.get('response')
                if ask is None or ask['status'] != 'pending' or ask['revision'] != d['revision'] or d['status'] != 'pending':
                    raise ValueError('追问已失效或已回答')
                if not isinstance(response, str) or not response.strip() or len(response) > 60000:
                    raise ValueError('回答必须为非空文本，且不超过 60000 字')
                ask.update(status='answered', response=response.strip())
            elif action == 'answer':
                if d['status'] != 'pending' or body.get('revision') != d['revision']:
                    raise ValueError('题目已更新或已提交，请刷新')
                q = d['question']
                choices = body.get('choices', [])
                note = body.get('note', '')
                if not isinstance(choices, list) or not all(isinstance(x, str) for x in choices) or not isinstance(note, str):
                    raise ValueError('回答格式不正确')
                if len(note) > 12000:
                    raise ValueError('补充说明过长')
                valid = {x['id'] for x in q.get('options', [])}
                if len(choices) != len(set(choices)) or any(x not in valid for x in choices):
                    raise ValueError('选项无效')
                cancelled = body.get('cancelled') is True
                if not cancelled:
                    if q.get('type', 'single') == 'single' and len(choices) > 1:
                        raise ValueError('此题只能单选')
                    if q.get('type') == 'text' and choices:
                        raise ValueError('文本题不能提交选项')
                    if not choices and not note.strip():
                        raise ValueError('请选择选项或填写你的意见')
                answer = {'id': q['id'], 'revision': d['revision'], 'question': q['question'],
                          'choices': choices, 'labels': [x['label'] for x in q.get('options', []) if x['id'] in choices],
                          'note': note.strip(), 'cancelled': cancelled, 'time': time.time()}
                d['answers'].append(answer)
                d['status'] = 'paused' if cancelled else 'answered'
                for ask in d['asks']:
                    if ask['status'] == 'pending':
                        ask['status'] = 'cancelled'
            elif action == 'finish':
                if d['status'] in ('pending', 'paused', 'timed_out'):
                    raise ValueError('尚有未决问题，不能标记完成')
                d.update(status='completed', summary=str(body.get('summary', '本轮复核已完成。')))
            else:
                raise ValueError('不支持的操作')
            d['version'] += 1
            save(self.path, d)
            self.data = d
            self.lock.notify_all()
            return copy.deepcopy(d)


def serve(args):
    directory = Path(args.session).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    if not 1 <= args.idle_seconds <= 3600:
        raise ValueError('idle-seconds 必须介于 1 和 3600 秒之间')
    state = Review(directory, idle_seconds=args.idle_seconds,
                   notifier=lambda question: notify_macos(question, args.thread_id))
    ui_token, control_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, mime='application/json'):
            payload = json.dumps(data, ensure_ascii=False).encode() if mime == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self, control=False):
            token = self.headers.get('Authorization', '').removeprefix('Bearer ')
            return secrets.compare_digest(token, control_token) or (not control and secrets.compare_digest(token, ui_token))

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == '/api/state':
                if not self.authorized():
                    return self.reply(401, {'error': '无访问权限，请使用本次会话的完整链接'})
                try:
                    since = parse_qs(parsed.query).get('since', [None])[0]
                    snapshot = state.snapshot(since)
                    return self.reply(200, snapshot)
                except OSError:
                    return self.reply(500, {'error': '状态保存失败，请检查磁盘后重试'})
            assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if self.path not in assets:
                return self.reply(404, {'error': '页面不存在'})
            name, mime = assets[self.path]
            self.reply(200, (ROOT / 'web' / name).read_bytes(), mime)

        def do_POST(self):
            action = self.path.removeprefix('/api/')
            if self.path not in ('/api/publish', '/api/answer', '/api/finish', '/api/wait', '/api/ask', '/api/respond', '/api/activity', '/api/notify', '/api/stop'):
                return self.reply(404, {'error': '接口不存在'})
            if not self.authorized(control=action not in ('answer', 'ask', 'activity')):
                return self.reply(401, {'error': '无访问权限'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > 262144:
                    raise ValueError('请求过大')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求必须为对象')
                if action == 'stop':
                    result = state.prepare_stop(body.get('revision'))
                    try:
                        self.reply(200, result)
                    finally:
                        threading.Thread(target=server.shutdown, daemon=True).start()
                    return
                if action == 'wait':
                    seconds = max(0, min(float(body.get('seconds', 45)), 50))
                    with state.lock:
                        state.expire()
                        state.lock.wait_for(state.has_event, seconds)
                        snapshot = state.snapshot()
                    return self.reply(200, snapshot)
                self.reply(200, state.update(action, body))
            except OSError:
                self.reply(500, {'error': '状态保存失败，请检查磁盘后重试'})
            except (ValueError, TypeError, KeyError) as error:
                self.reply(400, {'error': str(error)})

    server = BoundedHTTPServer(('127.0.0.1', args.port), Handler)
    base = f'http://127.0.0.1:{server.server_port}'
    info = {'base': base, 'url': f'{base}/#{ui_token}', 'control_token': control_token, 'pid': os.getpid(), 'thread_id': args.thread_id}
    save(directory / 'connection.json', info)
    print(json.dumps({'url': info['url'], 'session': str(directory)}, ensure_ascii=False), flush=True)
    stop_watch = threading.Event()

    def watch_idle():
        terminal_since = None
        terminal_revision = None
        while not stop_watch.wait(0.5):
            try:
                with state.lock:
                    state.expire()
                    d = state.data
                    if d['status'] in TERMINAL:
                        if terminal_since is None or terminal_revision != d['revision']:
                            terminal_since, terminal_revision = time.monotonic(), d['revision']
                        if time.monotonic() - terminal_since >= args.cleanup_seconds:
                            state.prepare_stop(d['revision'])
                            should_stop = True
                        else:
                            should_stop = False
                    else:
                        terminal_since = terminal_revision = None
                        should_stop = False
                if should_stop:
                    server.shutdown()
                    return
            except OSError:
                pass  # 保留未决状态，下次重试落盘，不终止超时监视器。

    threading.Thread(target=watch_idle, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop_watch.set()
        server.server_close()
        print(json.dumps({'stopped': True, 'port': server.server_port}), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['serve', 'publish', 'state', 'wait', 'finish', 'respond', 'bind-tab', 'stop'])
    p.add_argument('--session', required=True)
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID'),
                   help='通知点击目标，默认当前 CODEX_THREAD_ID；恢复时须属于当前任务')
    p.add_argument('--idle-seconds', type=int, default=600, help='无操作超时秒数，默认 600；短值仅用于隔离测试')
    p.add_argument('--cleanup-seconds', type=float, default=60,
                   help='已结束题目的服务回收兜底秒数，默认60；短值仅用于测试')
    p.add_argument('--revision', type=int, help='stop 必须指定当前题目版本')
    p.add_argument('--file')
    p.add_argument('--seconds', type=float, default=45)
    p.add_argument('--summary', default='本轮复核已完成。')
    p.add_argument('--tab-id')
    p.add_argument('--browser-id')
    args = p.parse_args()
    if args.action == 'serve':
        if not 0.1 <= args.cleanup_seconds <= 600:
            p.error('cleanup-seconds 必须介于0.1和600秒')
        return serve(args)
    info = json.loads((Path(args.session) / 'connection.json').read_text())
    request_action = args.action
    body = None
    if args.action == 'stop':
        if args.revision is None:
            p.error('stop 需要 --revision，防止误停新的题目')
        body = {'revision': args.revision}
    if args.action == 'bind-tab':
        if not args.tab_id or not args.browser_id:
            p.error('bind-tab 需要 --tab-id 和 --browser-id')
        info['tab'] = {'id': args.tab_id, 'browser_id': args.browser_id, 'url': info['url']}
        save(Path(args.session) / 'connection.json', info)
        request_action = 'notify'
        body = {}
    if args.action in ('publish', 'respond'):
        if not args.file:
            p.error('publish/respond 需要 --file')
        body = json.loads(Path(args.file).read_text())
    elif args.action == 'wait':
        body = {'seconds': args.seconds}
    elif args.action == 'finish':
        body = {'summary': args.summary}
    req = Request(info['base'] + '/api/' + request_action,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': 'Bearer ' + info['control_token'], 'Content-Type': 'application/json'})
    with urlopen(req, timeout=60) as response:
        result = json.loads(response.read().decode())
        if args.action == 'bind-tab':
            result = {'tab': info['tab'], 'notification': result.get('notification')}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()

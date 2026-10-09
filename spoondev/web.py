"""Local, read-only search website for the observation database."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
import threading
import time
from http.cookies import SimpleCookie
from . import accounts, __version__
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from . import webdata
from . import profiledb
from . import fans


class LimitedServer(ThreadingHTTPServer):
    """Bound active requests so slow clients cannot create unlimited threads."""
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(16)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request); return
        try: super().process_request(request,address)
        except BaseException:
            self.slots.release(); raise
    def process_request_thread(self,request,address):
        try: super().process_request_thread(request,address)
        finally: self.slots.release()


def make_server(database, host='127.0.0.1', port=8080, *, auth=False, auth_database='data/accounts.sqlite3', public_url=None):
    if public_url:
        parsed=urlsplit(public_url)
        if not auth or host not in ('127.0.0.1','localhost') or parsed.scheme!='https' or not parsed.netloc or parsed.path not in ('','/') or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('External publication requires auth, a loopback host, and an HTTPS origin')
        public_url='https://'+parsed.netloc
    if auth: accounts.initialize(auth_database)
    login_lock=threading.Lock()
    attempts=[]
    database = str(Path(database).resolve())
    if not Path(database).is_file():
        raise FileNotFoundError('Database not found; run init-db or collect-spoon first')
    profiledb.initialize(database)
    fans.initialize(database)
    from . import worker
    worker.initialize(database)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type='application/json; charset=utf-8', headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for key,value in (headers or {}).items(): self.send_header(key,value)
            self.end_headers()
            self.wfile.write(body)

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def handle(self):
            try: super().handle()
            except (ConnectionResetError,BrokenPipeError,TimeoutError): pass

        def actor(self):
            self.token=''
            try:
                cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
                self.token=cookie['spoondev_session'].value if 'spoondev_session' in cookie else ''
            except Exception: pass
            self.user=accounts.session(auth_database,self.token) if auth else None
            self.private_db=accounts.private(auth_database,self.user['id']) if self.user else None
            return self.user

        def own_profile(self):
            return accounts.account_settings(self.private_db)['spoon_profile'] if auth else None

        def fan_scope(self, owner_id):
            if auth and self.user['role'] != 'admin':
                profile = self.own_profile()
                if not profile or profile['id'] != owner_id:
                    raise PermissionError('ファン一覧は登録したご自身の配信者に限り操作できます。')

        def resolve_profile(self, raw):
            import re
            if not isinstance(raw, str):
                raise ValueError('プロフィールURLまたは数値IDを指定してください。')
            raw = raw.strip()
            if raw.startswith('https://'):
                parts = urlsplit(raw)
                match = re.fullmatch(r'/jp/channel/([0-9]{1,20})(?:/tab/[a-z]+)?/?', parts.path)
                if (parts.hostname not in ('www.spooncast.net', 'spooncast.net') or
                        parts.username or parts.port or parts.fragment or not match):
                    raise ValueError('SpoonのプロフィールURLを指定してください。')
                raw = match.group(1)
            uid = fans.numeric_id(raw)
            profile = webdata.user_details(database, uid)
            if profile is None:
                from .directory import resolve_user
                profile = resolve_user(uid)
            if not profile or str(profile.get('id')) != uid:
                raise ValueError('プロフィールの数値IDを確認できませんでした。')
            return {key: profile.get(key) for key in ('id', 'name', 'tag')}

        def enrich(self, users):
            from .insights import listener_insights
            profile = self.own_profile()
            ids = list(dict.fromkeys(str(u.get('id') or u.get('user_id')) for u in users))
            metrics = {}
            for start in range(0, len(ids), 100):
                metrics.update(listener_insights(database, ids[start:start+100],
                    broadcaster_id=profile['id'] if profile else None))
            for user in users:
                uid = str(user.get('id') or user.get('user_id'))
                user['insights'] = metrics.get(uid)

        def gate(self):
            if not auth: return True
            if public_url and self.headers.get('Host')!=urlsplit(public_url).netloc:
                self.respond(403,{'error':'Invalid host'}); return False
            if self.actor():
                path=urlsplit(self.path).path
                if self.user['must_change'] and path not in ('/password','/api/password','/api/logout'):
                    self.respond(303 if not path.startswith('/api/') else 403,{'error':'パスワードを変更してください。','password_change_required':True},headers={'Location':'/password'})
                    return False
                if path.startswith('/api/admin/') and self.user['role']!='admin':
                    self.respond(403,{'error':'管理者のみ操作できます。'});return False
                return True
            self.respond(303 if urlsplit(self.path).path=='/' else 401,{'error':'ログインしてください。'},headers={'Location':'/login'})
            return False

        def do_GET(self):
            try:self._get()
            except PermissionError as exc:self.respond(403,{'error':str(exc)})
            except (sqlite3.Error,OSError):self.respond(503,{'error':'データを読み込めませんでした。時間をおいて再試行してください。'})
            except ValueError:self.respond(400,{'error':'リクエストを確認してください。'})

        def _get(self):
            path=urlsplit(self.path).path
            if auth and path=='/login':
                self.respond(200,Path(__file__).with_name('static').joinpath('login.html').read_bytes(),'text/html; charset=utf-8'); return
            if not self.gate(): return
            if auth and path=='/password':
                self.respond(200,Path(__file__).with_name('static').joinpath('password.html').read_text().replace('CSRF_TOKEN',self.user['csrf']).encode(),'text/html; charset=utf-8');return
            if auth and path=='/api/admin/collection-status':
                if self.user['username']!='kitomoya':
                    self.respond(403,{'error':'システム情報はkitomoya専用です。'});return
                from .collection_status import read
                with webdata._read(database) as conn:result=read(conn)
                self.respond(200,result);return
            if auth and path=='/api/account/settings':
                result=accounts.account_settings(self.private_db)
                result['can_change']=self.user['role']=='admin' or not result['spoon_profile']
                self.respond(200,result);return
            if auth and path=='/api/account/profile-preview':
                query=parse_qs(urlsplit(self.path).query,max_num_fields=3)
                from .directory import DirectoryError
                try: result=self.resolve_profile(query.get('spoon_id',[''])[0])
                except DirectoryError:
                    self.respond(502,{'error':'プロフィールを確認できませんでした。時間をおいて再試行してください。'});return
                self.respond(200,result);return
            if auth and path=='/api/dashboard':
                from .dashboard import summary
                self.respond(200,summary(database,self.private_db));return
            if auth and path=='/api/admin/users':
                result=accounts.list_users(auth_database)
                for user in result:
                    setting=accounts.account_settings(accounts.private(auth_database,user['id']))
                    user['spoon_profile']=setting['spoon_profile']
                    user['binding_locked']=setting['binding_locked']
                self.respond(200,result);return
            if auth and path=='/api/admin/labels':
                rows=accounts.list_labels(auth_database)
                self.respond(200,{'labels':[row['name'] for row in rows],'items':rows});return
            if auth and path=='/api/favorites':
                self.respond(200,accounts.favorites(self.private_db)); return
            if len(self.path) > 4096:
                self.respond(414, {'error':'リクエストが長すぎます。'})
                return
            route = urlsplit(self.path)
            try:
                query = parse_qs(route.query, max_num_fields=10)
                if route.path == '/':
                    html=Path(__file__).with_name('static').joinpath('index.html').read_text()
                    environment={'name':'公開' if public_url else '開発' if port==8081 else 'ローカル', 'version':__version__}
                    html=html.replace('<script>','<script>window.spoondevEnvironment='+json.dumps(environment,ensure_ascii=False)+';</script><script>',1)
                    if auth:
                        self.user['can_view_stats']=self.user['username']=='kitomoya'
                        if not self.user['can_view_stats']:
                            html=html.replace('id="stats"','id="stats" hidden')
                        bootstrap=json.dumps(self.user).replace('<','\\u003c')
                        html=html.replace('<script>','<script>window.spoondevAccount='+bootstrap+';</script><script>',1)
                    self.respond(200,html.encode(),'text/html; charset=utf-8')
                elif route.path == '/api/stats':
                    if auth and self.user['username']!='kitomoya':
                        self.respond(403,{'error':'集計情報は管理者のみ閲覧できます。'})
                    else:
                        self.respond(200, webdata.stats(database))
                elif route.path == '/api/on-air':
                    from .onair import current_public_lives, LiveStatusError
                    try:
                        self.respond(200, current_public_lives())
                    except LiveStatusError as exc:
                        self.respond(503, {'error':str(exc)})
                elif route.path == '/api/fan-owners':
                    result=webdata.fan_owners(database, self.private_db if auth else None)
                    if auth and self.user['role']!='admin':
                        own=self.own_profile()
                        result=[row for row in result if own and row['id']==own['id']]
                    self.respond(200,result)
                elif route.path == '/api/fans':
                    owner_id = fans.numeric_id(query.get('owner_id', [''])[0])
                    self.fan_scope(owner_id)
                    offset = int(query.get('offset', ['0'])[0])
                    if not 0 <= offset <= 1000000:
                        raise ValueError('Invalid offset')
                    result=webdata.fan_destinations(database, owner_id, offset=offset,
                        private_database=self.private_db if auth else None, sort=query.get('sort',['recent'])[0],
                        q=query.get('q',[''])[0], activity=query.get('activity',['all'])[0])
                    if auth and self.user['username']!='kitomoya':result.pop('indexed_djs',None)
                    self.enrich(result['fans'])
                    self.respond(200,result)
                elif route.path == '/fan-export.js':
                    self.respond(200, Path(__file__).with_name('static').joinpath('fan-export.js').read_bytes(), 'text/javascript; charset=utf-8')
                elif route.path == '/api/favorites/activity':
                    ids = query.get('ids', [''])[0]
                    result=webdata.favorite_activity(database, ids.split(',') if ids else [])
                    self.enrich(result['users'])
                    self.respond(200,result)
                elif route.path == '/api/users':
                    term = query.get('q',[''])[0].strip()
                    offset = int(query.get('offset',['0'])[0])
                    if len(term) > 200 or not 0 <= offset <= 1000000:
                        raise ValueError('Invalid search parameter')
                    if query.get('scope',['local'])[0] == 'spoon' and term:
                        from .directory import search_users, DirectoryError
                        try:
                            result = search_users(term,offset=offset,limit=50)
                            profiledb.cache_users(database,[{key:u.get(key) for key in ('id','name','tag')} for u in result['users']])
                        except DirectoryError as exc:
                            result = webdata.search_users(database,term,limit=50,offset=offset)
                            result['warning'] = str(exc)+' 保存済みデータの検索結果を表示しています。'
                            result['source'] = 'local_fallback'
                        self.enrich(result['users'])
                        self.respond(200,result)
                    else:
                        result=webdata.search_users(database, term, limit=50, offset=offset)
                        self.enrich(result['users'])
                        self.respond(200,result)
                elif route.path.startswith('/api/users/'):
                    user_id = unquote(route.path[len('/api/users/'):])
                    if not user_id or len(user_id) > 200 or '/' in user_id:
                        raise ValueError('Invalid account ID')
                    user = webdata.user_details(database, user_id)
                    if user is None and user_id.isascii() and user_id.isdigit():
                        from .directory import resolve_user, DirectoryError
                        try:
                            remote = resolve_user(user_id)
                            profiledb.cache_users(database,[{key:remote.get(key) for key in ('id','name','tag')}])
                            user = webdata.user_details(database,user_id)
                        except DirectoryError:
                            self.respond(502,{'error':'Spoonのプロフィールを取得できませんでした。ユーザーが存在しないとは限りません。'})
                            return
                    if user is not None:
                        self.enrich([user])
                        self.respond(200,user)
                    else:self.respond(404,{'error':'ユーザーが見つかりません。'})
                elif route.path == '/api/history':
                    broadcaster = query.get('broadcaster_id',[''])[0]
                    listener = query.get('listener_id',[''])[0]
                    if not broadcaster or not listener or max(len(broadcaster),len(listener)) > 200:
                        raise ValueError('Both IDs required')
                    self.respond(200, webdata.history(database,broadcaster,listener))
                else:
                    self.respond(404, {'error':'ページが見つかりません。'})
            except PermissionError as exc:
                self.respond(403,{'error':str(exc)})
            except ValueError:
                self.respond(400, {'error':'検索条件を確認してください。'})
            except (sqlite3.Error, OSError):
                self.respond(503, {'error':'データを読み込めませんでした。時間をおいて再試行してください。'})

        def do_POST(self):
            path=urlsplit(self.path).path
            if path not in ('/api/fans/import','/api/fans/clear','/api/login','/api/logout','/api/favorites','/api/password','/api/account/settings','/api/admin/labels','/api/admin/users/spoon-profile','/api/admin/users/reset-password','/api/admin/users/create','/api/admin/users/label','/api/admin/users/role','/api/admin/users/delete') or (not auth and path not in ('/api/fans/import','/api/fans/clear')):
                self.respond(405,{'error':'この操作は利用できません。'}); return
            if path!='/api/login' and not self.gate(): return
            origin=self.headers.get('Origin')
            expected=public_url or 'http://'+self.headers.get('Host','')
            if (auth and origin!=expected) or (origin and origin!=expected) or self.headers.get('Sec-Fetch-Site')=='cross-site':
                self.respond(403,{'error':'このサイトから操作してください。'}); return
            if auth and path!='/api/login' and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),self.user['csrf']):
                self.respond(403,{'error':'ページを再読み込みしてください。'}); return
            if path=='/api/login':
                with login_lock:
                    now=time.monotonic(); attempts[:]=[t for t in attempts if now-t<60]
                    if len(attempts)>=10:
                        self.respond(429,{'error':'少し待ってからログインしてください。'}); return
                    attempts.append(now)
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                self.respond(415, {'error':'JSON形式の一覧を指定してください。'})
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= (8192 if path=='/api/login' else 8*1024*1024):
                    self.respond(413, {'error':'一覧は8MB以内に分けて取り込んでください。'})
                    return
                self.connection.settimeout(15)
                body=self.rfile.read(length)
                if len(body)!=length:
                    raise ValueError('一覧の受信が完了していません。')
                payload=json.loads(body)
                if not isinstance(payload,dict): raise ValueError('JSON object required')
                if path=='/api/login':
                    token=accounts.login(auth_database,payload.get('username',''),payload.get('password',''))
                    if not token:
                        self.respond(401,{'error':'ユーザー名かパスワードを確認してください。'}); return
                    self.respond(200,{'ok':True},headers={'Set-Cookie':'spoondev_session='+token+'; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800'+('; Secure' if public_url else '')}); return
                if path=='/api/logout':
                    accounts.logout(auth_database,self.token)
                    self.respond(200,{'ok':True},headers={'Set-Cookie':'spoondev_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'+('; Secure' if public_url else '')}); return
                if path=='/api/password':
                    result=accounts.change_password(auth_database,self.user['id'],payload.get('current_password'),payload.get('new_password'))
                    self.respond(200,result,headers={'Set-Cookie':'spoondev_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'+('; Secure' if public_url else '')});return
                if path=='/api/account/settings':
                    if 'spoon_id' not in payload: raise ValueError('spoon_id を明示してください。解除する場合は null を指定してください。')
                    raw=payload.get('spoon_id')
                    if self.user['role']!='admin' and self.own_profile():
                        raise PermissionError('配信者登録は確定済みです。変更は管理者に依頼してください。')
                    if payload.get('confirmed') is not True:
                        raise ValueError('配信者の登録内容を確認してください。')
                    from .directory import DirectoryError
                    try: profile=self.resolve_profile(raw) if raw is not None else None
                    except DirectoryError:
                        self.respond(502,{'error':'プロフィールを確認できませんでした。時間をおいて再試行してください。'});return
                    result=accounts.account_settings(self.private_db,{'spoon_profile':profile},
                        allow_change=self.user['role']=='admin',confirmed=True,actor=self.user)
                    if profile:
                        profiledb.cache_users(database,[profile])
                        for kind in ('monthly','gifts'): worker.enqueue(database,kind,profile['id'],priority=100)
                elif path=='/api/admin/users/spoon-profile':
                    target=next((u for u in accounts.list_users(auth_database) if u['id']==payload.get('id')),None)
                    if target is None:raise ValueError('ユーザーが存在しません。')
                    if 'spoon_id' not in payload or payload.get('confirmed') is not True:
                        raise ValueError('配信者の変更内容を確認してください。')
                    from .directory import DirectoryError
                    try: profile=self.resolve_profile(payload['spoon_id']) if payload['spoon_id'] is not None else None
                    except DirectoryError:
                        self.respond(502,{'error':'プロフィールを確認できませんでした。時間をおいて再試行してください。'});return
                    result=accounts.account_settings(accounts.private(auth_database,target['id']),
                        {'spoon_profile':profile},allow_change=True,confirmed=True,actor=self.user)
                    if profile:
                        profiledb.cache_users(database,[profile])
                        for kind in ('monthly','gifts'):worker.enqueue(database,kind,profile['id'],priority=100)
                elif path=='/api/admin/labels':
                    result=accounts.add_label(auth_database,payload.get('label'))
                elif path=='/api/admin/users/reset-password':
                    if payload.get('id')==self.user['id']: raise ValueError('自分のパスワードはアカウント設定から変更してください。')
                    result=accounts.reset_password(auth_database,payload.get('id'),payload.get('password'))
                elif path=='/api/admin/users/create':
                    uid=accounts.create(auth_database,payload.get('username'),payload.get('password'),label=payload.get('label','利用者'))
                    result={'id':uid}
                elif path=='/api/admin/users/role':
                    if isinstance(payload.get('username'),str) and payload['username'].strip().lower()==self.user['username']: raise ValueError('自分の権限はこの画面から変更できません。')
                    result=accounts.set_role(auth_database,payload.get('username'),payload.get('role'))
                elif path=='/api/admin/users/label':
                    result=accounts.set_label(auth_database,payload.get('id'),payload.get('label'))
                elif path=='/api/admin/users/delete':
                    if payload.get('confirm') is not True: raise ValueError('削除を確認してください。')
                    result=accounts.delete_user(auth_database,payload.get('id'))
                elif path=='/api/favorites':
                    result=accounts.favorites(self.private_db,payload)
                    if result is None:
                        self.respond(409,{'error':'別の画面で更新されました。再読み込みしてください。'}); return
                elif path=='/api/fans/clear':
                    self.fan_scope(fans.numeric_id(payload.get('owner_id')))
                    result=fans.clear_followers(self.private_db if auth else database,payload)
                else:
                    self.fan_scope(fans._user(payload.get('owner'),True)['id'])
                    result=fans.import_followers(self.private_db if auth else database,payload)
                self.respond(200,result)
            except accounts.BindingConflict as exc:
                self.respond(409,{'error':str(exc)})
            except PermissionError as exc:
                self.respond(403,{'error':str(exc)})
            except (ValueError,UnicodeError) as exc:
                self.respond(400, {'error':str(exc)})
            except (sqlite3.Error,OSError):
                self.respond(503, {'error':'一覧を保存できませんでした。再試行してください。'})

        def log_message(self, *_):
            pass

    server = LimitedServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(database, host='127.0.0.1', port=8080, **options):
    server = make_server(database,host,port,**options)
    print(f'Search website listening on {host}, port {server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

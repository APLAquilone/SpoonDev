"""Local, read-only search website for the observation database."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from . import webdata
from . import profiledb
from . import fans


def make_server(database, host='127.0.0.1', port=8080):
    database = str(Path(database).resolve())
    if not Path(database).is_file():
        raise FileNotFoundError('Database not found; run init-db or collect-spoon first')
    profiledb.initialize(database)
    fans.initialize(database)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if len(self.path) > 4096:
                self.respond(414, {'error':'リクエストが長すぎます。'})
                return
            route = urlsplit(self.path)
            try:
                query = parse_qs(route.query, max_num_fields=10)
                if route.path == '/':
                    self.respond(200, Path(__file__).with_name('static').joinpath('index.html').read_bytes(), 'text/html; charset=utf-8')
                elif route.path == '/api/stats':
                    self.respond(200, webdata.stats(database))
                elif route.path == '/api/fan-owners':
                    self.respond(200, webdata.fan_owners(database))
                elif route.path == '/api/fans':
                    owner_id = fans.numeric_id(query.get('owner_id', [''])[0])
                    offset = int(query.get('offset', ['0'])[0])
                    if not 0 <= offset <= 1000000:
                        raise ValueError('Invalid offset')
                    self.respond(200, webdata.fan_destinations(database, owner_id, offset=offset))
                elif route.path == '/fan-export.js':
                    self.respond(200, Path(__file__).with_name('static').joinpath('fan-export.js').read_bytes(), 'text/javascript; charset=utf-8')
                elif route.path == '/api/favorites/activity':
                    ids = query.get('ids', [''])[0]
                    self.respond(200, webdata.favorite_activity(database, ids.split(',') if ids else []))
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
                        self.respond(200,result)
                    else:
                        self.respond(200, webdata.search_users(database, term, limit=50, offset=offset))
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
                    self.respond(200,user) if user is not None else self.respond(404,{'error':'ユーザーが見つかりません。'})
                elif route.path == '/api/history':
                    broadcaster = query.get('broadcaster_id',[''])[0]
                    listener = query.get('listener_id',[''])[0]
                    if not broadcaster or not listener or max(len(broadcaster),len(listener)) > 200:
                        raise ValueError('Both IDs required')
                    self.respond(200, webdata.history(database,broadcaster,listener))
                else:
                    self.respond(404, {'error':'ページが見つかりません。'})
            except ValueError:
                self.respond(400, {'error':'検索条件を確認してください。'})
            except (sqlite3.Error, OSError):
                self.respond(503, {'error':'データを読み込めませんでした。時間をおいて再試行してください。'})

        def do_POST(self):
            if urlsplit(self.path).path != '/api/fans/import':
                self.respond(405, {'error':'この操作は利用できません。'})
                return
            # JSON plus browser-origin checks protect LAN writes from cross-site requests.
            origin=self.headers.get('Origin')
            if (origin and origin != 'http://'+self.headers.get('Host','')) or self.headers.get('Sec-Fetch-Site')=='cross-site':
                self.respond(403, {'error':'このサイトから操作してください。'})
                return
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                self.respond(415, {'error':'JSON形式の一覧を指定してください。'})
                return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 8*1024*1024:
                    self.respond(413, {'error':'一覧は8MB以内に分けて取り込んでください。'})
                    return
                self.connection.settimeout(15)
                body=self.rfile.read(length)
                if len(body)!=length:
                    raise ValueError('一覧の受信が完了していません。')
                result=fans.import_followers(database,json.loads(body))
                self.respond(200,result)
            except (ValueError,UnicodeError) as exc:
                self.respond(400, {'error':str(exc)})
            except (sqlite3.Error,OSError):
                self.respond(503, {'error':'一覧を保存できませんでした。再試行してください。'})

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(database, host='127.0.0.1', port=8080):
    server = make_server(database,host,port)
    print(f'Search website listening on {host}, port {server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

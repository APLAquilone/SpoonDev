"""Local, read-only search website for the observation database."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlsplit

from . import webdata


def make_server(database, host='127.0.0.1', port=8080):
    database = str(Path(database).resolve())
    if not Path(database).is_file():
        raise FileNotFoundError('Database not found; run init-db or collect-spoon first')

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
                elif route.path == '/api/users':
                    term = query.get('q',[''])[0].strip()
                    offset = int(query.get('offset',['0'])[0])
                    if len(term) > 200 or not 0 <= offset <= 1000000:
                        raise ValueError('Invalid search parameter')
                    self.respond(200, webdata.search_users(database, term, limit=50, offset=offset))
                elif route.path.startswith('/api/users/'):
                    user_id = unquote(route.path[len('/api/users/'):])
                    if not user_id or len(user_id) > 200 or '/' in user_id:
                        raise ValueError('Invalid account ID')
                    user = webdata.user_details(database, user_id)
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
            self.respond(405, {'error':'このサイトは読み取り専用です。'})

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

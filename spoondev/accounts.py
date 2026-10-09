"""Local operator-managed accounts and physically separated private data."""
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import threading

_HASH_SLOTS=threading.BoundedSemaphore(2)
from pathlib import Path
from . import fans, profiledb


def initialize(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,account_id TEXT NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL);''')
    path.chmod(0o600)


def password_hash(password,salt):
    if not isinstance(password,str) or not 12<=len(password)<=1024:
        raise ValueError('パスワードは12〜1024文字で指定してください。')
    with _HASH_SLOTS:
        return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=32768,r=8,p=1,maxmem=64*1024*1024).hex()


def create(path,username,password,reset=False):
    username=username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_-]{3,64}',username):
        raise ValueError('ユーザー名は英小文字・数字・_- の3〜64文字です。')
    salt=secrets.token_hex(16); hashed=password_hash(password,salt); initialize(path)
    with sqlite3.connect(path) as c:
        old=c.execute('SELECT id FROM accounts WHERE username=?',(username,)).fetchone()
        if reset:
            if not old: raise ValueError('ユーザーが存在しません。')
            uid=old[0]; c.execute('UPDATE accounts SET salt=?,password=? WHERE id=?',(salt,hashed,uid))
            c.execute('DELETE FROM sessions WHERE account_id=?',(uid,))
        else:
            if old: raise ValueError('ユーザー名は登録済みです。')
            uid=secrets.token_hex(16)
            c.execute('INSERT INTO accounts VALUES(?,?,?,?)',(uid,username,salt,hashed))
    private(path,uid)
    return uid


def private(path,uid):
    if not re.fullmatch('[0-9a-f]{32}',uid): raise ValueError('Invalid account')
    folder=Path(path).parent/'account-data'/uid; folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    folder.chmod(0o700); db=folder/'private.sqlite3'
    profiledb.initialize(db); fans.initialize(db)
    with sqlite3.connect(db) as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS favorites(id TEXT PRIMARY KEY,name TEXT,tag TEXT);
        CREATE TABLE IF NOT EXISTS favorite_revision(revision INTEGER NOT NULL);
        INSERT INTO favorite_revision SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM favorite_revision);''')
    db.chmod(0o600)
    return str(db)


def login(path,username,password):
    with sqlite3.connect(path) as c:
        row=c.execute('SELECT id,username,salt,password FROM accounts WHERE username=?',(str(username).lower(),)).fetchone()
        salt=row[2] if row else '00'*16
        try: hashed=password_hash(password,salt)
        except ValueError: hashed=password_hash('invalid-password',salt)
        if not row or not hmac.compare_digest(hashed,row[3]): return None
        token=secrets.token_hex(32); csrf=secrets.token_hex(32)
        c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row[0],csrf,time.time()+7*86400))
        return token


def session(path,token):
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        row=c.execute('SELECT a.id,a.username,s.csrf FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE token=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return dict(row) if row else None


def logout(path,token):
    with sqlite3.connect(path) as c: c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))


def favorites(database,payload=None):
    with sqlite3.connect(database) as c:
        if payload is not None:
            users=payload.get('users'); revision=payload.get('revision')
            if not isinstance(users,list) or len(users)>2000 or type(revision) is not int: raise ValueError('お気に入りの形式を確認してください。')
            users=[fans._user(u) for u in users]
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT revision FROM favorite_revision').fetchone()[0]!=revision: return None
            c.execute('DELETE FROM favorites')
            c.executemany('INSERT INTO favorites VALUES(?,?,?)',[(u['id'],u['name'],u['tag']) for u in users])
            c.execute('UPDATE favorite_revision SET revision=revision+1')
        c.row_factory=sqlite3.Row
        return {'users':[dict(r) for r in c.execute('SELECT * FROM favorites ORDER BY rowid')], 'revision':c.execute('SELECT revision FROM favorite_revision').fetchone()[0]}


def claim(path,uid,source):
    target=private(path,uid)
    fans.initialize(source)
    with sqlite3.connect(source) as c:
        owners=c.execute('SELECT id,name,tag,complete FROM fan_owners').fetchall()
        for oid,name,tag,complete in owners:
            followers=[{'id':i,'name':n,'tag':t} for i,n,t in c.execute('SELECT user_id,name,tag FROM registered_fans WHERE owner_id=?',(oid,))]
            fans.import_followers(target,{'owner':{'id':oid,'name':name,'tag':tag},'followers':followers,'complete':bool(complete)})

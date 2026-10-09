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
        columns={r[1] for r in c.execute('PRAGMA table_info(accounts)')}
        # Before v0.2, the initial-password migration set every existing account
        # to must_change=1. It did not record whether this was a real reset.
        # Exempt those legacy flags once; future create/reset operations carry
        # their reason and retain the requirement across server restarts.
        legacy_password_policy = 'must_change_reason' not in columns
        for name,definition in [('label',"TEXT NOT NULL DEFAULT '保守'"),('must_change','INTEGER NOT NULL DEFAULT 0'),('role',"TEXT NOT NULL DEFAULT 'user'"),
                                ('must_change_reason',"TEXT NOT NULL DEFAULT ''"),('created_at','TEXT'),('password_updated_at','TEXT'),('last_login_at','TEXT')]:
            if name not in columns: c.execute(f'ALTER TABLE accounts ADD COLUMN {name} {definition}')
        if legacy_password_policy:
            c.execute("UPDATE accounts SET must_change=0,must_change_reason=''")
        c.execute("UPDATE accounts SET role='admin' WHERE username='kitomoya'")
    path.chmod(0o600)


def password_hash(password,salt):
    if not isinstance(password,str) or not 12<=len(password)<=1024:
        raise ValueError('パスワードは12〜1024文字で指定してください。')
    with _HASH_SLOTS:
        return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=32768,r=8,p=1,maxmem=64*1024*1024).hex()


def create(path,username,password,reset=False, *, label="保守", must_change=True):
    label=validate_label(label)
    if not isinstance(username,str): raise ValueError("ユーザーIDを指定してください。")
    username=username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_-]{3,64}',username):
        raise ValueError('ユーザー名は英小文字・数字・_- の3〜64文字です。')
    salt=secrets.token_hex(16); hashed=password_hash(password,salt); initialize(path)
    with sqlite3.connect(path) as c:
        old=c.execute('SELECT id FROM accounts WHERE username=?',(username,)).fetchone()
        if reset:
            if not old: raise ValueError('ユーザーが存在しません。')
            uid=old[0]; c.execute('UPDATE accounts SET salt=?,password=?,must_change=?,must_change_reason=?,password_updated_at=? WHERE id=?',
                                 (salt,hashed,int(must_change),'reset' if must_change else '',profiledb._timestamp(None),uid))
            c.execute('DELETE FROM sessions WHERE account_id=?',(uid,))
        else:
            if old: raise ValueError('ユーザー名は登録済みです。')
            uid=secrets.token_hex(16)
            stamp=profiledb._timestamp(None)
            c.execute('INSERT INTO accounts(id,username,salt,password,label,must_change,role,must_change_reason,created_at,password_updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (uid,username,salt,hashed,label,int(must_change),'admin' if username=='kitomoya' else 'user','initial' if must_change else '',stamp,stamp))
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
        INSERT INTO favorite_revision SELECT 0 WHERE NOT EXISTS(SELECT 1 FROM favorite_revision);
        CREATE TABLE IF NOT EXISTS account_settings(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          spoon_id TEXT,spoon_name TEXT,spoon_tag TEXT,updated_at TEXT);
        INSERT OR IGNORE INTO account_settings(singleton) VALUES(1);''')
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
        c.execute('UPDATE accounts SET last_login_at=? WHERE id=?',(profiledb._timestamp(None),row[0]))
        return token


def session(path,token):
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        row=c.execute('SELECT a.id,a.username,a.label,a.role,a.must_change,s.csrf FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE token=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return dict(row) if row else None


def logout(path,token):
    with sqlite3.connect(path) as c: c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(token.encode()).hexdigest(),))


def favorites(database,payload=None):
    with sqlite3.connect(database) as c:
        if payload is None:
            # A phone/PC save can commit between the two SELECTs below. Keep
            # users and revision in one read snapshot so the optimistic guard
            # cannot accidentally accept an outdated list with a new revision.
            c.execute('BEGIN')
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


def account_settings(database,payload=None):
    """Read/save the logged-in account's Spoon binding in its private database.

    The HTTP caller chooses this database from the authenticated session, never
    from a client account ID. Clearing the binding preserves fan registrations.
    """
    with sqlite3.connect(database) as c:
        if payload is not None:
            if not isinstance(payload,dict) or 'spoon_profile' not in payload:
                raise ValueError('ご自身のSpoonプロフィールを指定してください。')
            raw=payload['spoon_profile']
            profile=None if raw is None else fans._user(raw)
            c.execute('UPDATE account_settings SET spoon_id=?,spoon_name=?,spoon_tag=?,updated_at=? WHERE singleton=1',
                      (profile['id'] if profile else None,profile['name'] if profile else None,
                       profile['tag'] if profile else None,profiledb._timestamp(None)))
        row=c.execute('SELECT spoon_id,spoon_name,spoon_tag,updated_at FROM account_settings WHERE singleton=1').fetchone()
        profile={'id':row[0],'name':row[1],'tag':row[2]} if row and row[0] else None
        return {'spoon_profile':profile,'updated_at':row[3] if row else None}


def claim(path,uid,source):
    with sqlite3.connect(path) as c:
        row=c.execute('SELECT username FROM accounts WHERE id=?',(uid,)).fetchone()
    if not row or row[0]!='kitomoya':
        raise ValueError('既存のローカル登録の引き継ぎは kitomoya のみ利用できます。')
    target=private(path,uid)
    fans.initialize(source)
    with sqlite3.connect(source) as c:
        owners=c.execute('SELECT id,name,tag,complete FROM fan_owners').fetchall()
        for oid,name,tag,complete in owners:
            followers=[{'id':i,'name':n,'tag':t} for i,n,t in c.execute('SELECT user_id,name,tag FROM registered_fans WHERE owner_id=?',(oid,))]
            fans.import_followers(target,{'owner':{'id':oid,'name':name,'tag':tag},'followers':followers,'complete':bool(complete)})


def validate_label(label):
    if not isinstance(label,str) or not 1<=len(label.strip())<=64 or any(ord(ch)<32 for ch in label):
        raise ValueError('ラベルは1〜64文字で指定してください。')
    return label.strip()


def list_users(path):
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute('SELECT id,username,label,role,must_change,must_change_reason,created_at,password_updated_at,last_login_at FROM accounts ORDER BY username')]


def set_label(path,uid,label):
    label=validate_label(label)
    with sqlite3.connect(path) as c:
        if c.execute('UPDATE accounts SET label=? WHERE id=?',(label,uid)).rowcount!=1: raise ValueError('ユーザーが存在しません。')
    return {'ok':True}


def delete_user(path,uid):
    with sqlite3.connect(path) as c:
        row=c.execute('SELECT username FROM accounts WHERE id=?',(uid,)).fetchone()
        if not row: raise ValueError('ユーザーが存在しません。')
        if row[0]=='kitomoya': raise ValueError('管理者アカウントは削除できません。')
        c.execute('DELETE FROM sessions WHERE account_id=?',(uid,))
        c.execute('DELETE FROM accounts WHERE id=?',(uid,))
    # Inaccessible account files are retained for operator backup/recovery.
    return {'ok':True}


def change_password(path,uid,current,new):
    with sqlite3.connect(path) as c:
        row=c.execute('SELECT salt,password FROM accounts WHERE id=?',(uid,)).fetchone()
        if not row or not hmac.compare_digest(password_hash(current,row[0]),row[1]):
            raise ValueError('現在のパスワードを確認してください。')
        if current==new: raise ValueError('初期パスワードとは異なるパスワードを設定してください。')
        salt=secrets.token_hex(16);hashed=password_hash(new,salt)
        c.execute("UPDATE accounts SET salt=?,password=?,must_change=0,must_change_reason='',password_updated_at=? WHERE id=?",
                  (salt,hashed,profiledb._timestamp(None),uid))
        c.execute('DELETE FROM sessions WHERE account_id=?',(uid,))
    return {'ok':True}


def reset_password(path,uid,password):
    """Set an administrator-provided initial password; revoke all old sessions."""
    salt=secrets.token_hex(16);hashed=password_hash(password,salt)
    with sqlite3.connect(path) as c:
        if c.execute('''UPDATE accounts SET salt=?,password=?,must_change=1,
                     must_change_reason='reset',password_updated_at=? WHERE id=?''',
                     (salt,hashed,profiledb._timestamp(None),uid)).rowcount!=1:
            raise ValueError('ユーザーが存在しません。')
        c.execute('DELETE FROM sessions WHERE account_id=?',(uid,))
    return {'ok':True,'must_change':True}


def set_role(path,username,role):
    if role not in ('admin','user') or not isinstance(username,str):
        raise ValueError('権限は admin または user を指定してください。')
    initialize(path)
    with sqlite3.connect(path) as c:
        row=c.execute('SELECT id,username FROM accounts WHERE username=?',(username.strip().lower(),)).fetchone()
        if not row: raise ValueError('ユーザーが存在しません。')
        if row[1]=='kitomoya' and role!='admin': raise ValueError('kitomoya の管理者権限は解除できません。')
        c.execute('UPDATE accounts SET role=? WHERE id=?',(role,row[0]))
        c.execute('DELETE FROM sessions WHERE account_id=?',(row[0],))
    return {'username':row[1],'role':role}

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


class BindingLockedError(PermissionError):
    """An ordinary account cannot change an already registered broadcaster."""


class BindingConflict(ValueError):
    """Another request completed the first registration before this one."""


class BindingConfirmationRequired(ValueError):
    """The displayed public profile must be explicitly confirmed before saving."""


_SEED_LABELS = ('利用者', '管理者', 'テスト')


def initialize(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,account_id TEXT NOT NULL,csrf TEXT NOT NULL,expires REAL NOT NULL);''')
        c.execute('BEGIN IMMEDIATE')
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
        c.execute('CREATE TABLE IF NOT EXISTS label_catalog(name TEXT PRIMARY KEY,created_at TEXT NOT NULL)')
        stamp=profiledb._timestamp(None)
        c.executemany('INSERT OR IGNORE INTO label_catalog(name,created_at) VALUES(?,?)',
                      [(label,stamp) for label in _SEED_LABELS])
        # Legacy display labels are preserved verbatim. None of these labels
        # grant rights; sessions always read the independent accounts.role.
        c.execute('''INSERT OR IGNORE INTO label_catalog(name,created_at)
                  SELECT DISTINCT label,? FROM accounts''',(stamp,))
    path.chmod(0o600)


def password_hash(password,salt):
    if not isinstance(password,str) or not 12<=len(password)<=1024:
        raise ValueError('パスワードは12〜1024文字で指定してください。')
    with _HASH_SLOTS:
        return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=32768,r=8,p=1,maxmem=64*1024*1024).hex()


def create(path,username,password,reset=False, *, label="利用者", must_change=True):
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
            _selected_label(c,label)
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
        _settings_schema(c)
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


def _settings_schema(c):
    """Add lock metadata without rewriting any existing private registration."""
    if not c.in_transaction:
        c.execute('BEGIN IMMEDIATE')
    columns={r[1] for r in c.execute('PRAGMA table_info(account_settings)')}
    migrating='binding_locked' not in columns
    for name,definition in [('binding_locked','INTEGER NOT NULL DEFAULT 0'),
                            ('binding_confirmed_at','TEXT'),('binding_revision','INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            c.execute(f'ALTER TABLE account_settings ADD COLUMN {name} {definition}')
    if migrating:
        # An existing profile is locked at its current value. Its original
        # registration did not have a confirmation dialog, so do not invent a
        # historical confirmation timestamp for it.
        c.execute('UPDATE account_settings SET binding_locked=CASE WHEN spoon_id IS NULL THEN 0 ELSE 1 END')
    c.execute('''CREATE TABLE IF NOT EXISTS binding_audit(
              id INTEGER PRIMARY KEY,action TEXT NOT NULL,actor_id TEXT,actor_username TEXT,
              target_account_id TEXT,before_id TEXT,after_id TEXT,observed_at TEXT NOT NULL)''')


def _setting_result(row):
    profile={'id':row[0],'name':row[1],'tag':row[2]} if row and row[0] else None
    return {'spoon_profile':profile,'updated_at':row[3] if row else None,
            'binding_locked':bool(row[4]) if row else False,
            'binding_confirmed_at':row[5] if row else None,'binding_revision':row[6] if row else 0}


def account_settings(database,payload=None, *, allow_change=False, confirmed=False, actor=None):
    """Read/save the authenticated account's private broadcaster association.

    The HTTP caller selects the database and passes the verified session actor;
    it must never derive allow_change/actor from a JSON request. Ordinary first
    registration requires confirmation and atomically locks the association.
    Only an explicit administrator operation may replace or clear it. Favorites,
    fan imports and passwords are unaffected by this setting.
    """
    select='''SELECT spoon_id,spoon_name,spoon_tag,updated_at,binding_locked,
              binding_confirmed_at,binding_revision FROM account_settings WHERE singleton=1'''
    with sqlite3.connect(database) as c:
        if payload is None:
            return _setting_result(c.execute(select).fetchone())
        if not isinstance(payload,dict) or 'spoon_profile' not in payload:
            raise ValueError('ご自身のSpoonプロフィールを指定してください。')
        raw=payload['spoon_profile']
        profile=None if raw is None else fans._user(raw)
        administrator=allow_change is True
        if administrator and (not isinstance(actor,dict) or actor.get('role')!='admin'
                              or not isinstance(actor.get('id'),str) or not re.fullmatch('[0-9a-f]{32}',actor['id'])
                              or not isinstance(actor.get('username'),str)
                              or not re.fullmatch('[a-z0-9_-]{3,64}',actor['username'])):
            raise BindingLockedError('配信者の変更・解除は管理者への依頼が必要です。')
        before=c.execute(select).fetchone()
        if not administrator and (before and (before[0] is not None or before[4]) or profile is None):
            raise BindingLockedError('確定済みの配信者は変更・解除できません。管理者へ依頼してください。')
        if confirmed is not True:
            raise BindingConfirmationRequired('表示されたSpoonプロフィールを確認して確定してください。')
        # Read the precondition before taking the write lock, then use a CAS
        # update. Concurrent first-registration requests cannot overwrite the
        # winner even if they both observed an initially empty setting.
        c.execute('BEGIN IMMEDIATE')
        if administrator:
            before=c.execute(select).fetchone()
        stamp=profiledb._timestamp(None)
        parameters=(profile['id'] if profile else None,profile['name'] if profile else None,
                    profile['tag'] if profile else None,stamp,int(profile is not None),stamp if profile else None)
        update='''UPDATE account_settings SET spoon_id=?,spoon_name=?,spoon_tag=?,updated_at=?,
                  binding_locked=?,binding_confirmed_at=?,binding_revision=binding_revision+1 WHERE singleton=1'''
        if not administrator:
            update+=' AND spoon_id IS NULL AND binding_locked=0'
        if c.execute(update,parameters).rowcount!=1:
            raise BindingConflict('別の画面で配信者が確定されました。再読み込みしてください。')
        target=Path(database).parent.name
        target=target if re.fullmatch('[0-9a-f]{32}',target) else None
        action=('admin_clear' if profile is None else 'admin_replace') if administrator else 'initial_bind'
        c.execute('''INSERT INTO binding_audit(action,actor_id,actor_username,target_account_id,
                  before_id,after_id,observed_at) VALUES(?,?,?,?,?,?,?)''',
                  (action,actor.get('id') if isinstance(actor,dict) else None,
                   actor.get('username') if isinstance(actor,dict) else None,target,
                   before[0] if before else None,profile['id'] if profile else None,stamp))
        return _setting_result(c.execute(select).fetchone())


def binding_audit(database,limit=50):
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError('監査履歴は1〜200件で指定してください。')
    with sqlite3.connect(database) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute('SELECT * FROM binding_audit ORDER BY id DESC LIMIT ?',(limit,))]


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


def _selected_label(c,label):
    if not c.execute('SELECT 1 FROM label_catalog WHERE name=?',(label,)).fetchone():
        raise ValueError('登録済みのタグ候補から選択してください。新しいタグは先に追加してください。')


def list_labels(path):
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute('''SELECT l.name,l.created_at,COUNT(a.id) AS user_count
                FROM label_catalog l LEFT JOIN accounts a ON a.label=l.name
                GROUP BY l.name ORDER BY CASE l.name WHEN '利用者' THEN 0 WHEN '管理者' THEN 1
                WHEN 'テスト' THEN 2 ELSE 3 END,l.created_at,l.name''')]


def add_label(path,label):
    label=validate_label(label)
    with sqlite3.connect(path) as c:
        c.execute('INSERT OR IGNORE INTO label_catalog(name,created_at) VALUES(?,?)',(label,profiledb._timestamp(None)))
    return next(item for item in list_labels(path) if item['name']==label)


def list_users(path):
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute('SELECT id,username,label,role,must_change,must_change_reason,created_at,password_updated_at,last_login_at FROM accounts ORDER BY username')]


def set_label(path,uid,label):
    label=validate_label(label)
    with sqlite3.connect(path) as c:
        _selected_label(c,label)
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

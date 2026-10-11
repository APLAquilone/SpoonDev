"""Owner-provided follower lists; no Spoon login credentials are stored."""
from . import profiledb
from .db import _connection

SCHEMA = '''
CREATE TABLE IF NOT EXISTS fan_owners (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, tag TEXT, imported_at TEXT NOT NULL,
 complete INTEGER NOT NULL CHECK(complete IN (0,1)));
CREATE TABLE IF NOT EXISTS registered_fans (
 owner_id TEXT NOT NULL REFERENCES fan_owners(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL, name TEXT, tag TEXT, PRIMARY KEY(owner_id,user_id));
CREATE INDEX IF NOT EXISTS registered_fans_user ON registered_fans(user_id,owner_id);
'''


def initialize(database):
    with _connection(database) as conn:
        conn.executescript(SCHEMA)


def numeric_id(value):
    if isinstance(value,bool) or not isinstance(value,(int,str)):
        raise ValueError('数値IDを指定してください。')
    value=str(value)
    if not value.isascii() or not value.isdigit() or len(value)>20 or int(value)<=0:
        raise ValueError('数値IDを指定してください。')
    return str(int(value))


def _user(raw, require_name=False):
    if not isinstance(raw,dict):
        raw={'id':raw}
    uid=numeric_id(raw.get('id'))
    name=raw.get('name',raw.get('nickname'))
    tag=raw.get('tag')
    if (name is not None and (not isinstance(name,str) or len(name)>500)) or (
        tag is not None and (not isinstance(tag,str) or len(tag)>500)):
        raise ValueError('名前・プロフィールIDの形式を確認してください。')
    if require_name and not name:
        raise ValueError('登録する配信者の名前を指定してください。')
    return {'id':uid,'name':name,'tag':tag}


def import_followers(database,payload):
    if not isinstance(payload,dict):
        raise ValueError('ファン一覧の形式を確認してください。')
    owner=_user(payload.get('owner'),True)
    followers=payload.get('followers')
    complete=payload.get('complete',False)
    if not isinstance(followers,list) or type(complete) is not bool:
        raise ValueError('followers の配列と complete の真偽値を指定してください。')
    canonical={}
    for raw in followers:
        user=_user(raw)
        if user['id'] in canonical and canonical[user['id']]!=user:
            raise ValueError('同じ数値IDに異なる名前が指定されています。')
        canonical[user['id']]=user
    stamp=profiledb._timestamp(None)
    with _connection(database) as conn, profiledb._atomic(conn):
        conn.execute('''INSERT INTO fan_owners VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          name=excluded.name,tag=excluded.tag,imported_at=excluded.imported_at,complete=excluded.complete''',
          (owner['id'],owner['name'],owner['tag'],stamp,int(complete)))
        if complete:
            conn.execute('DELETE FROM registered_fans WHERE owner_id=?',(owner['id'],))
        conn.executemany('''INSERT INTO registered_fans VALUES (?,?,?,?) ON CONFLICT(owner_id,user_id)
          DO UPDATE SET name=COALESCE(excluded.name,registered_fans.name),
          tag=COALESCE(excluded.tag,registered_fans.tag)''',
          [(owner['id'],u['id'],u['name'],u['tag']) for u in canonical.values()])
        # ID-only input does not manufacture a profile or overwrite a known name.
        profiledb.cache_users(conn,[owner]+[u for u in canonical.values() if u['name'] is not None],stamp)
        total=conn.execute('SELECT COUNT(*) FROM registered_fans WHERE owner_id=?',(owner['id'],)).fetchone()[0]
    return {'owner_id':owner['id'],'imported_count':len(canonical),'fan_count':total,'complete':complete}


def clear_followers(database, payload):
    """Reset this owner's fan registration, preserving shared observations."""
    if not isinstance(payload, dict) or payload.get('confirm') is not True:
        raise ValueError('登録ファンの全削除を確認してください。')
    owner_id = numeric_id(payload.get('owner_id'))
    with _connection(database) as conn, profiledb._atomic(conn):
        deleted = conn.execute('DELETE FROM registered_fans WHERE owner_id=?', (owner_id,)).rowcount
        conn.execute('DELETE FROM fan_owners WHERE id=?', (owner_id,))
    return {'owner_id': owner_id, 'deleted_count': deleted, 'fan_count': 0}


def remove_follower(database, payload):
    """Remove one owner-provided relation, keeping the owner's other settings.

    Unlike ``clear_followers``, an empty resulting list still has its owner
    registration. A manually edited full export is no longer a complete list;
    its original import timestamp and cached profiles remain unchanged.
    """
    if not isinstance(payload, dict) or payload.get('confirm') is not True:
        raise ValueError('このファンの登録解除を確認してください。')
    owner_id = numeric_id(payload.get('owner_id'))
    user_id = numeric_id(payload.get('user_id'))
    removed_at = None
    with _connection(database) as conn, profiledb._atomic(conn):
        deleted = conn.execute(
            'DELETE FROM registered_fans WHERE owner_id=? AND user_id=?',
            (owner_id, user_id),
        ).rowcount
        if deleted:
            conn.execute('UPDATE fan_owners SET complete=0 WHERE id=?', (owner_id,))
            removed_at = profiledb._timestamp(None)
        owner = conn.execute('SELECT complete FROM fan_owners WHERE id=?', (owner_id,)).fetchone()
        count = conn.execute('SELECT COUNT(*) FROM registered_fans WHERE owner_id=?',
                             (owner_id,)).fetchone()[0]
    return {'owner_id': owner_id, 'user_id': user_id, 'deleted_count': deleted,
            'fan_count': count, 'complete': bool(owner[0]) if owner else False,
            'removed_at': removed_at}

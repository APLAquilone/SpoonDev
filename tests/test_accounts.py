import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from spoondev import accounts, db, web

class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        self.database=root/'shared.sqlite3'; self.auth=root/'accounts.sqlite3'
        db.initialize(self.database)
        self.uid=accounts.create(self.auth,'alice','example-password-123',must_change=False)
        accounts.create(self.auth,'bob','other-password-123',must_change=False)
        self.server=web.make_server(self.database,port=0,auth=True,auth_database=self.auth)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,body=None,cookie='',csrf=''):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        headers={'Origin':self.origin,'Content-Type':'application/json','Cookie':cookie,'X-CSRF-Token':csrf}
        c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        r=c.getresponse();status=r.status;headers=dict(r.getheaders());raw=r.read();c.close()
        try: data=json.loads(raw)
        except ValueError: data=raw.decode()
        return status,headers,data
    def login(self,name,password):
        status,headers,_=self.request('/api/login',{'username':name,'password':password});self.assertEqual(status,200)
        cookie=headers['Set-Cookie'].split(';')[0]
        actor=accounts.session(self.auth,cookie.split('=')[1]);return cookie,actor['csrf']
    def test_isolation_csrf_revision_logout_reset(self):
        self.assertEqual(self.request('/api/stats')[0],401)
        self.assertEqual(self.request('/')[0],303)
        for user in accounts.list_users(self.auth):
            accounts.account_settings(accounts.private(self.auth,user['id']),
                                      {'spoon_profile':{'id':'316644201','name':'Own DJ'}},confirmed=True)
        alice,csrf=self.login('alice','example-password-123');bob,bcsrf=self.login('bob','other-password-123')
        payload={'users':[{'id':'123','name':'private'}],'revision':0}
        self.assertEqual(self.request('/api/favorites',payload,alice)[0],403)
        self.assertEqual(self.request('/api/favorites',payload,alice,csrf)[0],200)
        self.assertEqual(self.request('/api/favorites',payload,alice,csrf)[0],409)
        self.assertEqual(self.request('/api/favorites',cookie=bob)[2]['users'],[])
        fan={'owner':{'id':'316644201','name':'owner'},'followers':[{'id':'123','name':'private'}],'complete':True}
        self.assertEqual(self.request('/api/fans/import',fan,alice,csrf)[0],200)
        self.assertEqual(len(self.request('/api/fan-owners',cookie=alice)[2]),1)
        self.assertEqual(self.request('/api/fan-owners',cookie=bob)[2],[])
        result=self.request('/api/fans?owner_id=316644201',cookie=bob)
        self.assertEqual(result[2]['fans'],[])
        self.assertEqual(self.request('/api/fans?owner_id=316644201',cookie=alice)[2]['total'],1)
        bob_fan={**fan,'followers':[{'id':'456','name':'bob-only'}]}
        self.assertEqual(self.request('/api/fans/import',bob_fan,bob,bcsrf)[0],200)
        self.assertEqual(self.request('/api/fans?owner_id=316644201',cookie=bob)[2]['fans'][0]['id'],'456')
        self.assertEqual(self.request('/api/fans?owner_id=316644201',cookie=alice)[2]['fans'][0]['id'],'123')
        self.assertEqual(self.request('/api/logout',{},alice,csrf)[0],200)
        self.assertEqual(self.request('/api/favorites',cookie=alice)[0],401)
        accounts.create(self.auth,'bob','new-password-123',reset=True)
        self.assertEqual(self.request('/api/favorites',cookie=bob)[0],401)
    def test_clear_fans_is_scoped_and_preserves_other_data(self):
        accounts.set_role(self.auth,'alice','admin');accounts.set_role(self.auth,'bob','admin')
        alice,csrf=self.login('alice','example-password-123')
        bob,bcsrf=self.login('bob','other-password-123')
        first={'owner':{'id':'10','name':'owner'},'followers':[{'id':'123','name':'fan'}],'complete':True}
        other={**first,'owner':{'id':'20','name':'other'}}
        for cookie,token,payload in [(alice,csrf,first),(alice,csrf,other),(bob,bcsrf,first)]:
            self.assertEqual(self.request('/api/fans/import',payload,cookie,token)[0],200)
        self.request('/api/favorites',{'users':[{'id':'123','name':'favorite'}],'revision':0},alice,csrf)
        clear={'owner_id':'10','confirm':True}
        self.assertEqual(self.request('/api/fans/clear',clear)[0],401)
        self.assertEqual(self.request('/api/fans/clear',clear,alice)[0],403)
        self.assertEqual(self.request('/api/fans/clear',{'owner_id':'10'},alice,csrf)[0],400)
        result=self.request('/api/fans/clear',clear,alice,csrf)
        self.assertEqual(result[0],200);self.assertEqual(result[2]['deleted_count'],1)
        self.assertEqual(self.request('/api/fans?owner_id=10',cookie=alice)[2]['total'],0)
        self.assertEqual(self.request('/api/fans?owner_id=20',cookie=alice)[2]['total'],1)
        self.assertEqual(self.request('/api/fans?owner_id=10',cookie=bob)[2]['total'],1)
        self.assertEqual(len(self.request('/api/favorites',cookie=alice)[2]['users']),1)
        self.assertEqual(len(self.request('/api/fan-owners',cookie=alice)[2]),1)
        self.assertIsNone(self.request('/api/fans?owner_id=10',cookie=alice)[2]['owner'])
        self.assertEqual(self.request('/api/fans/clear',clear,alice,csrf)[2]['deleted_count'],0)

    def test_legacy_claim_restricted_to_kitomoya(self):
        from spoondev import fans
        fans.initialize(self.database)
        fans.import_followers(self.database,{'owner':{'id':'316644201','name':'legacy'},'followers':[],'complete':False})
        with self.assertRaises(ValueError): accounts.claim(self.auth,self.uid,self.database)
        admin=accounts.create(self.auth,'kitomoya','admin-password-123',must_change=False)
        accounts.claim(self.auth,admin,self.database)
        alice,_=self.login('alice','example-password-123')
        kitomoya,csrf=self.login('kitomoya','admin-password-123')
        self.assertEqual(self.request('/api/fan-owners',cookie=alice)[2],[])
        self.assertEqual(len(self.request('/api/fan-owners',cookie=kitomoya)[2]),1)
        self.assertEqual(self.request('/api/fans/clear',{'owner_id':'316644201','confirm':True},kitomoya,csrf)[0],200)
        self.assertEqual(self.request('/api/fan-owners',cookie=kitomoya)[2],[])

    def test_stats_only_kitomoya(self):
        accounts.create(self.auth,'kitomoya','admin-password-123',must_change=False)
        admin,admin_csrf=self.login('kitomoya','admin-password-123')
        alice,csrf=self.login('alice','example-password-123')
        self.assertEqual(self.request('/api/stats',cookie=admin)[0],200)
        self.assertEqual(self.request('/api/stats',cookie=alice)[0],403)
        # Client-supplied fields cannot grant permissions.
        self.assertEqual(self.request('/api/stats?username=kitomoya',cookie=alice)[0],403)
        html=self.request('/',cookie=alice)[2]
        self.assertIn('id="stats" hidden',html)
        self.assertIn('"can_view_stats": false',html)
        self.assertNotIn('id="stats" hidden',self.request('/',cookie=admin)[2])
        # Ordinary users retain their own favorite and fan configuration.
        self.assertEqual(self.request('/api/favorites',{'users':[],'revision':0},alice,csrf)[0],200)
        payload={'owner':{'id':'1','name':'Owner'},'followers':['2'],'complete':True}
        accounts.account_settings(accounts.private(self.auth,self.uid),
                                  {'spoon_profile':{'id':'1','name':'Owner'}},confirmed=True)
        for cookie,token in ((alice,csrf),(admin,admin_csrf)):
            self.assertEqual(self.request('/api/fans/import',payload,cookie,token)[0],200)
        self.assertNotIn('indexed_djs',self.request('/api/fans?owner_id=1',cookie=alice)[2])
        self.assertIn('indexed_djs',self.request('/api/fans?owner_id=1',cookie=admin)[2])
        self.assertEqual(self.request('/api/admin/collection-status',cookie=alice)[0],403)

    def test_first_login_requires_password_change_and_revokes_sessions(self):
        accounts.create(self.auth,'firstuser','initial-password-123')
        cookie,csrf=self.login('firstuser','initial-password-123')
        self.assertEqual(self.request('/',cookie=cookie)[1]['Location'],'/password')
        self.assertEqual(self.request('/api/favorites',cookie=cookie)[0],403)
        self.assertEqual(self.request('/password',cookie=cookie)[0],200)
        payload={'current_password':'initial-password-123','new_password':'personal-password-456'}
        self.assertEqual(self.request('/api/password',payload,cookie)[0],403)
        self.assertEqual(self.request('/api/password',{**payload,'current_password':'wrong-password-123'},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/password',{**payload,'new_password':'initial-password-123'},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/password',payload,cookie,csrf)[0],200)
        self.assertEqual(self.request('/api/favorites',cookie=cookie)[0],401)
        new,_=self.login('firstuser','personal-password-456')
        self.assertEqual(self.request('/api/favorites',cookie=new)[0],200)

    def test_admin_account_management_and_authorization(self):
        accounts.add_label(self.auth,'プラン1');accounts.add_label(self.auth,'プラン3')
        accounts.create(self.auth,'kitomoya','admin-password-123',must_change=False)
        admin,csrf=self.login('kitomoya','admin-password-123')
        user,uc=self.login('alice','example-password-123')
        self.assertEqual(self.request('/api/admin/users',cookie=user)[0],403)
        create={'username':'createduser','password':'initial-password-123','label':'プラン1'}
        self.assertEqual(self.request('/api/admin/users/create',create,user,uc)[0],403)
        self.assertEqual(self.request('/api/admin/users/create',create,admin)[0],403)
        result=self.request('/api/admin/users/create',create,admin,csrf)
        self.assertEqual(result[0],200);uid=result[2]['id']
        listing=self.request('/api/admin/users',cookie=admin)[2]
        record=next(u for u in listing if u['id']==uid)
        self.assertEqual((record['role'],record['label'],record['must_change']),('user','プラン1',1))
        self.assertNotIn('password',record);self.assertNotIn('salt',record)
        self.assertEqual(next(u for u in listing if u['username']=='alice')['label'],'利用者')
        self.assertEqual(self.request('/api/admin/users/label',{'id':uid,'label':'プラン3'},admin,csrf)[0],200)
        created,_=self.login('createduser','initial-password-123')
        self.assertEqual(self.request('/api/admin/users/delete',{'id':uid,'confirm':True},admin,csrf)[0],200)
        self.assertEqual(self.request('/password',cookie=created)[0],401)
        aid=next(u for u in listing if u['username']=='kitomoya')['id']
        self.assertEqual(self.request('/api/admin/users/delete',{'id':aid,'confirm':True},admin,csrf)[0],400)
        # A replacement ID creates a fresh private namespace.
        replacement=self.request('/api/admin/users/create',create,admin,csrf)[2]['id']
        self.assertNotEqual(uid,replacement)

    def test_legacy_migration_labels_and_roles(self):
        import sqlite3
        legacy=Path(self.temp.name)/'legacy.sqlite3'
        with sqlite3.connect(legacy) as c:
            c.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,salt TEXT NOT NULL,password TEXT NOT NULL)')
            c.execute('INSERT INTO accounts VALUES(?,?,?,?)',('a'*32,'kitomoya','00'*16,'hash'))
        accounts.initialize(legacy);accounts.initialize(legacy)
        row=accounts.list_users(legacy)[0]
        self.assertEqual((row['label'],row['role'],row['must_change']),('保守','admin',0))
        accounts.add_label(legacy,'プラン2')
        accounts.set_label(legacy,row['id'],'プラン2');accounts.initialize(legacy)
        self.assertEqual(accounts.list_users(legacy)[0]['label'],'プラン2')

    def test_v02_migration_exempts_legacy_flags_only_once(self):
        import sqlite3
        legacy=Path(self.temp.name)/'legacy-forced.sqlite3'
        salt='11'*16; hashed=accounts.password_hash('existing-password-123',salt)
        with sqlite3.connect(legacy) as c:
            c.execute('''CREATE TABLE accounts(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,
                     salt TEXT NOT NULL,password TEXT NOT NULL,label TEXT NOT NULL,
                     must_change INTEGER NOT NULL,role TEXT NOT NULL)''')
            c.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?)',
                      ('b'*32,'olduser',salt,hashed,'プラン2',1,'user'))
        accounts.initialize(legacy)
        old=accounts.list_users(legacy)[0]
        self.assertEqual((old['must_change'],old['must_change_reason'],old['label']),(0,'','プラン2'))
        self.assertIsNotNone(accounts.login(legacy,'olduser','existing-password-123'))
        initial=accounts.create(legacy,'newuser','initial-password-123')
        accounts.initialize(legacy)
        row=next(u for u in accounts.list_users(legacy) if u['id']==initial)
        self.assertEqual((row['must_change'],row['must_change_reason']),(1,'initial'))
        self.assertTrue(row['created_at'])
        accounts.create(legacy,'olduser','reset-password-123',reset=True)
        accounts.initialize(legacy)
        old=next(u for u in accounts.list_users(legacy) if u['username']=='olduser')
        self.assertEqual((old['must_change'],old['must_change_reason']),(1,'reset'))

    def test_private_spoon_binding_is_scoped_and_preserves_registered_data(self):
        from spoondev import fans
        alice_db=accounts.private(self.auth,self.uid)
        bob_id=next(u['id'] for u in accounts.list_users(self.auth) if u['username']=='bob')
        bob_db=accounts.private(self.auth,bob_id)
        self.assertIsNone(accounts.account_settings(alice_db)['spoon_profile'])
        self.assertFalse(accounts.account_settings(alice_db)['binding_locked'])
        fans.import_followers(alice_db,{'owner':{'id':'999','name':'fan-list-owner'},
          'followers':[{'id':'444','name':'fan'}],'complete':True})
        self.assertIsNone(accounts.account_settings(alice_db)['spoon_profile'])
        accounts.favorites(alice_db,{'users':[{'id':'444','name':'favorite'}],'revision':0})
        profile={'id':'000316644201','name':'きー','tag':'1222kii','password':'discarded'}
        result=accounts.account_settings(alice_db,{'spoon_profile':profile,'account_id':bob_id},confirmed=True)
        self.assertEqual(result['spoon_profile'],{'id':'316644201','name':'きー','tag':'1222kii'})
        self.assertTrue(result['updated_at'])
        accounts.private(self.auth,self.uid)
        self.assertEqual(accounts.account_settings(alice_db),result)
        self.assertIsNone(accounts.account_settings(bob_db)['spoon_profile'])
        for payload in [{},[],{'spoon_profile':True},{'spoon_profile':{'id':'abc'}}]:
            with self.assertRaises(ValueError): accounts.account_settings(alice_db,payload)
        self.assertEqual(accounts.account_settings(alice_db),result)
        administrator={'id':'c'*32,'username':'fixtureadmin','role':'admin'}
        self.assertIsNone(accounts.account_settings(alice_db,{'spoon_profile':None},
                          confirmed=True,allow_change=True,actor=administrator)['spoon_profile'])
        self.assertEqual(len(accounts.favorites(alice_db)['users']),1)
        from spoondev import webdata
        self.assertEqual(webdata.fan_destinations(self.database,'999',private_database=alice_db)['total'],1)

    def test_administrator_password_reset_preserves_identity_and_private_data(self):
        private=accounts.private(self.auth,self.uid)
        accounts.account_settings(private,{'spoon_profile':{'id':'123','name':'Alice Spoon'}},confirmed=True)
        accounts.add_label(self.auth,'プラン3')
        accounts.set_label(self.auth,self.uid,'プラン3')
        accounts.set_role(self.auth,'alice','admin')
        token=accounts.login(self.auth,'alice','example-password-123')
        self.assertIsNotNone(accounts.session(self.auth,token))
        result=accounts.reset_password(self.auth,self.uid,'new-initial-password-456')
        self.assertEqual(result,{'ok':True,'must_change':True})
        self.assertIsNone(accounts.session(self.auth,token))
        self.assertIsNone(accounts.login(self.auth,'alice','example-password-123'))
        new_token=accounts.login(self.auth,'alice','new-initial-password-456')
        self.assertTrue(accounts.session(self.auth,new_token)['must_change'])
        accounts.initialize(self.auth)
        row=next(u for u in accounts.list_users(self.auth) if u['id']==self.uid)
        self.assertEqual((row['label'],row['role'],row['must_change_reason']),('プラン3','admin','reset'))
        self.assertEqual(accounts.account_settings(private)['spoon_profile']['id'],'123')
        with self.assertRaises(ValueError): accounts.reset_password(self.auth,self.uid,'short')
        with self.assertRaises(ValueError): accounts.reset_password(self.auth,'f'*32,'unused-password-123')
        self.assertIsNotNone(accounts.session(self.auth,new_token))
        accounts.change_password(self.auth,self.uid,'new-initial-password-456','personal-password-789')
        row=next(u for u in accounts.list_users(self.auth) if u['id']==self.uid)
        self.assertEqual((row['must_change'],row['must_change_reason']),(0,''))

    def test_explicit_role_assignment_persists_and_revokes_sessions(self):
        admin=accounts.create(self.auth,'kitomoya','admin-password-123',must_change=False)
        root,rc=self.login('kitomoya','admin-password-123')
        alice,ac=self.login('alice','example-password-123')
        action={'username':'bob','role':'admin'}
        self.assertEqual(self.request('/api/admin/users/role',action,alice,ac)[0],403)
        accounts.set_label(self.auth,self.uid,'管理者')
        self.assertEqual(self.request('/api/admin/users',cookie=alice)[0],403)
        self.assertEqual(self.request('/api/admin/users/role',{'username':'alice','role':'admin'},root,rc)[0],200)
        self.assertEqual(self.request('/api/admin/users',cookie=alice)[0],401)
        accounts.initialize(self.auth)
        promoted,pc=self.login('alice','example-password-123')
        self.assertEqual(self.request('/api/admin/users',cookie=promoted)[0],200)
        self.assertEqual(self.request('/api/stats',cookie=promoted)[0],403)
        self.assertEqual(self.request('/api/admin/users/role',{'username':'alice','role':'user'},promoted,pc)[0],400)
        self.assertEqual(self.request('/api/admin/users/role',{'username':'kitomoya','role':'user'},promoted,pc)[0],400)
        self.assertEqual(self.request('/api/admin/users/role',{'username':'alice','role':'user'},root,rc)[0],200)
        self.assertEqual(self.request('/api/admin/users',cookie=promoted)[0],401)
        demoted,_=self.login('alice','example-password-123')
        self.assertEqual(self.request('/api/admin/users',cookie=demoted)[0],403)

    def test_settings_dashboard_http_isolation_validation_and_errors(self):
        from spoondev import profiledb
        from unittest.mock import patch
        import sqlite3
        profiledb.cache_users(self.database,[{'id':'123','name':'Own DJ','tag':'mydj'}])
        alice,csrf=self.login('alice','example-password-123');bob,bc=self.login('bob','other-password-123')
        self.assertEqual(self.request('/api/account/settings',{'spoon_id':'https://www.spooncast.net/jp/channel/123/tab/home','confirmed':True},alice,csrf)[0],200)
        self.assertEqual(self.request('/api/dashboard',cookie=alice)[2]['profile']['id'],'123')
        self.assertIsNone(self.request('/api/dashboard',cookie=bob)[2]['profile'])
        self.assertEqual(self.request('/api/account/settings',{},alice,csrf)[0],400)
        self.assertEqual(self.request('/api/account/settings',cookie=alice)[2]['spoon_profile']['id'],'123')
        self.assertEqual(self.request('/api/account/settings',{'spoon_id':'https://evil.example/jp/channel/123','confirmed':True},bob,bc)[0],400)
        self.assertEqual(self.request('/api/account/settings',{'spoon_id':'123'},alice)[0],403)
        with patch('spoondev.dashboard.summary',side_effect=sqlite3.OperationalError('busy')):
            self.assertEqual(self.request('/api/dashboard',cookie=alice)[0],503)
        self.assertEqual(self.request('/api/account/settings',{'spoon_id':None,'confirmed':True},alice,csrf)[0],403)
        self.assertEqual(self.request('/api/dashboard',cookie=alice)[2]['profile']['id'],'123')

    def test_admin_reset_http_and_normalized_self_guard(self):
        accounts.create(self.auth,'kitomoya','admin-password-123',must_change=False)
        root,rc=self.login('kitomoya','admin-password-123')
        alice,ac=self.login('alice','example-password-123')
        self.assertEqual(self.request('/api/admin/users/reset-password',{'id':self.uid,'password':'new-initial-123'},alice,ac)[0],403)
        self.assertEqual(self.request('/api/admin/users/reset-password',{'id':self.uid,'password':'new-initial-123'},root,rc)[0],200)
        self.assertEqual(self.request('/api/favorites',cookie=alice)[0],401)
        initial,ic=self.login('alice','new-initial-123')
        self.assertEqual(self.request('/api/favorites',cookie=initial)[0],403)
        self.assertEqual(self.request('/api/admin/users/role',{'username':' KITOMOYA ','role':'user'},root,rc)[0],400)

    def test_wrong_password_and_public_validation(self):
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'wrong-password-123'})[0],401)
        for _ in range(10): self.request('/api/login',{'username':'alice','password':'wrong-password-123'})
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'example-password-123'})[0],429)
        with self.assertRaises(ValueError): web.make_server(self.database,public_url='https://example.com')
        with self.assertRaises(ValueError): web.make_server(self.database,host='0.0.0.0',auth=True,public_url='https://example.com')

    def test_favorites_read_revision_stays_with_list_during_other_device_save(self):
        import sqlite3
        from unittest.mock import patch
        private=accounts.private(self.auth,self.uid)
        accounts.favorites(private,{'users':[{'id':'123','name':'old favorite'}],'revision':0})
        connect=sqlite3.connect
        with connect(private) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        committed=[]

        class FavoriteRows:
            def __init__(self,cursor):self.cursor=cursor
            def __iter__(self):return iter(self.fetchall())
            def fetchall(self):
                rows=self.cursor.fetchall()
                # Simulate another device committing after GET reads the list,
                # before GET reads its revision. WAL allows this while the
                # reader's transaction retains its original snapshot.
                with connect(private) as writer:
                    writer.execute('DELETE FROM favorites')
                    writer.execute("INSERT INTO favorites VALUES('456','other device',NULL)")
                    writer.execute('UPDATE favorite_revision SET revision=revision+1')
                committed.append(True)
                return rows

        class InterleavedRead(sqlite3.Connection):
            def execute(self,sql,*args,**kwargs):
                cursor=super().execute(sql,*args,**kwargs)
                return FavoriteRows(cursor) if sql=='SELECT * FROM favorites ORDER BY rowid' else cursor

        def read_connection(path,*args,**kwargs):
            if str(path)==private:kwargs['factory']=InterleavedRead
            return connect(path,*args,**kwargs)

        with patch('spoondev.accounts.sqlite3.connect',side_effect=read_connection):
            snapshot=accounts.favorites(private)
        self.assertEqual(committed,[True])
        self.assertEqual(snapshot['users'],[{'id':'123','name':'old favorite','tag':None}])
        self.assertEqual(snapshot['revision'],1)
        # The old list must retain its old revision and be rejected on save;
        # it cannot acquire revision 2 and overwrite the other device's change.
        self.assertIsNone(accounts.favorites(private,{'users':snapshot['users'],'revision':snapshot['revision']}))
        current=accounts.favorites(private)
        self.assertEqual(current['revision'],2)
        self.assertEqual(current['users'],[{'id':'456','name':'other device','tag':None}])

if __name__=='__main__':unittest.main()

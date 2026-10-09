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
        admin,_=self.login('kitomoya','admin-password-123')
        alice,csrf=self.login('alice','example-password-123')
        self.assertEqual(self.request('/api/stats',cookie=admin)[0],200)
        self.assertEqual(self.request('/api/stats',cookie=alice)[0],403)
        # Client-supplied fields cannot grant permissions.
        self.assertEqual(self.request('/api/stats?username=kitomoya',cookie=alice)[0],403)
        html=self.request('/',cookie=alice)[2]
        self.assertIn('id="stats" hidden',html)
        self.assertIn('"can_view_stats": false',html)
        self.assertIn('ファン一覧</button>',html)
        self.assertNotIn('id="stats" hidden',self.request('/',cookie=admin)[2])
        # Ordinary users retain their own favorite and fan configuration.
        self.assertEqual(self.request('/api/favorites',{'users':[],'revision':0},alice,csrf)[0],200)

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
        self.assertEqual(next(u for u in listing if u['username']=='alice')['label'],'保守')
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
        self.assertEqual((row['label'],row['role'],row['must_change']),('保守','admin',1))
        accounts.set_label(legacy,row['id'],'プラン2');accounts.initialize(legacy)
        self.assertEqual(accounts.list_users(legacy)[0]['label'],'プラン2')

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

    def test_wrong_password_and_public_validation(self):
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'wrong-password-123'})[0],401)
        for _ in range(10): self.request('/api/login',{'username':'alice','password':'wrong-password-123'})
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'example-password-123'})[0],429)
        with self.assertRaises(ValueError): web.make_server(self.database,public_url='https://example.com')
        with self.assertRaises(ValueError): web.make_server(self.database,host='0.0.0.0',auth=True,public_url='https://example.com')

if __name__=='__main__':unittest.main()

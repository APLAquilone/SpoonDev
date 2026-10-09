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
        self.uid=accounts.create(self.auth,'alice','example-password-123')
        accounts.create(self.auth,'bob','other-password-123')
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
        self.assertEqual(self.request('/api/logout',{},alice,csrf)[0],200)
        self.assertEqual(self.request('/api/favorites',cookie=alice)[0],401)
        accounts.create(self.auth,'bob','new-password-123',reset=True)
        self.assertEqual(self.request('/api/favorites',cookie=bob)[0],401)
    def test_wrong_password_and_public_validation(self):
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'wrong-password-123'})[0],401)
        for _ in range(10): self.request('/api/login',{'username':'alice','password':'wrong-password-123'})
        self.assertEqual(self.request('/api/login',{'username':'alice','password':'example-password-123'})[0],429)
        with self.assertRaises(ValueError): web.make_server(self.database,public_url='https://example.com')
        with self.assertRaises(ValueError): web.make_server(self.database,host='0.0.0.0',auth=True,public_url='https://example.com')

if __name__=='__main__':unittest.main()

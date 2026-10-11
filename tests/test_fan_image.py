"""Bounded browser OCR/search/import behavior without external OCR or Spoon calls."""
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE_HARNESS = r"""
const fs=require('fs'),assert=require('assert');
(async()=>{
  const source=fs.readFileSync(process.argv[1],'utf8');
  const m=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const file=(size=1024,type='image/png')=>({size,type});
  assert.throws(()=>m.validateImageFiles([]),/選択/);
  assert.throws(()=>m.validateImageFiles(Array(4).fill(file())),/3枚/);
  assert.throws(()=>m.validateImageFiles([file(1,'image/svg+xml')]),/JPEG/);
  assert.throws(()=>m.validateImageFiles([file(8*1024*1024+1)]),/8MB/);
  assert.throws(()=>m.validateImageFiles(Array(3).fill(file(6*1024*1024))),/16MB/);
  assert.equal(m.validateImageFiles([file(),file()]).length,2);
  assert.deepEqual(m.candidateNames('フォロワー\n9:48\n71%\nきー\nきー\nしらたまもなか\n@name'),['きー','しらたまもなか','@name']);
  assert.equal(m.candidateNames(Array.from({length:70},(_,i)=>'name'+i).join('\n')).length,30);
  assert.deepEqual(m.validSearchUsers([{id:12,name:'同名'},{id:13,name:'同名'},{id:12},{id:'0'},{id:'abc'},{id:'1'.repeat(21)}]).map(x=>x.id),['12','13']);

  let active=0,max=0;
  const lookup=await m.lookupCandidates(['a-name','b-name','c-name'],async query=>{
    active++;max=Math.max(max,active);await new Promise(r=>setTimeout(r,5));active--;
    if(query==='b-name')throw Error('検索失敗');return [{id:query==='a-name'?'12':'13',name:'同名'},{id:'bad'}];
  });
  assert.equal(max,2);assert.equal(lookup[0].users[0].id,'12');assert.equal(lookup[1].error,'検索失敗');
  const tagLookup=await m.lookupCandidates(['@1222kii'],async keyword=>{
    assert.equal(keyword,'1222kii');return [{id:'316644201',tag:'1222kii'}];
  });
  assert.equal(tagLookup[0].query,'@1222kii');assert.equal(tagLookup[0].users[0].id,'316644201');
  const cancelSearch=new AbortController();
  const pending=m.lookupCandidates(['pending'],()=>new Promise(()=>{}),{signal:cancelSearch.signal});
  cancelSearch.abort();await assert.rejects(pending,{name:'AbortError'});

  let terminated=0,recognized=0,decodes=0;
  const text=await m.recognizeImages([file(),file()],{decode:async()=>{decodes++;return new Uint8Array([1]);},workerFactory:()=>({initialize:async()=>{},recognize:async()=>{recognized++;return 'きー';},terminate(){terminated++;}})});
  assert.equal(text,'きー\nきー');assert.equal(recognized,2);assert.equal(decodes,2);assert.equal(terminated,1);
  await assert.rejects(m.recognizeImages([file()],{decode:async()=>{throw Error('decode failed');},workerFactory:()=>({initialize:async()=>{},recognize:async()=>{},terminate(){terminated++;}})}),/decode failed/);
  assert.equal(terminated,2);

  const workerMessages=[];let workerTerminations=0;
  class FakeWorker{
    constructor(url){assert.ok(url.endsWith('/ocr/worker.min.js'));}
    terminate(){workerTerminations++;}
    postMessage(packet){workerMessages.push(packet);queueMicrotask(()=>this.onmessage({data:{...packet,status:'resolve',data:packet.action==='recognize'?{text:'しらたま'}:{}}}));}
  }
  const ocr=m.createOcrWorker({WorkerClass:FakeWorker,baseUrl:'http://localhost/ocr/'});await ocr.initialize();
  assert.equal(await ocr.recognize(new Uint8Array([1])),'しらたま');ocr.terminate();
  assert.deepEqual(workerMessages.map(x=>x.action),['load','loadLanguage','initialize','setParameters','recognize']);
  assert.equal(workerMessages[0].payload.options.corePath,'http://localhost/ocr/tesseract-core-lstm.wasm.js');
  assert.equal(workerMessages[1].payload.options.cacheMethod,'none');assert.equal(workerTerminations,1);
  class StalledWorker extends FakeWorker{postMessage(){}}
  const cancelOcr=new AbortController(),stalled=m.createOcrWorker({WorkerClass:StalledWorker,signal:cancelOcr.signal,baseUrl:'http://localhost/ocr/'});
  const initialization=stalled.initialize();cancelOcr.abort();await assert.rejects(initialization,{name:'AbortError'});assert.equal(workerTerminations,2);
  const realTimer=global.setTimeout;
  global.setTimeout=(fn,ms)=>ms>=60000?realTimer(fn,0):realTimer(fn,ms);
  const expired=m.createOcrWorker({WorkerClass:StalledWorker,baseUrl:'http://localhost/ocr/'});
  await assert.rejects(expired.initialize(),/時間切れ/);global.setTimeout=realTimer;assert.equal(workerTerminations,3);

  class Element{
    constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.listeners={};this.value='';this.textContent='';}
    append(...items){this.children.push(...items);}
    replaceChildren(...items){this.children=[...items];}
    addEventListener(name,fn){this.listeners[name]=fn;}
    querySelectorAll(selector){const all=[];function walk(e){for(const c of e.children){if(c.tag==='input'&&c.dataset.userId)all.push(c);walk(c);}}walk(this);return all;}
    setAttribute(){}
    async click(){await this.listeners.click?.();}
  }
  global.document={createElement:tag=>new Element(tag)};
  const container=new Element('div');let owner='316644201',added=[];
  const widget=m.mountFanImageImport(container,{ownerId:()=>owner,searchUsers:async()=>[{id:'12',name:'同名'},{id:'13',name:'同名'}],addFans:async users=>{added.push(users);return false;}});
  const details=container.children[0],textLabel=details.children.find(e=>e.children.some(c=>c.tag==='textarea')),textarea=textLabel.children.find(e=>e.tag==='textarea');
  const find=details.children.find(e=>e.tag==='button'&&e.textContent==='候補を検索'),add=details.children.at(-1);
  textarea.value='同名';await find.click();assert.equal(added.length,0);assert.equal(add.disabled,true);
  let checks=container.querySelectorAll();assert.equal(checks.length,2);assert.equal(checks[0].checked,false);
  checks[0].checked=true;checks[0].listeners.change();assert.equal(add.disabled,false);await add.click();
  assert.equal(added.length,1);assert.deepEqual(added[0].map(x=>x.id),['12']);assert.equal(add.disabled,false);
  owner='999';await add.click();assert.equal(added.length,1);assert.equal(add.disabled,true);
  textarea.value='同名';await find.click();checks=container.querySelectorAll();checks[0].checked=true;checks[0].listeners.change();
  owner=undefined;await add.click();assert.equal(added.length,1);assert.equal(add.disabled,true);
  widget.reset();assert.equal(textarea.value,'');widget.destroy();assert.equal(container.children.length,0);
  console.log(JSON.stringify({bounded_inputs:true,search_concurrency:max,cancel_search:true,ocr_cleanup:true,worker_protocol:true,explicit_id_selection:true,owner_guard:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


class FanImageAssetsTests(unittest.TestCase):
    def test_pinned_assets_match_recorded_checksums_and_licenses(self):
        directory=ROOT/'spoondev/static/ocr'
        manifest=json.loads((directory/'manifest.json').read_text())
        self.assertEqual(manifest['tesseract_js_version'],'6.0.1')
        self.assertEqual(manifest['tesseract_js_core_version'],'6.0.0')
        self.assertEqual(manifest['tessdata_fast_version'],'4.1.0')
        for name,record in manifest['assets'].items():
            data=(directory/name).read_bytes()
            self.assertEqual(len(data),record['size'])
            self.assertEqual(hashlib.sha256(data).hexdigest(),record['sha256'])
            if name.endswith('.gz'):
                self.assertEqual(hashlib.sha256(gzip.decompress(data)).hexdigest(),record['upstream_sha256'])
        for name in ('TESSERACT-LICENSE.md','CORE-LICENSE.txt','TESSDATA-LICENSE.txt','worker.min.js.LICENSE.txt'):
            self.assertGreater((directory/name).stat().st_size,100)

    @unittest.skipUnless(shutil.which('node'),'Node.js is optional; browser OCR behavior checks require it')
    def test_browser_selection_cancellation_and_resource_bounds(self):
        node=shutil.which('node')
        version=subprocess.run([node,'-p','process.versions.node'],check=True,capture_output=True,text=True,timeout=5).stdout.strip()
        if int(version.split('.')[0])<18:
            self.skipTest('Optional browser OCR behavior checks require Node.js 18 or newer')
        result=subprocess.run([node,'-e',NODE_HARNESS,str(ROOT/'spoondev/static/fan-image.js')],check=True,capture_output=True,text=True,timeout=15)
        values=json.loads(result.stdout)
        self.assertEqual(values['search_concurrency'],2)
        self.assertTrue(values['explicit_id_selection'])
        self.assertTrue(values['ocr_cleanup'])
        self.assertTrue(values['owner_guard'])

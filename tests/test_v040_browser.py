
"""Run the actual v0.4 browser functions against a small DOM and mocked API."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
function extract(start,end){const begin=source.indexOf(start),finish=source.indexOf(end,begin);assert(begin>=0&&finish>begin,start);return source.slice(begin,finish)}
class Element{
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.listeners={};this._text='';this.hidden=false;this.disabled=false;this.open=false;this.value='';this.className='';this.isConnected=true;this.classList={add:(...values)=>{this.className+=' '+values.join(' ')}}}
 set textContent(value){this._text=String(value);this.children=[]}get textContent(){return this._text+this.children.map(item=>item.textContent).join('')}
 append(...items){for(const item of items){if(item.parentNode)item.remove();item.parentNode=this;this.children.push(item)}}
 replaceChildren(...items){for(const item of this.children)item.parentNode=null;this.children=[];this._text='';this.append(...items)}
 setAttribute(name,value){this.attributes[name]=String(value)}getAttribute(name){return this.attributes[name]}
 addEventListener(type,fn){(this.listeners[type]||=[]).push(fn)}
 remove(){if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(item=>item!==this);this.parentNode=null}}
 replaceWith(item){const parent=this.parentNode;if(!parent)return;item.remove();const index=parent.children.indexOf(this);parent.children.splice(index,1,item);item.parentNode=parent;this.parentNode=null}
 matches(selector){if(selector.startsWith('.'))return this.className.split(/\s+/).includes(selector.slice(1));if(selector==='details[open]')return this.tagName==='DETAILS'&&this.open;if(selector==='[data-fan-candidate-id]')return this.dataset.fanCandidateId!==undefined;return this.tagName.toLowerCase()===selector}
 querySelectorAll(selector){return this.children.flatMap(item=>[...(item.matches(selector)?[item]:[]),...item.querySelectorAll(selector)])}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null}
 showModal(){this.open=true}close(){this.open=false;for(const fn of this.listeners.close||[])fn()}
}
const elements=new Map();
function $(id){if(!elements.has(id))elements.set(id,new Element(id.includes('dialog')?'dialog':'div'));return elements.get(id)}
function node(tag,text,cls){const value=new Element(tag);if(text!==undefined)value.textContent=text;if(cls)value.className=cls;return value}
const posts=[];
const context=vm.createContext({Map,Set,Promise,Date,Intl,AbortController,URLSearchParams,String,Number,Error,console,queueMicrotask,
 $,node,state:(target,text)=>target.replaceChildren(node('p',text)),date:value=>value,compactDate:value=>value,temp:value=>typeof value==='number'?value+'°':'—',
 identity:user=>(user.tag?'@'+user.tag+' · ':'')+'ID '+(user.id??user.user_id),
 person:(user,fn)=>{const button=node('button',user.name,'person');button.onclick=fn;return button},
 selectUser(){},isFavorite:()=>false,saveFavorites:async()=>true,
 favorites:[],selected:null,account:{id:'account-one',csrf:'token',role:'admin'},accountSettings:{spoon_profile:{id:'11',name:'Own'}},
 expandedListenerRows:new Set(),compactUserCache:new Map(),compactRowSerial:0,
 appendDestinationPreview(target){target.append(node('div','previews','destination-preview'))},listenerMetrics:()=>node('details','metrics'),
 fanOwner:{id:'11',name:'Own'},fanOwners:[],fanAbort:undefined,fanOffset:0,fanSeq:0,fanQuery:'',fanActivity:'all',fanImportBusy:false,fanOwnerDraft:true,
 loadFanOwners:async()=>{},loadFans:async()=>{},labelCatalog:[],confirm:()=>true,openAdminBinding(){},openAdminReset(){},
 postAccount:async()=>{},window:{},document:{querySelectorAll:selector=>[...elements.values()].flatMap(item=>item.querySelectorAll(selector))},
 fetch:async(path,options)=>{posts.push({path,options,body:JSON.parse(options.body)});return {ok:true,json:async()=>({imported_count:1,fan_count:2})}}
});
vm.runInContext(extract('function listenerDestination(', '\nfunction observedInterval('),context);
vm.runInContext(extract('function compactListenerTable(', '\nfunction renderFavorites('),context);
vm.runInContext(extract('let fanSource=', '\nfunction resetFanSettings('),context);
vm.runInContext(extract('function labelSelect(', '\nasync function loadAdmin('),context);
vm.runInContext(extract('async function loadAdmin(', "\n$('add-label-form')"),context);
(async()=>{
 context.api=async(path)=>path.startsWith('/api/dashboard/listener-followup')?{
  baseline:{user_id:'11',name:'Own',observed_at:'2026-10-10T10:00:00Z',favorite_temperature:20,complete:true},
  observations:[{user_id:'22',name:'Other',observed_at:'2026-10-10T11:00:00Z',favorite_temperature:0,complete:false}],has_more:false
 }:{id:'33',name:'Listener',broadcasters:[]};
 const user={id:'33',name:'Listener',last_seen_at:'2026-10-10T10:00:00Z',latest_temperature:20,last_live_at:'2026-10-10T11:00:00Z',
  own_observation:{user_id:'11',name:'Own',observed_at:'2026-10-10T10:00:00Z',favorite_temperature:20,complete:true},
  latest_observation:{user_id:'22',name:'Other',observed_at:'2026-10-10T11:00:00Z',favorite_temperature:0,complete:false}};
 const table=context.compactListenerTable([user],'dashboard'),body=table.children[1],observed=body.children[0].children[1];
 assert(observed.textContent.includes('Other 0°'));assert(!observed.textContent.includes('Own'));assert(observed.textContent.includes('2026-10-10T11:00:00Z'));
 const expand=body.children[0].children[0].children[0].children.at(-1);await expand.onclick();
 const timeline=body.children[1].querySelector('.listener-followup');assert(timeline.textContent.includes('自枠で最後に観測'));assert(timeline.textContent.includes('Own 20°'));assert(timeline.textContent.includes('Other 0°'));assert(timeline.textContent.includes('部分取得'));
 context.api=async(path)=>path==='/api/admin/labels'?{labels:['利用者','管理者']}:[
  {id:'two',username:'someone',role:'user',label:'管理者',must_change:true},
  {id:'three',username:'manager',role:'admin',label:'利用者',must_change:false}];
 await context.loadAdmin();const rows=$('admin-users').children;
 assert(rows.every(row=>row.tagName==='DETAILS'&&!row.open));assert.strictEqual(rows[0].children[0].textContent,'someone利用者');assert.strictEqual(rows[1].children[0].textContent,'manager管理者');
 assert(rows[0].children[1].textContent.includes('初期パスワード変更待ち'));assert(!rows[0].children[0].textContent.includes('パスワード'));rows[0].open=true;await context.loadAdmin();assert($('admin-users').children[0].open);
 const selected=context.fanCandidateCheckbox({id:'44',name:'Fan',tag:'fan'});selected.checked=true;selected.onchange();assert($('fan-selection-count').textContent.includes('1人'));
 const promise=context.openFanSelectionReview([{id:'44',name:'Fan',tag:'fan'}]);assert.strictEqual($('fan-review-users').textContent,'Fan@fan · ID 44');$('fan-review-confirm').checked=true;await $('fan-review-save').onclick();await promise;
 assert.strictEqual(posts[0].path,'/api/fans/import');assert.strictEqual(posts[0].body.complete,false);assert.strictEqual(posts[0].body.owner.id,'11');assert.deepStrictEqual(posts[0].body.followers,[{id:'44',name:'Fan',tag:'fan'}]);assert.strictEqual(posts[0].options.headers['X-CSRF-Token'],'token');
 await context.removeFan({id:'44',name:'Fan'},node('button'));assert.strictEqual(posts[1].path,'/api/fans/remove');assert.deepStrictEqual(posts[1].body,{owner_id:'11',user_id:'44',confirm:true});
 await assert.rejects(context.openFanSelectionReview([{id:'not-id',name:'Wrong'}]),/数値ID/);
 const candidateRequests=[],candidateRenders=[];context.renderFanList=data=>candidateRenders.push(data.fans.map(user=>user.id));
 context.listActivityApi=(path,request)=>new Promise((resolve,reject)=>{const entry={path,request,resolve};candidateRequests.push(entry);request.signal.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error)});});
 vm.runInContext("fanSource='observed'",context);const firstCandidates=context.loadFanCandidates();
 const backgroundCandidates=context.loadFanCandidates({background:true});assert.strictEqual(candidateRequests.length,1);await backgroundCandidates;
 vm.runInContext("fanSource='monthly'",context);const secondCandidates=context.loadFanCandidates();assert(candidateRequests[0].request.signal.aborted);assert(!candidateRequests[1].request.signal.aborted);
 candidateRequests[0].resolve({users:[{id:'old'}],total:1,state:'ready',checked_at:'old'});candidateRequests[1].resolve({users:[{id:'new'}],total:1,state:'ready',checked_at:'new'});await firstCandidates;await secondCandidates;assert.deepStrictEqual(candidateRenders,[['new']]);

 console.log(JSON.stringify({latest:true,timeline:true,admin:true,partial_import:true,individual_remove:true,numeric_identity:true,candidate_ownership:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""

@unittest.skipUnless(shutil.which('node'), 'Node.js is optional; browser UI checks require it')
class V040BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node=shutil.which('node')
        version=subprocess.run([node,'-p','process.versions.node'],check=True,capture_output=True,text=True,timeout=5).stdout.strip()
        if int(version.split('.')[0])<18:
            raise unittest.SkipTest('Optional browser checks require Node.js 18 or newer')
        source=Path(__file__).resolve().parents[1]/'spoondev/static/index.html'
        result=subprocess.run([node,'-e',HARNESS,str(source)],check=False,capture_output=True,text=True,timeout=15)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.results=json.loads(result.stdout)
    def test_dashboard_uses_latest_all_dj_observation(self):
        self.assertTrue(self.results['latest'])
    def test_followup_keeps_own_baseline_and_temperature_zero(self):
        self.assertTrue(self.results['timeline'])
    def test_admin_summary_only_exposes_username_and_actual_role(self):
        self.assertTrue(self.results['admin'])
    def test_smartphone_add_is_confirmed_partial_and_csrf_scoped(self):
        self.assertTrue(self.results['partial_import'])
    def test_individual_remove_preserves_other_settings(self):
        self.assertTrue(self.results['individual_remove'])
    def test_same_name_resolution_requires_numeric_identity(self):
        self.assertTrue(self.results['numeric_identity'])


    def test_candidate_source_changes_cancel_old_request_without_background_starvation(self):
        self.assertTrue(self.results['candidate_ownership'])

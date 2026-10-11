// Images stay on this device. Only explicitly searched names and selected IDs use the API.
export const IMAGE_LIMITS = Object.freeze({files:3, fileBytes:8*1024*1024,
  totalBytes:16*1024*1024, pixels:4000000, edge:2400, candidates:30, searchConcurrency:2});

function abortError(){return new DOMException('中止しました。','AbortError');}
function assertActive(signal){if(signal?.aborted)throw abortError();}
const numericId=value=>/^\d{1,20}$/.test(String(value??''))&&!/^0+$/.test(String(value));

export function validateImageFiles(values){
  const files=Array.from(values||[]);
  if(!files.length)throw Error('フォロワー画面の画像を選択してください。');
  if(files.length>IMAGE_LIMITS.files)throw Error('画像は一度に3枚まで選択できます。');
  let total=0;
  for(const file of files){
    if(!['image/jpeg','image/png','image/webp'].includes(file.type))throw Error('JPEG・PNG・WebPの画像を選択してください。');
    if(!Number.isFinite(file.size)||file.size<=0||file.size>IMAGE_LIMITS.fileBytes)throw Error('画像1枚は8MB以内にしてください。');
    total+=file.size;
  }
  if(total>IMAGE_LIMITS.totalBytes)throw Error('画像の合計は16MB以内にしてください。');
  return files;
}

export function candidateNames(text){
  const seen=new Set(),names=[];
  const chrome=/^(?:フォロー中|フォロワー|フォロー|フォローする|フォロー解除|ファン|一覧|全体|戻る|検索|閉じる|following|followers|follow|cancel|search)$/i;
  for(const raw of String(text||'').split(/\r?\n/)){
    const name=raw.replace(/[\u0000-\u001f]/g,'').trim();
    if(!name||name.length>200||chrome.test(name)||/^\d{1,2}:\d{2}$/.test(name)||/^\d{1,3}(?:\s*[%人])?$/.test(name))continue;
    const key=name.normalize('NFKC').toLocaleLowerCase('ja');
    if(!seen.has(key)){seen.add(key);names.push(name);}
    if(names.length===IMAGE_LIMITS.candidates)break;
  }
  return names;
}

export function validSearchUsers(values){
  const seen=new Set();
  return Array.from(values||[]).filter(user=>{
    if(!user||!numericId(user.id)||seen.has(String(user.id)))return false;
    seen.add(String(user.id));return true;
  }).slice(0,10).map(user=>({id:String(user.id),name:String(user.name||user.id),tag:user.tag?String(user.tag):null}));
}

function delay(milliseconds,signal){
  return new Promise((resolve,reject)=>{
    assertActive(signal);
    const abort=()=>{clearTimeout(timer);reject(abortError());};
    const timer=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve();},milliseconds);
    signal?.addEventListener('abort',abort,{once:true});
  });
}

export async function lookupCandidates(names,searchUsers,{signal,onResult=()=>{}}={}){
  const queries=candidateNames(names.join('\n')),results=new Array(queries.length);
  let next=0;
  async function run(){
    while(next<queries.length){
      assertActive(signal);
      const index=next++,query=queries[index],request=new AbortController();
      const abort=()=>request.abort();signal?.addEventListener('abort',abort,{once:true});
      let timedOut=false;
      const timeout=setTimeout(()=>{timedOut=true;request.abort();},20000);
      let interrupted;
      try{
        const canceled=new Promise((resolve,reject)=>{interrupted=()=>reject(abortError());request.signal.addEventListener('abort',interrupted,{once:true});});
        // Printed profile IDs include @; the public search takes the bare tag.
        const keyword=/^@[A-Za-z0-9._-]+$/.test(query)?query.slice(1):query;
        const users=await Promise.race([Promise.resolve().then(()=>searchUsers(keyword,request.signal)),canceled]);assertActive(signal);
        results[index]={query,users:validSearchUsers(users),error:null};
      }catch(error){
        assertActive(signal);
        results[index]={query,users:[],error:timedOut?'検索が時間切れになりました。名前を編集して再検索できます。':String(error?.message||'検索できませんでした。')};
      }finally{clearTimeout(timeout);signal?.removeEventListener('abort',abort);request.signal.removeEventListener('abort',interrupted);}
      assertActive(signal);onResult(results[index],index);
      // These are interactive public searches, not an unbounded account crawl.
      if(next<queries.length)await delay(400,signal);
    }
  }
  await Promise.all(Array.from({length:Math.min(IMAGE_LIMITS.searchConcurrency,queries.length)},run));
  return results;
}

/** A small adapter for the pinned Tesseract.js 6 worker protocol. */
export function createOcrWorker({signal,onProgress=()=>{},WorkerClass=globalThis.Worker,
  baseUrl=new URL('/ocr/',globalThis.location?.href||'http://localhost/').href}={}){
  assertActive(signal);
  if(!WorkerClass||typeof WebAssembly==='undefined')throw Error('このブラウザでは画像の読み取りを使えません。候補を入力して検索してください。');
  const worker=new WorkerClass(new URL('worker.min.js',baseUrl).href),pending=new Map();
  let serial=0,closed=false;
  function stop(error=abortError()){
    if(closed)return;closed=true;worker.terminate();signal?.removeEventListener('abort',abort);
    for(const job of pending.values()){clearTimeout(job.timer);job.reject(error);}pending.clear();
  }
  const abort=()=>stop();signal?.addEventListener('abort',abort,{once:true});
  worker.onmessage=event=>{
    if(closed)return;
    const message=event.data||{},job=pending.get(message.jobId);if(!job)return;
    if(message.status==='progress'){onProgress(message.data||{});return;}
    if(!['resolve','reject'].includes(message.status))return;
    clearTimeout(job.timer);pending.delete(message.jobId);
    if(message.status==='resolve')job.resolve(message.data);else job.reject(Error('画像を読み取れませんでした。候補を手入力して検索できます。'));
  };
  worker.onerror=()=>stop(Error('画像の読み取りを開始できませんでした。候補を手入力して検索できます。'));
  function call(action,payload,timeout=60000){
    assertActive(signal);
    if(closed)return Promise.reject(abortError());
    return new Promise((resolve,reject)=>{
      const jobId='image-'+(++serial),timer=setTimeout(()=>stop(Error('画像の読み取りが時間切れになりました。候補を手入力して検索できます。')),timeout);
      pending.set(jobId,{resolve,reject,timer});
      try{worker.postMessage({workerId:'fan-image',jobId,action,payload});}
      catch(error){stop(Error('画像を読み取れませんでした。候補を手入力して検索できます。'));}
    });
  }
  return {async initialize(){
    await call('load',{options:{lstmOnly:true,corePath:new URL('tesseract-core-lstm.wasm.js',baseUrl).href,logging:false}});
    await call('loadLanguage',{langs:'jpn+eng',options:{langPath:baseUrl,cacheMethod:'none',gzip:true,lstmOnly:true}});
    await call('initialize',{langs:'jpn+eng',oem:1,config:{}});
    await call('setParameters',{params:{tessedit_pageseg_mode:'11',preserve_interword_spaces:'1'}});
  },async recognize(image){const result=await call('recognize',{image,options:{},output:{text:true}},90000);return result?.text||'';},terminate:stop};
}

async function imageBytes(file,signal){
  assertActive(signal);
  const url=URL.createObjectURL(file),image=new Image();
  try{
    await new Promise((resolve,reject)=>{
      const abort=()=>{cleanup();image.src='';reject(abortError());};
      const timer=setTimeout(()=>{cleanup();image.src='';reject(Error('画像を開けませんでした。別の画像を選んでください。'));},15000);
      function cleanup(){clearTimeout(timer);signal?.removeEventListener('abort',abort);image.onload=image.onerror=null;}
      image.onload=()=>{cleanup();resolve();};image.onerror=()=>{cleanup();reject(Error('画像を開けませんでした。JPEG・PNG・WebPを選んでください。'));};
      signal?.addEventListener('abort',abort,{once:true});image.src=url;
    });
    assertActive(signal);
    if(!image.naturalWidth||!image.naturalHeight)throw Error('画像を開けませんでした。');
    const scale=Math.min(1,IMAGE_LIMITS.edge/Math.max(image.naturalWidth,image.naturalHeight),Math.sqrt(IMAGE_LIMITS.pixels/(image.naturalWidth*image.naturalHeight)));
    const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.floor(image.naturalWidth*scale));canvas.height=Math.max(1,Math.floor(image.naturalHeight*scale));
    try{
      const context=canvas.getContext('2d');if(!context)throw Error('画像を開けませんでした。');
      context.fillStyle='#fff';context.fillRect(0,0,canvas.width,canvas.height);context.drawImage(image,0,0,canvas.width,canvas.height);
      const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(Error('画像を変換できませんでした。')),'image/png'));
      assertActive(signal);return new Uint8Array(await blob.arrayBuffer());
    }finally{canvas.width=canvas.height=0;}
  }finally{image.src='';URL.revokeObjectURL(url);}
}

export async function recognizeImages(files,{signal,onProgress=()=>{},workerFactory=createOcrWorker,decode=imageBytes}={}){
  validateImageFiles(files);assertActive(signal);
  const ocr=workerFactory({signal,onProgress}),texts=[];
  try{
    await ocr.initialize();
    for(let index=0;index<files.length;index++){
      assertActive(signal);onProgress({status:'image',index:index+1,total:files.length});
      const bytes=await decode(files[index],signal);assertActive(signal);texts.push(await ocr.recognize(bytes));
    }
    return texts.join('\n');
  }finally{ocr.terminate();}
}

function element(tag,text,className){const value=document.createElement(tag);if(text!==undefined)value.textContent=text;if(className)value.className=className;return value;}

export function mountFanImageImport(container,{searchUsers,addFans,ownerId=()=>null,onComplete=()=>{}}){
  if(!container||typeof searchUsers!=='function'||typeof addFans!=='function')throw Error('画像登録の設定が不足しています。');
  const details=element('details',undefined,'fan-image-import'),summary=element('summary','フォロワー画面の画像から追加（補助）');
  details.append(summary,element('p','画像はこの端末内で読み取り、サーバへ送信しません。読み取りには約7.6MBの補助データを使います。装飾文字や同じ名前は取り違えるため、最後に数値IDを確認して選んでください。','hint'));
  const fileLabel=element('label','画像を選択（3枚まで・JPEG / PNG / WebP）'),files=element('input');files.type='file';files.accept='image/jpeg,image/png,image/webp';files.multiple=true;fileLabel.append(files);
  const actions=element('div',undefined,'fan-image-actions'),read=element('button','画像から候補を読み取る'),cancel=element('button','中止');read.type=cancel.type='button';cancel.hidden=true;actions.append(read,cancel);
  const status=element('p','写真が読み取れない場合は、下に名前やプロフィールIDを入力して検索できます。','hint');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const namesLabel=element('label','読み取った候補（1行に1人・編集できます）'),names=element('textarea');names.rows=5;names.maxLength=12000;names.placeholder='しらたまもなか\n@profile_id';namesLabel.append(names);
  const search=element('button','候補を検索');search.type='button';
  const results=element('div',undefined,'fan-image-query-results'),add=element('button','選択した人を確認');add.type='button';add.disabled=true;
  details.append(fileLabel,actions,status,namesLabel,search,results,add);container.replaceChildren(details);
  let request=null,serial=0,destroyed=false,selected=new Map(),checkedOwner=null;
  function currentOwner(){return String((typeof ownerId==='function'?ownerId():ownerId)??'');}
  function active(token){return !destroyed&&token===serial;}
  function selectionChanged(){
    for(const input of results.querySelectorAll('input[data-user-id]')){input.checked=selected.has(input.dataset.userId);input.disabled=Boolean(request);}
    add.textContent=`選択した ${selected.size} 人を確認`;add.disabled=!selected.size||Boolean(request);
  }
  function busy(value){read.disabled=search.disabled=value;cancel.hidden=!value;files.disabled=value;names.disabled=value;selectionChanged();}
  function stop(){serial++;request?.abort();request=null;busy(false);}
  function begin(){stop();request=new AbortController();busy(true);return {token:serial,signal:request.signal};}
  async function end(token){if(active(token)){request=null;busy(false);}}
  function renderResult(row){
    const group=element('section',undefined,'fan-image-query-group');group.append(element('h4',row.query));
    if(row.error||!row.users.length)group.append(element('p',row.error||'候補が見つかりません。上の名前を修正して再検索してください。',row.error?'error':'hint'));
    for(const user of row.users){
      const choice=element('label',undefined,'fan-candidate-choice fan-image-candidate'),check=element('input');check.type='checkbox';check.dataset.userId=user.id;check.checked=selected.has(user.id);check.disabled=Boolean(request);const text=element('div');text.append(element('strong',user.name),element('div',(user.tag?'@'+user.tag+' · ':'')+'ID '+user.id,'sub'));
      check.addEventListener('change',()=>{if(request){check.checked=selected.has(user.id);return;}if(check.checked)selected.set(user.id,user);else selected.delete(user.id);selectionChanged();});choice.append(check,text);group.append(choice);
    }
    if(row.users.length===10)group.append(element('p','候補は上位10人まで表示します。プロフィールIDなどで絞って再検索できます。','hint'));
    results.append(group);
  }
  read.addEventListener('click',async()=>{
    let imageFiles;try{imageFiles=validateImageFiles(files.files);}catch(error){status.textContent=error.message;return;}
    const {token,signal}=begin();status.textContent='読み取りを準備しています…';selected.clear();results.replaceChildren();checkedOwner=null;
    try{
      const text=await recognizeImages(imageFiles,{signal,onProgress:progress=>{
        if(!active(token))return;
        if(progress.status==='image')status.textContent=`画像 ${progress.index} / ${progress.total} を読み取っています…`;
        else if(progress.status==='recognizing text')status.textContent=`画像を読み取っています… ${Math.round((progress.progress||0)*100)}%`;
      }});
      if(!active(token))return;names.value=candidateNames(text).join('\n');
      status.textContent=names.value?'候補を読み取りました。不要な行や読み違いを直して「候補を検索」を押してください。一度に30候補まで検索します。':'名前を読み取れませんでした。画像を拡大して撮り直すか、名前を入力して検索してください。';
    }catch(error){if(active(token)&&error.name!=='AbortError')status.textContent=error.message||'読み取れませんでした。名前を入力して検索できます。';}
    finally{if(active(token))files.value='';await end(token);}
  });
  search.addEventListener('click',async()=>{
    const queries=candidateNames(names.value);if(!queries.length){status.textContent='名前やプロフィールIDを1行に1人ずつ入力してください。';return;}
    const {token,signal}=begin();checkedOwner=currentOwner();selected.clear();results.replaceChildren();status.textContent=`${queries.length} 件の候補を検索しています…`;let count=0;
    try{
      await lookupCandidates(queries,searchUsers,{signal,onResult:row=>{if(active(token)){renderResult(row);status.textContent=`${++count} / ${queries.length} 件を検索しました。`;}}});
      if(active(token))status.textContent='登録する人を選んでください。検索結果は数値IDで区別します。見つからない名前は編集して再検索できます。';
    }catch(error){if(active(token)&&error.name!=='AbortError')status.textContent=error.message;}
    finally{await end(token);}
  });
  cancel.addEventListener('click',()=>{stop();status.textContent='中止しました。候補は編集して検索できます。';});
  add.addEventListener('click',async()=>{
    if(request||!selected.size)return;
    if(!currentOwner()||checkedOwner!==currentOwner()){selected.clear();results.replaceChildren();selectionChanged();status.textContent='対象のプロフィールが変わりました。もう一度検索してください。';return;}
    const token=serial,users=Array.from(selected.values());request=new AbortController();busy(true);cancel.hidden=true;
    try{const result=await addFans(users);if(active(token)&&result!==false){selected.clear();results.replaceChildren();status.textContent='選択した人を追加しました。';selectionChanged();await onComplete();}}
    catch(error){if(active(token)&&error.name!=='AbortError')status.textContent=error.message||'登録できませんでした。選択を確認して再度お試しください。';}
    finally{await end(token);}
  });
  function reset(){stop();selected.clear();checkedOwner=null;files.value=names.value='';results.replaceChildren();status.textContent='画像または名前から候補を検索できます。';selectionChanged();}
  return {reset,destroy(){reset();destroyed=true;container.replaceChildren();}};
}

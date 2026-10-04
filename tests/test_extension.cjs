const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..', 'browser-extension');
const context = { URL, location: {href:'https://player.example.com/play',origin:'https://player.example.com'},
  performance: {getEntriesByType:()=>[{name:'https://cdn.example.com/index.m3u8?token=public'}, {name:'https://cdn.example.com/chunk.ts'}]},
  document: { title:'Fixture', querySelectorAll(selector) {
    if (selector === 'video') return [{currentSrc:'blob:https://player.example.com/id',mediaKeys:null,querySelectorAll:()=>[]}];
    if (selector === 'a[href]') return [{href:'https://cdn.example.com/sample.mp4'}];
    if (selector === 'script:not([src])') return [{textContent:`var Vurl = 'https://vip.ffzyread.com/20231004/17545_73bf4be7/index.m3u8'; var adposter='https://175.178.227.159:7788/Play/adposter.mp4'; evil = "https://example.com/a.js";`}];
    if (selector === 'iframe[src]') return [{src:'https://another.example.com/embed'}];
    return [];
  }} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root,'media.js'),'utf8'), context);
const support = context.MediaSupport;
assert(support.isBilibiliUrl('https://www.bilibili.com/video/BV1'));
assert(!support.isBilibiliUrl('https://bilibili.com.evil.test/'));
assert(!support.isHttpUrl('blob:https://example.com/abc'));
assert(!support.isHttpUrl('https://u:p@example.com/'));
assert.equal(support.mediaKind('https://example.com/a.m3u8?q=1'), 'hls');
let result = support.probePage();
assert.equal(result.candidates.length,3);
assert(result.hasBlob);
assert.equal(result.frameOrigins[0],'https://another.example.com/*');
assert(result.candidates.some(x=>x.url.includes('vip.ffzyread.com') && x.kind==='hls' && x.source==='script'));
assert(!result.candidates.some(x=>x.url.includes('adposter.mp4')), 'Unobserved script MP4 must not become a candidate');
context.document.querySelectorAll = s=>s==='video' ? [{currentSrc:'https://cdn.example.com/video',mediaKeys:{},querySelectorAll:()=>[]}] : [];
result = support.probePage();
assert(result.protectedMedia);
assert(result.candidates.some(x=>x.url==='https://cdn.example.com/video' && x.kind==='direct' && x.source==='video'), 'Actual video without an extension remains discoverable');
const manifest = JSON.parse(fs.readFileSync(path.join(root,'manifest.json')));
assert(manifest.permissions.includes('activeTab') && manifest.permissions.includes('scripting'));
assert(!manifest.host_permissions);
assert(!manifest.permissions.includes('cookies'));
assert.equal(manifest.name,'视频本地下载器');
assert.equal(manifest.action.default_title,'视频本地下载器');
for (const size of ['16','32','48','128']) {
  const iconPath = path.join(root,manifest.icons[size]);
  assert(fs.existsSync(iconPath), `Missing ${size}px extension icon`);
}
const popupHtml = fs.readFileSync(path.join(root,'popup.html'),'utf8');
assert(popupHtml.includes('<h1 id="pageTitle">视频本地下载器</h1>'));
assert(!popupHtml.includes('Edge / Chrome'));
assert(!popupHtml.includes('pageSubtitle'));

async function workerTest(capabilities) {
  const stored = {jobs:[{id:'a',url:'https://example.com/page',status:'等待中'}, {id:'b',url:'https://example.com/page2',status:'等待中'}]};
  const listeners = {};
  const posted = [];
  const port = {onMessage:{addListener(fn){listeners.host=fn;}},onDisconnect:{addListener(fn){listeners.disconnect=fn;}},
    postMessage(msg){posted.push(msg);},disconnect(){}};
  let ctx;
  const chrome = {storage:{local:{async get(){return structuredClone(stored);},async set(value){Object.assign(stored,structuredClone(value));}}},
    runtime:{id:'extension',getURL:path=>`chrome-extension://extension/${path}`,connectNative(){queueMicrotask(()=>listeners.host({type:'host_ready',capabilities}));return port;},
      onMessage:{addListener(fn){listeners.runtime=fn;}},sendMessage:async()=>{}}};
  ctx = vm.createContext({chrome,URL,setTimeout,clearTimeout,importScripts(file){vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),ctx);}});
  vm.runInContext(fs.readFileSync(path.join(root,'service_worker.js'),'utf8'),ctx);
  function send(message){return new Promise(resolve=>listeners.runtime(message,{id:'extension',url:'chrome-extension://extension/popup.html'},resolve));}
  const media = {url:'https://cdn.example.com/stream.m3u8',kind:'hls',referrer:'https://player.example.com/embed'};
  const response = await send({type:'start_queue',jobs:[{id:'a',url:'https://example.com/page',media}],maxRetries:3});
  if (!capabilities.length) {
    assert.equal(response.ok,false);
    assert(response.error.includes('版本过旧'));
    assert.equal(posted.length,0);
    const legacy = await send({type:'start_queue',jobs:[{id:'a',url:'https://www.bilibili.com/video/BV1'}],maxRetries:3});
    assert(legacy.ok);
  } else {
    assert(response.ok);
    assert.equal(posted[0].jobs[0].media.url,media.url);
    assert.equal(posted[0].maxRetries,3);
    // Native callbacks arrive synchronously; serialized storage updates must
    // retain both jobs' status even though each uses asynchronous get/set.
    listeners.host({type:'job_state',id:'a',status:'下载中',attempts:1});
    listeners.host({type:'job_metadata',id:'a',title:'解析后的单集标题'});
    listeners.host({type:'job_success',id:'a',path:'/downloads/a.mp4'});
    listeners.host({type:'job_state',id:'b',status:'失败',attempts:1,error:'HTTP Error 403'});
    listeners.host({type:'queue_done',cancelled:false});
    await vm.runInContext('hostEvents',ctx);
    assert.equal(stored.jobs[0].status,'完成');
    assert.equal(stored.jobs[0].title,'解析后的单集标题');
    assert.equal(stored.jobs[0].outputPath,'/downloads/a.mp4');
    assert.equal(stored.jobs[1].status,'失败');
    assert.equal(stored.jobs[1].error,'HTTP Error 403');
    assert.equal(stored.queueStatus,'idle');
  }
  listeners.disconnect();
  await vm.runInContext('hostEvents',ctx);
  if (capabilities.length) {
    const page = await send({type:'start_queue',jobs:[{id:'a',url:'https://example.com/watch'}],maxRetries:3});
    assert.equal(page.ok,capabilities.includes('page_scan'));
    if (!page.ok) assert(page.error.includes('1.4.0'));
  }
}

async function popupTest() {
  const elements = new Map();
  function element(){return {value:'',style:{},scrollHeight:30,children:[],handlers:{},classList:{toggle(){}},
    addEventListener(event,fn){this.handlers[event]=fn;},replaceChildren(...xs){this.children=xs;},append(...xs){this.children.push(...xs);}};}
  const doc = {getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},createElement:element};
  const stored = {jobs:[{id:'legacy-media',url:'https://www.agedm.io/play/20230189/2/1',status:'失败',attempts:1,error:'HTTP Error 403',
    media:{url:'https://cdn.example.com/stream.m3u8',kind:'hls',referrer:'https://jx.wuzhoupai.com:8443/embed'}}]};
  let granted = false;
  let requested = [];
  let submitted;
  let runtimeReply = {ok:true};
  let scans = 0;
  const active = {id:42,url:'https://www.agedm.io/play/20230189/2/1',title:'示例'};
  const chrome = {tabs:{query:async()=>[active]},storage:{local:{get:async()=>structuredClone(stored),set:async v=>Object.assign(stored,structuredClone(v))},onChanged:{addListener(){}}},
    permissions:{contains:async()=>granted,request:async({origins})=>{requested=origins;granted=true;return true;}},
    scripting:{executeScript:async({target})=>{
      if (!target.allFrames) scans++;
      const top = {frameId:0,result:{candidates:[],frameOrigins:['https://jx.wuzhoupai.com:8443/*'],title:'示例',pageUrl:active.url}};
      return target.allFrames && granted ? [top,{frameId:4,result:{candidates:[{url:'https://cdn.example.com/stream.m3u8',kind:'hls',referrer:'https://jx.wuzhoupai.com:8443/embed',source:'script'}],frameOrigins:[],hasBlob:true}}] : [top];
    }},runtime:{sendMessage:async msg=>{submitted=msg;return runtimeReply;},onMessage:{addListener(){}}}};
  const ctx = vm.createContext({chrome,URL,document:doc,setTimeout,clearTimeout,
    getComputedStyle:()=>({lineHeight:'18.2px',paddingTop:'6px',paddingBottom:'6px',borderTopWidth:'1px',borderBottomWidth:'1px'}),
    crypto:{randomUUID:()=>`job-${Math.random()}`}});
  vm.runInContext(fs.readFileSync(path.join(root,'media.js'),'utf8'),ctx);
  vm.runInContext(fs.readFileSync(path.join(root,'popup.js'),'utf8'),ctx);
  await new Promise(r=>setTimeout(r,0));
  assert.equal(scans,1, 'Opening the popup must scan exactly once');
  assert.equal(doc.getElementById('mediaSection').hidden,true, 'Zero candidates must hide the results');
  assert.equal(requested.length,0, 'Automatic scan must not request permissions');
  doc.getElementById('maxRetries').value='2';
  await doc.getElementById('maxRetries').handlers.change();
  await vm.runInContext('loadState()',ctx);
  assert.equal(stored.maxRetries,2);
  assert.equal(doc.getElementById('maxRetries').value,'2', 'Retry preference survives reloading popup state');
  await doc.getElementById('scanMedia').handlers.click();
  assert.equal(doc.getElementById('grantFrames').hidden,false);
  assert.equal(doc.getElementById('addMedia').disabled,true);
  assert.equal(requested.length,0); // Scan alone must never request permission.
  await doc.getElementById('grantFrames').handlers.click();
  assert.equal(requested.length,1);
  assert.equal(requested[0],'https://jx.wuzhoupai.com:8443/*');
  assert.equal(doc.getElementById('addMedia').disabled,false);
  assert.equal(doc.getElementById('mediaSection').hidden,false);
  doc.getElementById('mediaChoice').value='0';
  await doc.getElementById('addMedia').handlers.click();
  await doc.getElementById('addMedia').handlers.click(); // Deduplicate media URLs.
  assert.equal(stored.jobs.length,1);
  assert.equal(stored.jobs[0].id,'legacy-media');
  assert.equal(stored.jobs[0].error,'');
  assert.equal(stored.jobs[0].status,'等待中');
  assert.equal(stored.jobs[0].url,active.url);
  assert.equal(stored.jobs[0].media.kind,'hls');
  assert.equal(stored.jobs[0].title,doc.getElementById('mediaChoice').children[0].textContent, 'Scan and queue names must be identical');
  await doc.getElementById('startQueue').handlers.click();
  assert.equal(submitted.jobs[0].media.referrer,'https://jx.wuzhoupai.com:8443/embed');
  assert.equal(submitted.maxRetries,2);
  assert.equal(submitted.jobs[0].media.browser.tabId,42);
  assert.equal(submitted.jobs[0].media.browser.frameId,4);

  stored.queueStatus='idle';
  stored.jobs=[
    {id:'done-old',url:'https://example.com/1',status:'完成',outputPath:'/downloads/a.mp4',createdAt:10,attempts:1},
    {id:'wait-old',url:'https://example.com/2',status:'等待中',createdAt:20},
    {id:'done-new',url:'https://example.com/3',status:'完成',outputPath:'/downloads/b.mp4',createdAt:30,attempts:2},
    {id:'wait-new',url:'https://example.com/4',status:'等待中',createdAt:40},
    {id:'active',url:'https://example.com/5',status:'下载中',createdAt:5},
    {id:'failed',url:'https://example.com/6',status:'失败',createdAt:50},
  ];
  await vm.runInContext('loadState()',ctx);
  assert.equal(vm.runInContext('sortedJobs().map(job=>job.id).join(",")',ctx),'active,wait-new,wait-old,failed,done-new,done-old');
  assert.equal(stored.jobs[0].id,'done-old', 'Rendering never mutates queue storage');
  function descendants(node){return [node,...node.children.flatMap(descendants)];}
  const rendered = doc.getElementById('jobs').children.flatMap(descendants);
  assert(!rendered.some(node=>/^\d+ 次$/.test(node.textContent || '')), 'Attempt count is not displayed');
  const play=rendered.find(node=>node.ariaLabel==='播放视频');
  await play.handlers.click({ctrlKey:true});
  assert.equal(submitted.action,'play_video');assert.equal(submitted.jobId,'done-new');assert.equal(submitted.chooseApplication,true);
  await play.handlers.click({ctrlKey:false});
  assert.equal(submitted.chooseApplication,false);
  await doc.getElementById('chooseDirectory').handlers.click();
  assert.equal(submitted.action,'choose_directory');
  await doc.getElementById('openDirectory').handlers.click();
  assert.equal(submitted.action,'open_directory');
  await doc.getElementById('startQueue').handlers.click();
  assert.equal(submitted.jobs.map(job=>job.id).join(','),'wait-new,wait-old', 'Waiting jobs download in visible newest-first order');
  assert(submitted.jobs.every(job=>job.transport==='background'), 'Pasted page links use background media transport');

  stored.queueStatus='idle';
  stored.jobs=[{id:'start-failure',url:'https://example.com/fail',status:'等待中',createdAt:60}];
  runtimeReply={ok:false,error:'Native Host test failure'};
  await vm.runInContext('loadState()',ctx);
  await doc.getElementById('startQueue').handlers.click();
  assert.equal(stored.jobs[0].status,'失败', 'Start failures must not leave a job waiting');
  assert.equal(stored.jobs[0].error,'Native Host test failure');
}

async function desktopWorkerTest() {
  const listeners={},posted=[];
  const stored={jobs:[{id:'done',status:'完成',outputPath:'/downloads/video.mp4'},{id:'waiting',status:'等待中'}]};
  const port={onMessage:{addListener:fn=>listeners.host=fn},onDisconnect:{addListener:fn=>listeners.disconnect=fn},
    postMessage(message){
      posted.push(message);
      queueMicrotask(()=>listeners.host({type:'desktop_result',requestId:message.requestId,ok:true,
        ...(message.action==='get_directory' ? {outputDirectory:'/downloads'} : {})}));
    },disconnect(){}};
  const runtime={id:'extension',getURL:path=>`chrome-extension://extension/${path}`,
    connectNative(){queueMicrotask(()=>listeners.host({type:'host_ready',capabilities:['desktop_actions']}));return port;},
    onMessage:{addListener:fn=>listeners.runtime=fn},sendMessage:async()=>{}};
  const chrome={runtime,storage:{local:{get:async()=>structuredClone(stored),set:async values=>Object.assign(stored,structuredClone(values))}}};
  let ctx;
  ctx=vm.createContext({chrome,URL,setTimeout,clearTimeout,crypto:require('node:crypto').webcrypto,
    importScripts(file){vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),ctx);}});
  vm.runInContext(fs.readFileSync(path.join(root,'service_worker.js'),'utf8'),ctx);
  const sender={id:runtime.id,url:runtime.getURL('popup.html')};
  const send=(message,source=sender)=>new Promise(resolve=>listeners.runtime({type:'desktop_action',...message},source,resolve));
  assert.equal((await send({action:'play_video',jobId:'done'},{id:runtime.id,url:'https://example.com'})).ok,false);
  assert.equal(posted.length,0);
  assert.equal((await send({action:'play_video',jobId:'waiting'})).ok,false);
  assert.equal(posted.length,0);
  assert.equal((await send({action:'get_directory'})).ok,true);
  assert.equal(stored.downloadDirectory,'/downloads');
  assert.equal((await send({action:'play_video',jobId:'done',path:'/malicious/ignored.exe',chooseApplication:true})).ok,true);
  assert.equal(posted.at(-1).path,'/downloads/video.mp4');
  assert.equal(posted.at(-1).chooseApplication,true);
  assert.equal(vm.runInContext('desktopRequests.size',ctx),0);
  listeners.disconnect();await vm.runInContext('hostEvents',ctx);
}

async function browserFetchTest() {
  const sent = [];
  const listeners = new Set();
  let fetchOptions;
  let activeSends = 0;
  const ctx = vm.createContext({location:{href:'https://player.example.com/embed'}, AbortController, setTimeout,clearTimeout,
    btoa:value=>Buffer.from(value,'binary').toString('base64'),
    fetch:async(url,options)=>{
      fetchOptions=options;
      const response=new Response(new Uint8Array(180000).fill(42),{headers:{'Content-Type':'video/mp4','Content-Length':'180000'}});
      Object.defineProperty(response,'url',{value:url});
      return response;
    },chrome:{runtime:{onMessage:{addListener:fn=>listeners.add(fn),removeListener:fn=>listeners.delete(fn)},sendMessage:async message=>{
      assert.equal(activeSends++,0,'Backpressure must allow only one unacknowledged chunk');
      sent.push(message);
      await new Promise(r=>setTimeout(r,1));
      activeSends--;
      return {ok:true};
    }}}});
  vm.runInContext(fs.readFileSync(path.join(root,'browser_fetch.js'),'utf8'),ctx);
  await ctx.fetchBrowserResource('request','https://cdn.example.com/file.mp4','GET','bytes=0-','https://player.example.com/embed');
  assert.equal(fetchOptions.credentials,'omit');
  assert.equal(fetchOptions.headers.Range,'bytes=0-');
  assert.equal(Object.keys(fetchOptions.headers).length,1);
  assert.equal(sent[0].phase,'headers');
  assert.equal(sent.at(-1).phase,'end');
  const data=Buffer.concat(sent.filter(x=>x.phase==='data').map(x=>Buffer.from(x.data,'base64')));
  assert.equal(data.length,180000);
  assert(data.every(x=>x===42));
  sent.forEach((x,i)=>assert.equal(x.seq,i));
  assert.equal(listeners.size,0);
  sent.length=0;
  fetchOptions=null;
  await ctx.fetchBrowserResource('changed','https://cdn.example.com/file.mp4','GET','','https://player.example.com/old');
  assert.equal(fetchOptions,null);
  assert.equal(sent[0].phase,'error');
  assert.equal(listeners.size,0);
}

async function workerBrowserRelayTest() {
  let ctx;
  const listeners = {};
  const posted = [];
  const frameListeners = new Set();
  const job={id:'browser-job',url:'https://example.com/watch',media:{url:'https://cdn.example.com/master.m3u8',kind:'hls',
    browser:{tabId:11,frameId:3,frameUrl:'https://player.example.com/embed'}}};
  const port={onMessage:{addListener:fn=>listeners.host=fn},onDisconnect:{addListener:fn=>listeners.disconnect=fn},
    postMessage(message){posted.push(message);if(message.type==='browser_response')queueMicrotask(()=>listeners.host({type:'browser_ack',requestId:message.requestId,seq:message.seq,ok:true}));},disconnect(){}};
  const chrome={storage:{local:{get:async()=>({jobs:[job]}),set:async()=>{}}},runtime:{id:'extension',getURL:path=>`chrome-extension://extension/${path}`,
    connectNative(){queueMicrotask(()=>listeners.host({type:'host_ready',capabilities:['public_web_media','browser_http']}));return port;},
    onMessage:{addListener:fn=>listeners.runtime=fn},sendMessage:async()=>{}},
    tabs:{get:async()=>({url:job.url}),sendMessage:async(_id,message)=>{for(const fn of frameListeners)fn(message);}},
    scripting:{executeScript:async({target,args})=>{
      assert.equal(target.tabId,11);assert.equal(target.frameIds[0],3);
      const frameCtx=vm.createContext({location:{href:job.media.browser.frameUrl},AbortController,setTimeout,clearTimeout,
        btoa:s=>Buffer.from(s,'binary').toString('base64'),
        fetch:async(url,options)=>{assert.equal(options.credentials,'omit');const response=new Response(new Uint8Array(140000).fill(7));Object.defineProperty(response,'url',{value:url});return response;},
        chrome:{runtime:{onMessage:{addListener:fn=>frameListeners.add(fn),removeListener:fn=>frameListeners.delete(fn)},
          sendMessage:message=>new Promise(resolve=>listeners.runtime(message,{tab:{id:11},frameId:3,url:job.media.browser.frameUrl},resolve))}}});
      vm.runInContext(fs.readFileSync(path.join(root,'browser_fetch.js'),'utf8'),frameCtx);
      await frameCtx.fetchBrowserResource(...args);
    }}};
  ctx=vm.createContext({chrome,URL,setTimeout,clearTimeout,importScripts(file){vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),ctx);}});
  vm.runInContext(fs.readFileSync(path.join(root,'service_worker.js'),'utf8'),ctx);
  const started=await new Promise(resolve=>listeners.runtime({type:'start_queue',jobs:[job],maxRetries:3},{id:'extension',url:'chrome-extension://extension/popup.html'},resolve));
  assert(started.ok);
  listeners.host({type:'browser_request',requestId:'req',jobId:job.id,url:job.media.url,method:'GET'});
  await new Promise(r=>setTimeout(r,10));
  const messages=posted.filter(x=>x.type==='browser_response');
  assert.equal(messages[0].phase,'headers');assert.equal(messages.at(-1).phase,'end');
  assert.equal(Buffer.concat(messages.filter(x=>x.phase==='data').map(x=>Buffer.from(x.data,'base64'))).length,140000);
  messages.forEach((m,i)=>assert.equal(m.seq,i));
  const invalid=await new Promise(resolve=>listeners.runtime({type:'browser_response',requestId:'req',seq:42,phase:'data',data:'YQ=='},{tab:{id:12},frameId:3,url:job.media.browser.frameUrl},resolve));
  assert.equal(invalid.ok,false);
  assert.equal(frameListeners.size,0);
  assert.equal(vm.runInContext('browserRequests.size',ctx),0);
  listeners.host({type:'queue_done'});await vm.runInContext('hostEvents',ctx);
  listeners.disconnect();await vm.runInContext('hostEvents',ctx);
}

async function workerBackgroundRelayTest() {
  let ctx;
  const listeners={},posted=[],stored={jobs:[{id:'background',url:'https://example.com/watch',transport:'background',status:'等待中'}]};
  const job=stored.jobs[0];
  const port={onMessage:{addListener:fn=>listeners.host=fn},onDisconnect:{addListener:fn=>listeners.disconnect=fn},
    postMessage(message){posted.push(message);if(message.type==='browser_response')queueMicrotask(()=>listeners.host({type:'browser_ack',requestId:message.requestId,seq:message.seq,ok:true}));},disconnect(){}};
  const chrome={storage:{local:{get:async()=>structuredClone(stored),set:async values=>Object.assign(stored,structuredClone(values))}},
    permissions:{contains:async()=>false},runtime:{id:'extension',getURL:path=>`chrome-extension://extension/${path}`,
      connectNative(){queueMicrotask(()=>listeners.host({type:'host_ready',capabilities:['public_web_media','page_scan','background_http']}));return port;},
      onMessage:{addListener:fn=>listeners.runtime=fn},sendMessage:async()=>{}}};
  ctx=vm.createContext({chrome,URL,setTimeout,clearTimeout,AbortController,btoa:s=>Buffer.from(s,'binary').toString('base64'),
    fetch:async(url,options)=>{assert.equal(options.credentials,'omit');const r=new Response(new Uint8Array(145123).fill(3));Object.defineProperty(r,'url',{value:url});return r;},
    importScripts(file){vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),ctx);}});
  vm.runInContext(fs.readFileSync(path.join(root,'service_worker.js'),'utf8'),ctx);
  const source={id:'extension',url:'chrome-extension://extension/popup.html'};
  const invalid=await new Promise(resolve=>listeners.runtime({type:'start_queue',jobs:[job]},{id:'extension',url:'https://example.com/watch'},resolve));
  assert.equal(invalid.ok,false);
  const started=await new Promise(resolve=>listeners.runtime({type:'start_queue',jobs:[job],maxRetries:3},source,resolve));
  assert(started.ok);
  listeners.host({type:'browser_request',requestId:'background-req',jobId:job.id,url:'https://cdn.example.com/video.mp4',method:'GET'});
  await new Promise(r=>setTimeout(r,10));
  const responses=posted.filter(message=>message.type==='browser_response');
  assert.equal(responses[0].phase,'headers');assert.equal(responses.at(-1).phase,'end');
  assert.equal(Buffer.concat(responses.filter(x=>x.phase==='data').map(x=>Buffer.from(x.data,'base64'))).length,145123);
  assert.equal(vm.runInContext('browserRequests.size',ctx),0);
  listeners.host({type:'job_state',id:job.id,status:'完成',attempts:1});
  await vm.runInContext('hostEvents',ctx);
  stored.jobs.push({id:'added-during-download',url:'https://example.com/next',status:'等待中',attempts:0});
  listeners.host({type:'queue_done'});await vm.runInContext('hostEvents',ctx);
  await new Promise(r=>setTimeout(r,10));
  const starts=posted.filter(message=>message.type==='start_queue');
  assert.equal(starts.length,2, 'A queue completion must continue links added during the prior download');
  assert.equal(starts[1].jobs.length,1);
  assert.equal(starts[1].jobs[0].id,'added-during-download');
  assert.equal(starts[1].jobs[0].transport,'background');
  listeners.disconnect();await vm.runInContext('hostEvents',ctx);
}

(async()=>{
  await workerTest([]);
  await workerTest(['public_web_media']);
  await workerTest(['public_web_media','page_scan']);
  await popupTest();
  await browserFetchTest();
  await workerBrowserRelayTest();
  await workerBackgroundRelayTest();
  await desktopWorkerTest();
  console.log('Extension probe, popup, Host compatibility, anonymous streaming, backpressure and permissions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});

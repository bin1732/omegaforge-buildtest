const Module=require('module'),path=require('path');
const {JSDOM}=require('/usr/local/lib/node_modules/jsdom');
require.extensions['.css']=()=>{};
const origResolve=Module._resolveFilename;
Module._resolveFilename=function(req,parent,...rest){
  if(req.startsWith('@/')) return origResolve.call(this,path.join('/tmp/ssrout/src',req.slice(2)),parent,...rest);
  try{return origResolve.call(this,req,parent,...rest);}catch(e){
    if(req.startsWith('.')||req.startsWith('/')) throw e;
    return origResolve.call(this,req,{paths:['/usr/local/lib/node_modules']},...rest);}
};
const dom=new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',{url:'http://127.0.0.1:8787/',pretendToBeVisual:true});
global.window=dom.window;global.document=dom.window.document;global.navigator=dom.window.navigator;
global.HTMLElement=dom.window.HTMLElement;global.Node=dom.window.Node;global.Event=dom.window.Event;
global.requestAnimationFrame=cb=>setTimeout(cb,0);global.cancelAnimationFrame=clearTimeout;
global.IS_REACT_ACT_ENVIRONMENT=true;global.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
const React=require('/usr/local/lib/node_modules/react');
const {createRoot}=require('/usr/local/lib/node_modules/react-dom/client');
const {act}=React;
const RUNS={runs:[{id:'run-abc123',status:'done',created_ts:1789912808,seq:12,title:'蒸馏：如何写好提示词',mode:'distill'}],total:1};
global.fetch=async(u)=>{const s=String(u);const b=s.includes('/api/runs')?RUNS:(s.includes('/api/status')?{ok:true}:{});
  return {ok:true,status:200,json:async()=>b,text:async()=>JSON.stringify(b)};};
(async()=>{
  const mod=require('/tmp/ssrout/src/pages/RunsPage.js');
  const RunsPage=mod.RunsPage||mod.default;
  const root=createRoot(document.getElementById('root'));
  await act(async()=>{root.render(React.createElement(RunsPage));});
  await act(async()=>{await new Promise(r=>setTimeout(r,400));});
  const txt=document.body.textContent||'';
  console.log('=== 渲染文本(前500字) ===');console.log(txt.slice(0,500));
  console.log('=== 断言 ===');
  console.log('含任务标题:',txt.includes('蒸馏：如何写好提示词'));
  console.log('DOM节点数:',document.querySelectorAll('*').length);
})().catch(e=>{console.error('RENDER-FAIL:',e.message);console.error((e.stack||'').split('\n').slice(1,5).join('\n'));process.exit(1)});

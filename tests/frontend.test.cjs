const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness() {
  const requests = [];
  const node = {textContent:'', hidden:false, classList:{toggle(){}}, setAttribute(){}};
  const context = vm.createContext({
    document:{querySelector:()=>node, querySelectorAll:()=>[], addEventListener(){}},
    location:{hash:'#test'}, AbortSignal,
    fetch:(url, options)=>new Promise(resolve=>requests.push({url,options,resolve})),
    setTimeout(){}, setInterval(){}, clearTimeout(){},
  });
  const source = fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8');
  vm.runInContext(source, context); // 首次自动轮询挂起，后续测试控制返回顺序。
  vm.runInContext('render = () => {}; renderAsks = () => {};', context);
  return {context,requests};
}
const settle = ()=>new Promise(resolve=>setImmediate(resolve));
function respond(request, data) { request.resolve({ok:true,json:async()=>data}); }
const state = (version,status='pending')=>({version,revision:1,status,question:{},answers:[],asks:[],deadline:Date.now()/1000+600});

test('慢请求期间普通刷新复用请求，强制刷新等待旧请求结束', async()=>{
  const {context,requests} = harness();
  const first = vm.runInContext('refresh()',context);
  assert.equal(first,vm.runInContext('refresh()',context));
  const forced = vm.runInContext('refresh(true)',context);
  assert.equal(requests.length,1);
  respond(requests[0],state(1));
  await settle();
  assert.equal(requests.length,2);
  assert.equal(requests[1].url,'/api/state?since=1');
  respond(requests[1],state(2,'answered'));
  await forced;
  assert.equal(vm.runInContext('current.status',context),'answered');
  assert.ok(requests[0].options.signal);
});

test('未变更响应保留题目，只同步服务端截止时间',async()=>{
  const {context,requests} = harness();
  respond(requests[0],state(5));
  await settle();
  const refresh = vm.runInContext('refresh()',context);
  respond(requests[1],{unchanged:true,version:5,deadline:Date.now()/1000+700});
  await refresh;
  assert.equal(vm.runInContext('current.version',context),5);
  assert.equal(vm.runInContext('current.status',context),'pending');
  assert.ok(vm.runInContext('current.question',context));
});

// 模拟服务重启，验证自动更新会话、有限重试和调研隔离；不访问真实服务。
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('web_ui/app.js','utf8').replace(/init\(\);\s*$/,'');
async function scenario(responses){
  const calls=[];
  const context=vm.createContext({fetch:async(path,options)=>{
    calls.push({path,options});
    const [status,body]=responses.shift();
    return {status,ok:status===200,json:async()=>body};
  }});
  vm.runInContext(source,context);
  vm.runInContext('token="old";studyState={active_id:"study-a"}',context);
  return {context,calls};
}
(async()=>{
  let env=await scenario([[403,{error:'expired'}],[200,{token:'new',studies:{active_id:'study-a'}}],[200,{done:true}]]);
  assert.equal((await vm.runInContext('api("/api/run",{action:"build-bridge"})',env.context)).done,true);
  assert.equal(env.calls[2].options.headers['X-UI-Token'],'new');
  assert.equal(env.calls.length,3);
  env=await scenario([[403,{}],[200,{token:'new',studies:{active_id:'study-b'}}]]);
  await assert.rejects(vm.runInContext('api("/api/run",{})',env.context),/调研已变化/);
  assert.equal(env.calls.length,2);
  env=await scenario([[403,{}],[200,{token:'new',studies:{active_id:'study-a'}}],[403,{error:'origin rejected'}]]);
  await assert.rejects(vm.runInContext('api("/api/run",{})',env.context),/origin rejected/);
  assert.equal(env.calls.length,3);
  env=await scenario([[200,{token:'new'}]]);
  await vm.runInContext('api("/api/state")',env.context);
  assert.equal(vm.runInContext('token',env.context),'new');
  console.log('会话自动恢复检查通过');
})().catch(error=>{console.error(error);process.exitCode=1;});

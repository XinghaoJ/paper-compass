const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
// 密钥格式推测只在本地进行；普通表单提交不会附带尚未应用的密钥。
const source = fs.readFileSync('web_ui/app.js','utf8').replace(/init\(\);\s*$/, '');
const key={value:'',dataset:{}}, provider={value:''}, hint={textContent:''};
const context=vm.createContext({document:{querySelectorAll:()=>[],
  querySelector:selector=>({'#shared-model-key':key,'#shared-model-provider':provider,'#provider-hint':hint}[selector]||{})}});
vm.runInContext(source,context);
for(const [value,expected] of [['AIza-fake','gemini'],['sk-proj-fake','openai'],['sk-svcacct-fake','openai'],['sk-fake',''],['custom','']]){
  context.keyValue=value;
  assert.equal(vm.runInContext('inferKeyProvider(keyValue)',context),expected);
}
key.value='sk-ambiguous';provider.value='gemini';
vm.runInContext('onSharedKeyInput()',context);
assert.equal(provider.value,'');
provider.value='deepseek';key.value='sk-proj-fake';
vm.runInContext('providerManuallySelected=true;onSharedKeyInput()',context);
assert.equal(provider.value,'deepseek');
assert.equal(vm.runInContext('collectSettings().models',context),undefined);
assert.equal(vm.runInContext('modelDraftChanged',context),true);
console.log('共用模型表单检查通过');

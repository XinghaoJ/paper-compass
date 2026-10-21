/* 本地界面：表单配置、后台任务轮询与引用展示，不将 API Key 写入浏览器存储。 */
"use strict";
let token = "";
let currentJob = null;
let renderedResult = null;
let busyRequest = false;
let toastTimer;
let manualDestination = "";
let manualDirectoryField = "";
let manualRefreshJob = null;
let manualItems = [];
let wizardStep = 0;
let modelConnection = {};
let modelDraftChanged = false;
let modelRequestBusy = false;
let providerManuallySelected = false;
let connectionLost = false;
let studyState = null;
let studyRevision = 0;
let cleanupPlan = null;
const wizardLabels = ["研究条件","开始检索","Zotero 插件","文献下载"];
const labels = {search:"检索与摘要筛选",download:"Zotero 全文下载",index:"建立文献知识库",ask:"知识库问答","zotero-check":"Zotero 连接检查","build-bridge":"打包 Zotero 插件"};

// 创建元素时只插入文本，文献和模型输出不作为 HTML 执行。
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// 给出操作反馈，错误也在任务区域保留，不依赖短暂提示。
function toast(message, error=false) {
  const box = document.querySelector("#toast");
  box.textContent = message;
  box.className = "toast" + (error ? " error" : "");
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden=true; }, error ? 6500 : 3200);
}

// 统一请求本机接口，所有写操作附带当前会话令牌。
async function api(path, data, retrySession=true) {
  const response = await fetch(path, data === undefined ? {cache:"no-store"} : {
    method:"POST",headers:{"Content-Type":"application/json","X-UI-Token":token},body:JSON.stringify({...data,study_id:studyState?.active_id})
  });
  const result = await response.json();
  // 服务重启会更换令牌；仅在写操作被明确拒绝时恢复会话并重试一次。
  if(response.status===403 && data!==undefined && retrySession){
    const state=await api("/api/state");
    if(studyState?.active_id && state.studies?.active_id!==studyState.active_id)
      throw new Error("当前调研已变化，请点击“刷新界面”后再操作");
    return api(path,data,false);
  }
  if (!response.ok) throw new Error(result.error || "本机服务请求失败");
  if(path==="/api/state" && result.token)token=result.token;
  return result;
}

// 切换主要页面，后台任务继续运行。
function showTab(tab) {
  document.querySelectorAll(".tab-panel").forEach(node => {node.hidden = node.id !== `tab-${tab}`;});
  document.querySelectorAll("[data-tab]").forEach(node => node.classList.toggle("active",node.dataset.tab===tab));
}

// 用左右箭头逐步填写条件；只检查当前卡片，切换不会启动检索或清空输入。
function advanceWizard(direction) {
  const steps=Array.from(document.querySelectorAll(".wizard-step"));
  if(direction>0){
    const current=steps[wizardStep];
    if(wizardStep===0){
      const start=current.querySelector('[data-path="research.year_start"]');
      const end=current.querySelector('[data-path="research.year_end"]');
      end.setCustomValidity(Number(start.value)>Number(end.value)?"结束年份不能早于起始年份":"");
    }
    for(const input of current.querySelectorAll("input,textarea")){
      if(input.required && !input.value.trim())input.setCustomValidity("请填写此项");
      else if(input.dataset.path!=="research.year_end")input.setCustomValidity("");
      if(!input.reportValidity())return;
    }
  }
  const next=Math.max(0,Math.min(steps.length-1,wizardStep+direction));
  if(next===wizardStep)return;
  wizardStep=next;
  for(const [index,step] of steps.entries()){
    step.hidden=index!==next;
    step.classList.toggle("slide-back",direction<0);
  }
  document.querySelector("#wizard-prev").disabled=next===0;
  document.querySelector("#wizard-next").disabled=next===steps.length-1;
  document.querySelector("#wizard-next-label").textContent=next<steps.length-1?"下一步："+wizardLabels[next+1]:"下载未完成？前往文献补充";
  steps[next].querySelector("h1").focus({preventScroll:true});
}

// 创建带标签的路径输入框，并映射到后端配置。
function field(parent, title, path, {help=""}={}) {
  const group = element("div", "path-field");
  const label = element("label", "", title);
  const input=element("input");input.type="text";
  input.dataset.path=path;input.id="path-"+path.replaceAll(".","-");label.htmlFor=input.id;
  const row=element("div","path-input-row");
  const browse=element("button","button secondary","浏览");browse.type="button";
  browse.setAttribute("aria-label","浏览"+title);
  browse.addEventListener("click",()=>browsePath(input));
  row.append(input,browse);group.append(label,row);
  if(help)group.append(element("span","field-help",help));
  parent.append(group);
  return input;
}

// 打开本机文件夹或报告保存位置选择窗口；取消不修改输入，选中后由“应用路径”保存。
async function browsePath(input, field=input.dataset.path) {
  const buttons=document.querySelectorAll(".path-input-row button");
  buttons.forEach(button=>{button.disabled=true;});
  try{
    const result=await api("/api/browse-path",{field,current:input.value.trim()});
    if(result.path){input.value=result.path;toast(field==="study.directory"?"已选择新调研保存位置。":"已选择路径，点击“应用路径”保存。");}
  }catch(error){toast(error.message,true);}
  finally{buttons.forEach(button=>{button.disabled=false;});}
}

// 项目选择只显示名称，模型密钥与 Zotero 连接继续由当前服务共用。
function renderStudies(studies) {
  if(!studies)return;
  studyState=studies;
  const select=document.querySelector("#study-select");select.replaceChildren();
  for(const item of studies.items){const option=element("option","",item.name);option.value=item.id;select.append(option);}
  select.value=studies.active_id;
}

// 切换后清除上一调研的回答、导入映射和日志，再恢复目标调研自己的任务结果。
async function applyStudyState(state, refresh=true) {
  studyRevision++;
  fillForm(state.settings);renderStudies(state.studies);
  renderedResult=null;manualRefreshJob=null;manualItems=[];manualDestination="";
  document.querySelector("#task-log").textContent="点击按钮开始，任务进度和结果会显示在这里。";
  const imports=document.querySelector("#import-results");imports.textContent="";imports.hidden=true;
  const mapping=document.querySelector("#missing-mapping");mapping.replaceChildren();mapping.hidden=true;
  document.querySelector("#manual-list").replaceChildren();
  renderAnswerState("回答与引用","提问后，回答和对应原文会显示在这里。");
  renderJob(state.job);
  if(wizardStep)advanceWizard(-wizardStep);
  showTab("workflow");
  document.querySelector("#notice").hidden=true;
  if(refresh){try{await refreshManual();}catch(error){toast("调研已切换，补充清单读取失败："+error.message,true);}}
  else document.querySelector("#manual-count").textContent="0 篇待补充";
}

// 先展示实际文件范围，确认前不清理；运行中的任务与调研切换共用互斥保护。
async function previewStudyCleanup() {
  if(busyRequest||modelRequestBusy||currentJob?.status==="running")return;
  busyRequest=true;renderJob(currentJob);
  try{
    cleanupPlan=await api("/api/studies/cleanup-preview",{settings:collectSettings()});
    document.querySelector("#cleanup-summary").textContent=`${cleanupPlan.name}：${cleanupPlan.files} 个文件，约 ${(cleanupPlan.bytes/1024/1024).toFixed(1)} MB`;
    document.querySelector("#cleanup-paths").textContent=cleanupPlan.paths.join("\n")||"没有需要清理的文件。";
    document.querySelector("#cleanup-name").value="";
    document.querySelector("#confirm-cleanup").disabled=true;
    document.querySelector("#cleanup-dialog").showModal();
  }catch(error){toast(error.message,true);}
  finally{busyRequest=false;renderJob(currentJob);}
}

// 用户输入名称确认后发送一次清理请求，移除过期回答和清单，不立即生成新的空报告。
async function confirmStudyCleanup(event) {
  event.preventDefault();
  if(busyRequest||!cleanupPlan||document.querySelector("#cleanup-name").value!==cleanupPlan.name)return;
  busyRequest=true;renderJob(currentJob);
  document.querySelector("#confirm-cleanup").disabled=true;
  try{
    const result=await api("/api/studies/cleanup",{token:cleanupPlan.token,name:cleanupPlan.name});
    document.querySelector("#cleanup-dialog").close();cleanupPlan=null;
    await applyStudyState(result.state,false);
    toast(`已将 ${result.cleared} 个文件移至回收站。`);
  }catch(error){document.querySelector("#cleanup-dialog").close();cleanupPlan=null;toast(error.message,true);}
  finally{busyRequest=false;renderJob(currentJob);}
}

// 先把当前表单保存进原调研，再让服务原子切换配置，避免丢失尚未点击应用的研究条件。
async function changeStudy(create=false) {
  if(busyRequest||modelRequestBusy||currentJob?.status==="running")return;
  const select=document.querySelector("#study-select");
  busyRequest=true;renderJob(currentJob);
  try{
    const data={settings:collectSettings()};
    if(create){data.name=document.querySelector("#study-name").value.trim();data.directory=document.querySelector("#study-directory").value.trim();}
    else data.id=select.value;
    const state=await api(create?"/api/studies/create":"/api/studies/switch",data);
    document.querySelector("#study-dialog").close();
    await applyStudyState(state);
    toast(create?"调研已创建，请修改关键词与关注问题后开始检索。":"已切换调研，原调研条件已保存。");
  }catch(error){if(studyState)select.value=studyState.active_id;toast(error.message,true);}
  finally{busyRequest=false;renderJob(currentJob);}
}

// 筛选和问答只分别选择版本，共用上方的接口类型与密钥。
function buildModelFields() {
  const container=document.querySelector("#model-settings");
  for(const [key,title] of Object.entries({screening:"文献筛选模型",qa:"问答模型"})) {
    const label=element("label","",title);
    const select=element("select");select.dataset.modelRole=key;select.disabled=true;
    const option=element("option","","应用密钥后选择模型版本");option.value="";
    select.append(option);label.append(select);container.append(label);
    select.addEventListener("change",saveModelSelection);
  }
}

// 密钥前缀仅作本地提示，不向多个服务商发送密钥来试探归属。
function inferKeyProvider(key) {
  if(/^AIza/.test(key))return "gemini";
  if(/^sk-(proj|svcacct)-/.test(key))return "openai";
  return "";
}

// 修改连接后暂停版本选择，避免将旧接口的模型混入新接口。
function invalidateModelConnection() {
  modelDraftChanged=true;
  const key=document.querySelector("#shared-model-key");
  const sameProvider=document.querySelector("#shared-model-provider").value===modelConnection.provider;
  key.dataset.keyConfigured=String(sameProvider&&Boolean(modelConnection.has_api_key));
  key.placeholder=sameProvider&&modelConnection.has_api_key?"************（已配置，输入新密钥可替换）":"输入所选服务商的 API Key";
  document.querySelectorAll("[data-model-role]").forEach(select=>{
    select.replaceChildren();const option=element("option","","请先应用密钥");option.value="";
    select.append(option);select.disabled=true;
  });
  document.querySelector("#key-status").textContent="连接待应用";
  document.querySelector("#model-catalog-status").textContent="应用成功后，会更新两个模型版本下拉列表。";
}

// 在本地推测服务商；用户已手选时保留选择，歧义密钥要求手动选择。
function onSharedKeyInput() {
  const key=document.querySelector("#shared-model-key").value.trim();
  const provider=document.querySelector("#shared-model-provider");
  const inferred=inferKeyProvider(key);
  if(!providerManuallySelected)provider.value=key?inferred:(modelConnection.provider||"");
  document.querySelector("#provider-hint").textContent=inferred
    ?"已根据密钥格式提示接口类型，请确认；仅向所选服务商发送请求。"
    :"此密钥格式无法可靠区分服务商，请确认接口类型。";
  invalidateModelConnection();
}

// 模型连接请求期间禁止重复提交和改选，防止旧响应覆盖新选择。
function setModelBusy(busy) {
  modelRequestBusy=busy;
  document.querySelector("#apply-key").disabled=busy;
  document.querySelector("#shared-model-key").disabled=busy;
  document.querySelector("#shared-model-provider").disabled=busy;
  document.querySelectorAll("[data-model-role]").forEach(select=>{select.disabled=busy||modelDraftChanged||!modelConnection.models?.length;});
  renderJob(currentJob);
}

// 获取所选服务商的在线列表并应用共用连接，不发送文本生成请求。
async function applySharedModel() {
  if(modelRequestBusy)return;
  setModelBusy(true);
  const status=document.querySelector("#model-catalog-status");
  status.textContent="正在连接所选服务商，获取模型版本…";
  try{
    const provider=document.querySelector("#shared-model-provider").value;
    if(!provider)throw new Error("无法自动判断服务商，请先选择接口类型。");
    const result=await api("/api/model-connect",{provider,api_key:document.querySelector("#shared-model-key").value.trim(),settings:collectSettings()});
    refreshKeyStatus(result.connection);
    toast("共用密钥已应用，可分别选择筛选和问答模型。");
  }catch(error){status.textContent=error.message+"；原配置未修改。";toast(error.message,true);}
  finally{setModelBusy(false);}
}

// 两个版本选择立即保存；后端再次检查是否来自当前连接返回的列表。
async function saveModelSelection() {
  if(modelRequestBusy||modelDraftChanged)return;
  setModelBusy(true);
  try{
    const selection={};document.querySelectorAll("[data-model-role]").forEach(select=>{selection[select.dataset.modelRole]=select.value;});
    const result=await api("/api/model-selection",{selection});
    refreshKeyStatus(result.connection);
    document.querySelector("#model-catalog-status").textContent="模型版本已保存；筛选和问答共用同一密钥。";
  }catch(error){
    document.querySelectorAll("[data-model-role]").forEach(select=>{select.value=modelConnection.selection[select.dataset.modelRole];});
    document.querySelector("#model-catalog-status").textContent="保存失败："+error.message;toast(error.message,true);
  }finally{setModelBusy(false);}
}

// 路径单独成页；内部超时和向量配置沿用已有设置，不在界面重复展示。
function buildOtherFields() {
  const paths=document.querySelector("#path-settings");
  [["命中文献下载路径","download.output_dir"],["文献筛选报告路径","research.output_file"],
   ["问答报告路径","knowledge.answer_dir"]]
    .forEach(([title,path])=>field(paths,title,path));
}

// 读取嵌套配置字段，路径不存在时返回空白。
function getPath(data,path) {return path.split(".").reduce((value,key)=>value?.[key],data);}

// 写入嵌套字段，供独立配置对象直接映射后端数据类。
function setPath(data,path,value) {
  const keys=path.split(".");let object=data;
  keys.slice(0,-1).forEach(key => {object=object[key]??=( {} );});
  object[keys.at(-1)]=value;
}

// 从后端填充表单，密钥仅显示“已配置”，从不读取出真实值。
function fillForm(settings) {
  document.querySelectorAll("[data-path]").forEach(input => {
    const value=getPath(settings,input.dataset.path);
    if(input.type==="checkbox")input.checked=Boolean(value);
    else input.value=value??"";
  });
}

// 用星号占位区分已配置与未配置，不把密钥或假的星号值放回输入框。
function refreshKeyStatus(connection) {
  if(!connection)return;
  modelConnection=connection;modelDraftChanged=false;providerManuallySelected=false;
  const input=document.querySelector("#shared-model-key");
  input.value="";input.dataset.keyConfigured=String(Boolean(connection.has_api_key));
  input.placeholder=connection.has_api_key?"************（已配置，输入新密钥可替换）":"输入 API Key";
  document.querySelector("#shared-model-provider").value=connection.provider||"";
  const models=connection.models||[];
  if(models.length)document.querySelector("#provider-hint").textContent="筛选和问答共用此接口；需要更新模型列表时，再次点击“应用密钥”。";
  document.querySelectorAll("[data-model-role]").forEach(select=>{
    select.replaceChildren();select.disabled=!models.length;
    for(const model of models){const option=element("option","",model.label);option.value=model.id;select.append(option);}
    if(models.length)select.value=connection.selection[select.dataset.modelRole];
    else{const option=element("option","","应用密钥后选择模型版本");option.value="";select.append(option);}
  });
  document.querySelector("#key-status").textContent=models.length?"共用接口已连接":connection.has_api_key?"密钥已配置 · 待获取列表":"未配置密钥";
  document.querySelector("#model-catalog-status").textContent=models.length
    ?`已获取 ${models.length} 个文本模型版本，选择后自动保存。实际调用仍受账户权限和额度限制。`
    :"点击“应用密钥”获取当前接口的模型版本。";
}

// 收集表单快照，API Key 仅随本次同源请求进入本机服务内存。
function collectSettings() {
  const data={};
  document.querySelectorAll("[data-path]").forEach(input => {
    let value=input.type==="checkbox"?input.checked:input.value;
    if(input.type==="number") {
      if(value==="" && input.dataset.path.startsWith("research."))value=null;
      else{
        if(value==="" || !Number.isFinite(Number(value)))throw new Error("请填写有效数值："+input.closest("label").firstChild.textContent);
        value=Number(value);
      }
    }
    setPath(data,input.dataset.path,value);
  });
  return data;
}

// 根据任务状态禁用重复启动和显示可理解的完成状态。
function renderJob(job) {
  currentJob=job;
  const running=job?.status==="running";
  const supplementStatus=document.querySelector("#supplement-status");
  supplementStatus.hidden=job?.action!=="index";
  if(job?.action==="index")supplementStatus.textContent=running?"正在更新知识库…":job.status==="completed"?"知识库已更新，可前往知识库问答。":job.status==="cancelled"?"知识库更新已停止。":("知识库更新未完成。"+(job.logs.at(-1)||""));
  document.querySelectorAll(".run,.study-control").forEach(button=>{button.disabled=running||busyRequest||modelRequestBusy;});
  document.querySelectorAll(".manual-control").forEach(button=>{button.disabled=running||busyRequest;});
  document.querySelector("#pdf-drop-zone").setAttribute("aria-disabled",String(running||busyRequest));
  document.querySelector("#stop-task").disabled=!running;
  const status=document.querySelector("#job-status");
  const statusLabels={running:"运行中",completed:"已完成",failed:"未全部完成",cancelled:"已停止"};
  status.textContent=job?statusLabels[job.status]:"等待任务";
  status.className="badge "+(running?"running":job?.status==="failed"?"failed":job?.status==="completed"?"":"neutral");
  document.querySelector("#job-title").textContent=job?labels[job.action]:"准备就绪";
  if(job) {
    const log=document.querySelector("#task-log");const nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<50;
    const content=job.logs.join("\n") || "任务已启动，正在准备连接与处理文件…";
    if(log.textContent!==content){log.textContent=content;if(nearBottom)log.scrollTop=log.scrollHeight;}
    if(job.result && renderedResult!==job.id){renderResult(job.result);renderedResult=job.id;}
    if(job.action==="download" && !running && manualRefreshJob!==job.id && !busyRequest){
      manualRefreshJob=job.id;refreshManual().catch(error=>toast(error.message,true));
    }
    if(job.action==="ask" && ["failed","cancelled"].includes(job.status) && !job.result && renderedResult!==job.id){
      renderAnswerState(job.status==="cancelled"?"本次提问已停止":"本次提问未完成","请查看左侧任务记录。修正设置后可再次提问。");
      renderedResult=job.id;
    }
  }
}

// 提问开始或失败时明确展示当前状态，避免把上一轮回答误认为本次结果。
function renderAnswerState(title,text) {
  const content=element("div","empty-state");
  content.append(element("div","empty-symbol","⌕"),element("h2","",title),element("p","",text));
  document.querySelector("#answer-panel").replaceChildren(content);
}

// 回答使用结构化数据渲染；生成的报告保存在本机，不在任务记录中提供下载或预览入口。
function renderResult(result) {
  if(result.answer)renderAnswer(result.answer);
}

// 按实际证据编号展示回答，引用按钮定位到原文并展开对应片段。
function renderAnswer(data) {
  const container=document.querySelector("#answer-panel");container.replaceChildren();
  const header=element("div","answer-title");header.append(element("h2","","基于文献的回答"),element("span","badge",`${data.evidence.length} 个原文片段`));container.append(header);
  for(const claim of data.answer.claims) {
    const paragraph=element("p","answer-claim",claim.text);
    for(const source of new Set(claim.sources)) {
      const citation=element("a","citation",source);citation.href=`#evidence-${source}`;
      citation.addEventListener("click",event=>{event.preventDefault();const target=document.getElementById(`evidence-${source}`);if(target){target.open=true;target.scrollIntoView({behavior:"smooth",block:"center"});}});
      paragraph.append(citation);
    }
    container.append(paragraph);
  }
  if(data.answer.limitations)container.append(element("p","limitations",data.answer.limitations));
  container.append(element("h3","evidence-heading","原文证据 · 页码为 PDF 实际页序"));
  for(const item of data.evidence) {
    const detail=element("details","evidence");detail.id=`evidence-${item.source_id}`;
    detail.append(element("summary","",`${item.source_id}  ·  ${item.title}  / PDF 第 ${item.page} 页`));
    const body=element("div","evidence-body");body.append(element("p","evidence-meta",`DOI：${item.doi||"下载记录未提供"}  ·  页内片段 ${item.ordinal}`),element("blockquote","",item.text));
    if(item.pdf_url){const link=element("a","file-link","打开 PDF · 第 "+item.page+" 页 ↗");link.href=item.pdf_url;link.target="_blank";link.rel="noopener";body.append(link);}
    detail.append(body);container.append(detail);
  }
}

// 启动现有业务阶段，所有表单以独立配置快照传入，不重写 Python 源文件。
async function run(action) {
  if(busyRequest || modelRequestBusy || currentJob?.status==="running")return;
  busyRequest=true;renderJob(currentJob);
  try {
    if(["search","ask"].includes(action)&&(modelDraftChanged||!modelConnection.models?.length))
      throw new Error("请先在模型配置中应用共用密钥并选择模型版本。");
    const settings=collectSettings();
    const options={dry_run:false,retrieve_only:document.querySelector("#retrieve-only").checked};
    const result=await api("/api/run",{action,settings,options});
    document.querySelector("#notice").hidden=true;
    if(action==="ask")renderAnswerState("正在检索与阅读原文…","任务在后台运行，可在左侧查看日志或停止任务。");
    renderJob(result.job);
    toast(`${labels[action]}已启动${options.dry_run?"（预览）":""}`);
  } catch(error) {toast(error.message,true);document.querySelector("#notice").textContent=error.message;document.querySelector("#notice").hidden=false;}
  finally {busyRequest=false;renderJob(currentJob);}
}

// 轮询仅更新任务和连接状态，不覆盖用户正在编辑的表单。
async function poll() {
  const revision=studyRevision;
  try {
    const state=await api("/api/state");
    connectionLost=false;
    if(revision===studyRevision&&!busyRequest&&(!studyState||state.studies?.active_id===studyState.active_id))renderJob(state.job);
  } catch {
    if(!connectionLost)toast("本机服务连接中断，请检查运行程序的终端。",true);
    connectionLost=true;
  }
  setTimeout(poll,1000);
}

// 从磁盘恢复未完成清单，界面刷新或软件重启后仍可继续手动补充。
async function refreshManual() {
  if(currentJob?.status==="running")throw new Error("请等待当前任务完成后刷新清单");
  const result=await api("/api/manual-list",{settings:collectSettings()});
  manualDestination=result.output_dir;
  manualItems=result.items;
  manualDirectoryField=document.querySelector('[data-path="download.output_dir"]').value;
  document.querySelector("#manual-count").textContent=`${result.items.length} 篇待补充`;
  document.querySelector("#manual-destination").textContent="导入保存至："+result.output_dir;
  const report=document.querySelector("#manual-report");
  report.hidden=!result.report;
  if(result.report){report.href=result.report.url;report.download=result.report.name;}
  const list=document.querySelector("#manual-list");list.replaceChildren();
  if(!result.items.length)list.append(element("p","field-help","暂无下载未完成的记录。可以直接导入本地 PDF，或先运行自动下载。"));
  for(const item of result.items){
    const row=element("article","manual-item");
    row.append(element("h3","",item.title),element("p","field-help","DOI："+item.doi));
    const actions=element("div","manual-toolbar");
    const link=element("a","file-link","打开文章入口 ↗");link.href=item.url;link.target="_blank";link.rel="noopener noreferrer";
    const button=element("button","button secondary small manual-control","导入 PDF");
    button.addEventListener("click",()=>choosePDFs(item.doi,item.title));
    actions.append(link,button);row.append(actions);
    const reason=element("details","manual-reason");reason.append(element("summary","","查看未下载原因"),element("p","",item.reason));row.append(reason);
    list.append(row);
  }
  renderJob(currentJob);
}

// 使用浏览器文件选择器，用户无需填写路径；只将主动选择的文件发送给本机服务。
function choosePDFs(doi="",title="") {
  if(busyRequest||currentJob?.status==="running")return;
  const picker=element("input");picker.type="file";picker.accept=".pdf,application/pdf";picker.multiple=!doi;
  picker.hidden=true;document.body.append(picker);
  picker.addEventListener("cancel",()=>picker.remove(),{once:true});
  picker.addEventListener("change",()=>{const files=Array.from(picker.files);picker.remove();importPDFs(files,doi,title);});picker.click();
}

// 切换补充方式，缺失文献和用户自行收集的文献各有独立入口。
function showSupplement(mode) {
  document.querySelector("#manual-panel").hidden=mode!=="missing";
  document.querySelector("#extra-panel").hidden=mode!=="extra";
  document.querySelector("#manual-panel").classList.toggle("slide-back",mode==="missing");
  document.querySelectorAll("[data-supplement]").forEach(button=>{
    const selected=button.dataset.supplement===mode;
    button.classList.toggle("active",selected);button.setAttribute("aria-selected",String(selected));
  });
  document.querySelector("#supplement-prev").disabled=mode==="missing";
  document.querySelector("#supplement-next").disabled=mode==="extra";
  document.querySelector("#supplement-step").textContent=mode==="missing"?"01 / 02":"02 / 02";
  document.querySelector(mode==="missing"?"#missing-tab":"#extra-tab").focus({preventScroll:true});
}

// 多选缺失 PDF 后逐个指定对应文章，避免凭文件顺序或不可靠文件名误配 DOI。
function chooseMissingPDFs() {
  if(busyRequest||currentJob?.status==="running")return;
  if(!manualItems.length){toast("暂无缺失文献，请先刷新清单；其他 PDF 可在“添加其他文献”导入。");return;}
  const directory=manualDestination;
  const items=manualItems.slice();
  const picker=element("input");picker.type="file";picker.accept=".pdf,application/pdf";picker.multiple=true;
  picker.hidden=true;document.body.append(picker);
  picker.addEventListener("cancel",()=>picker.remove(),{once:true});
  picker.addEventListener("change",()=>{
    const files=Array.from(picker.files);picker.remove();if(!files.length)return;
    const box=document.querySelector("#missing-mapping");box.replaceChildren();box.hidden=false;
    box.append(element("p","field-help","请选择每个 PDF 对应的文章，标题和 DOI 将一并保存。"));
    const selectors=files.map(file=>{
      const label=element("label","mapping-row",file.name);
      const select=element("select","manual-control");
      const placeholder=element("option","","选择对应的缺失文献");placeholder.value="";select.append(placeholder);
      for(const item of items){const option=element("option","",`${item.title} · ${item.doi}`);option.value=item.doi;select.append(option);}
      label.append(select);box.append(label);return select;
    });
    const actions=element("div","manual-toolbar");
    const submit=element("button","button primary manual-control",`导入 ${files.length} 份 PDF`);
    submit.addEventListener("click",async()=>{
      const assignments=selectors.map(select=>select.value);
      if(assignments.some(value=>!value)){toast("请为每份 PDF 选择对应文章。",true);return;}
      if(new Set(assignments).size!==assignments.length){toast("同一篇文章只能选择一份 PDF，请检查对应关系。",true);return;}
      if(directory!==manualDestination){toast("下载目录已改变，请重新选择 PDF。",true);return;}
      await importPDFs(files,"","",assignments);box.hidden=true;box.replaceChildren();
    });
    const cancel=element("button","button secondary manual-control","取消");
    cancel.addEventListener("click",()=>{box.hidden=true;box.replaceChildren();});
    actions.append(submit,cancel);box.append(actions);renderJob(currentJob);
  });
  picker.click();
}

// 逐份导入并保留每份结果；单个失败不阻止其余文件，也不在此阶段调用模型。
async function importPDFs(files,doi="",title="",assignments=null) {
  if(!files.length||busyRequest||currentJob?.status==="running")return;
  busyRequest=true;renderJob(currentJob);
  const log=document.querySelector("#import-results");log.hidden=false;
  const linked=Boolean(doi||assignments);
  log.textContent=doi?`关联文章：${title}\nDOI：${doi}\n`:assignments?"批量补充缺失 PDF\n":"导入其他文献\n";
  try{
    if(linked && manualDirectoryField!==document.querySelector('[data-path="download.output_dir"]').value)
      throw new Error("下载目录已修改，请先刷新清单再选择文章");
    if(!linked)await refreshManual();
    for(const [index,file] of files.entries()){
      const articleDoi=assignments?assignments[index]:doi;
      log.textContent+=`正在导入：${file.name}${articleDoi?" → "+articleDoi:""}\n`;
      try{
        if(!file.name.toLowerCase().endsWith(".pdf")||file.size>50*1024*1024)throw new Error("请选择不超过 50 MB 的 PDF");
        const query=new URLSearchParams({filename:file.name,doi:articleDoi,output_dir:manualDestination});
        const response=await fetch("/api/import-pdf?"+query,{method:"POST",headers:{"X-UI-Token":token,"Content-Type":"application/pdf"},body:file});
        const result=await response.json();if(!response.ok)throw new Error(result.error||"导入失败");
        const status={imported:"已导入",skipped:"已存在，跳过重复文件",linked:"已关联已有文件"}[result.status];
        log.textContent+=`${status}：${file.name}（${result.pages} 页）\n`;
      }catch(error){log.textContent+=`失败：${file.name}：${error.message}\n`;}
    }
    await refreshManual();
    log.textContent+="处理结束。可更新知识库用于后续问答。";
  }catch(error){log.textContent+="导入未完成："+error.message;}
  finally{busyRequest=false;renderJob(currentJob);}
}

// 初始化页面、绑定用户动作，并开始读取后台任务状态。
async function init() {
  document.querySelector("#refresh-interface").addEventListener("click",()=>window.location.reload());
  buildModelFields();buildOtherFields();
  document.querySelector("#cleanup-study").addEventListener("click",previewStudyCleanup);
  document.querySelector("#cleanup-form").addEventListener("submit",confirmStudyCleanup);
  document.querySelector("#cancel-cleanup").addEventListener("click",()=>{if(!busyRequest){document.querySelector("#cleanup-dialog").close();cleanupPlan=null;}});
  document.querySelector("#cleanup-name").addEventListener("input",event=>{
    document.querySelector("#confirm-cleanup").disabled=!cleanupPlan||event.target.value!==cleanupPlan.name||!cleanupPlan.paths.length;
  });
  document.querySelector("#study-select").addEventListener("change",()=>changeStudy());
  document.querySelector("#new-study").addEventListener("click",()=>{
    document.querySelector("#study-name").value="";
    document.querySelector("#study-directory").value=studyState?.default_directory||"";
    document.querySelector("#study-dialog").showModal();
  });
  document.querySelector("#cancel-study").addEventListener("click",()=>document.querySelector("#study-dialog").close());
  document.querySelector("#browse-study-directory").addEventListener("click",()=>browsePath(document.querySelector("#study-directory"),"study.directory"));
  document.querySelector("#study-form").addEventListener("submit",event=>{event.preventDefault();changeStudy(true);});
  document.querySelector("#wizard-prev").addEventListener("click",()=>advanceWizard(-1));
  document.querySelector("#wizard-next").addEventListener("click",()=>advanceWizard(1));
  document.querySelectorAll(".wizard-step input,.wizard-step textarea").forEach(input=>input.addEventListener("input",()=>input.setCustomValidity("")));
  document.querySelector("#refresh-manual").addEventListener("click",()=>refreshManual().catch(error=>toast(error.message,true)));
  document.querySelector("#import-missing").addEventListener("click",chooseMissingPDFs);
  document.querySelector("#supplement-prev").addEventListener("click",()=>showSupplement("missing"));
  document.querySelector("#supplement-next").addEventListener("click",()=>showSupplement("extra"));
  document.querySelectorAll("[data-supplement]").forEach(button=>button.addEventListener("click",()=>showSupplement(button.dataset.supplement)));
  const drop=document.querySelector("#pdf-drop-zone");
  drop.addEventListener("click",()=>choosePDFs());
  drop.addEventListener("keydown",event=>{if(["Enter"," "].includes(event.key)){event.preventDefault();choosePDFs();}});
  drop.addEventListener("dragover",event=>{event.preventDefault();});
  drop.addEventListener("drop",event=>{event.preventDefault();importPDFs(Array.from(event.dataTransfer.files));});
  document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>showTab(button.dataset.tab)));
  document.querySelectorAll(".run").forEach(button=>button.addEventListener("click",()=>run(button.dataset.action)));
  document.querySelector("#apply-key").addEventListener("click",applySharedModel);
  document.querySelector("#shared-model-key").addEventListener("input",onSharedKeyInput);
  document.querySelector("#shared-model-provider").addEventListener("change",()=>{
    providerManuallySelected=true;invalidateModelConnection();
    document.querySelector("#provider-hint").textContent="密钥只会发送至所选服务商的官方接口。切换服务商后请填写对应密钥。";
  });
  document.querySelector("#apply-paths").addEventListener("click",async()=>{
    try{await api("/api/settings",{settings:collectSettings()});await refreshManual();toast("路径已应用。");}
    catch(error){toast(error.message,true);}
  });
  document.querySelector("#stop-task").addEventListener("click",async()=>{
    try{await api("/api/stop",{});toast("已停止本机任务。Zotero 已提交的下载可能继续。");}
    catch(error){toast(error.message,true);}
  });
  try{
    const state=await api("/api/state");token=state.token;fillForm(state.settings);renderStudies(state.studies);refreshKeyStatus(state.connection);renderJob(state.job);
    if(state.notice){document.querySelector("#notice").textContent=state.notice;document.querySelector("#notice").hidden=false;}
    if(state.job?.status!=="running")await refreshManual();
    poll();
  }catch(error){toast("无法连接本机服务："+error.message,true);}
}
init();

"use strict";

// One-time browser preference migration for this explicitly requested rename.
// Session files themselves are migrated and accessed only on the server.
try{
  for(const key of Object.keys(localStorage))if(key.startsWith("codexing-")){
    const target="recoding-"+key.slice(9);let value=localStorage.getItem(key);
    if(key==="codexing-session"){
      const saved=JSON.parse(value);if(typeof saved?.path==="string")saved.path=saved.path.replaceAll("/.codexing/","/.recoding/");value=JSON.stringify(saved);
    }
    if(localStorage.getItem(target)===null)localStorage.setItem(target,value);
    localStorage.removeItem(key);
  }
  for(const storage of [localStorage,sessionStorage]){
    const savedSession=JSON.parse(storage.getItem("recoding-session") || "null");
    if(typeof savedSession?.path==="string" && savedSession.path.includes("/.recoding.../")){
      savedSession.path=savedSession.path.replaceAll("/.recoding.../","/.recoding/");
      storage.setItem("recoding-session",JSON.stringify(savedSession));
    }
  }
}catch{} // Blocked browser storage never blocks server-backed Sessions.

const $ = id => document.getElementById(id);
const state = {config: null, session: null, source: null, sequence: 0, views: new Map(), current: null, opening: 0, switching: false, composing: false, eventLog: new Map(), picker: null, dialogMode: "create", imageAssets: new Map(), drafts: new Map()};
const unreadHumanMessages = new Map();
const inventoryRefreshes=new Map();
state.humanMessages = new Map();
const modelRates = new Map();
const modelSaves = new Set();
state.pendingSettings=new Map();
const liveAgentStates=new Set(["starting","running","sleeping","stopping"]);
const acceptingAgentStates=new Set(["starting","running","sleeping"]);
const humanInputStates=new Set(["starting","running","sleeping","stopping","stopped","failed"]);
const stoppedAgentStates=new Set(["stopped","failed"]);
const agentStatus=agent=>agent?.master_status || (agent?.status==="ready" ? "starting" : ['run','stop'].includes(agent?.status) ? 'stopped' : agent?.status || "stopped");
const loopIsLive=agent=>liveAgentStates.has(agentStatus(agent));
function sessionStatusLabel(session) {
  if(session.busy)return "占用";
  if(['run','stop'].includes(session.status))return session.status.toUpperCase();
  if(session.active_agents>0 && !acceptingAgentStates.has(agentStatus(session)))return `SUB ${session.active_agents}`;
  return ({starting:"START",running:"RUN",sleeping:"SLEEP",stopping:"STOPPING",stopped:"STOP",failed:"FAILED"})[agentStatus(session)] || "";
}
const sessionIsActive=session=>session.status==='run' || session.active_agents>0 || acceptingAgentStates.has(agentStatus(session));
const expandedDirectories = new Map();
let directorySessionContents=new Map();
const expandedSessionGroups=new Map();
let localSessionContents=new Map();
const nonSessionFolders=new Set();
let listingRequest, navigationTimer;
state.agentId = "master";state.agentDetail = null;state.viewSequence = 0;state.agentOpening = 0;
try{
  const stored=sessionStorage.getItem('recoding-page-id'),navigation=performance.getEntriesByType('navigation')[0];
  state.pageId=stored && /^[a-f0-9-]{36}$/.test(stored) && !(window.opener && navigation?.type==='navigate') ? stored : crypto.randomUUID();
  sessionStorage.setItem('recoding-page-id',state.pageId);
}catch{state.pageId=crypto.randomUUID();}
state.documentId=crypto.randomUUID();
state.guiConnected=false;
state.pageLeaving=false;
async function prepareGuiIdentity() {
  // A duplicated tab can inherit sessionStorage. Only one live document may
  // reuse that identity; reload releases its document lock automatically.
  if(!navigator.locks)return;
  await new Promise(resolve=>{
    navigator.locks.request(`recoding-gui-${state.pageId}`,{ifAvailable:true},async lock=>{
      if(!lock){state.pageId=crypto.randomUUID();try{sessionStorage.setItem('recoding-page-id',state.pageId);}catch{}resolve();return;}
      resolve();await new Promise(()=>{});
    }).catch(()=>resolve());
  });
}
state.agentBacklog = new Map();state.rootBacklog = new Map();state.rootHistory=new Map();
state.userInputs = new Map();
state.systemInputs = new Map();
const inputPlacements = new WeakMap();
const workspaceSync = {loaded: false, showTimer: null, hideTimer: null};
let soundEnabled = true, audioContext;
try {soundEnabled = localStorage.getItem("recoding-completion-sound") !== "off";} catch {}
let detailMode="chat";
const composerPreferences=new Map();
const conversationRanges=new Map();
let conversationRefreshPending=false;
function composerKey(){return state.session ? `${state.session.id}/${state.agentId}` : null;}
function composerPreference(){
  const key=composerKey();
  if(!key)return {};
  if(!composerPreferences.has(key)) {
    let saved;
    try{saved=JSON.parse(localStorage.getItem(`recoding-composer/${key}`));}catch{}
    composerPreferences.set(key,saved && typeof saved==="object" && !Array.isArray(saved) ? saved : {});
  }
  return composerPreferences.get(key);
}
function saveComposerPreference(patch){
  const key=composerKey();if(!key)return;
  const saved=Object.assign(composerPreference(),patch);
  try{localStorage.setItem(`recoding-composer/${key}`,JSON.stringify(saved));}catch{}
}
function loadComposerPreferences(){
  const mode=composerPreference().detailMode;
  detailMode=(isAllAgentsView() ? ["human","chat","filter"] : ["human","chat","all"]).includes(mode) ? mode : "chat";renderLoopVisibility();
  composerLayout.load();
  for(const id of ["composer-help","message-recipients","all-message-filter-panel"])if($(id).matches(":popover-open"))$(id).hidePopover();
}
const text = (tag, content, className) => {
  const node = document.createElement(tag);
  node.textContent = content ?? "";
  if (className) node.className = className;
  return node;
};
function renderLoopVisibility() {
  document.body.dataset.detailMode=detailMode;document.body.dataset.agentView=isAllAgentsView() ? "all" : "agent";
  $("detail-visibility").querySelector("[data-detail-mode=chat]").textContent=isAllAgentsView() ? "看全部agent对话" : "看Agent之间对话";
  $("detail-visibility").querySelector("[data-detail-mode=all]").hidden=isAllAgentsView();
  $("all-message-filter").hidden=!state.session || !isAllAgentsView();
  if(detailMode!=="filter" && $("all-message-filter-panel").matches(":popover-open"))$("all-message-filter-panel").hidePopover();
  for(const button of $("detail-visibility").querySelectorAll("button"))button.setAttribute("aria-pressed",String(button.dataset.detailMode===detailMode));
  renderConversationDetails();
}
$("detail-visibility").addEventListener("click",event=>{
  const button=event.target.closest("button[data-detail-mode]");
  if(!button || button.hidden || button.dataset.detailMode===detailMode && !conversationRanges.has(composerKey()) && !$("messages").querySelector(".message-navigation-target"))return;
  preserveConversationPosition(()=>{
    for(const node of $("messages").querySelectorAll(".message-navigation-target"))node.classList.remove("message-navigation-target");
    conversationRanges.delete(composerKey());
    detailMode=button.dataset.detailMode;
    saveComposerPreference({detailMode});renderLoopVisibility();if(isAllAgentsView())renderAllMessages();
  });
});
renderLoopVisibility();

function preserveConversationPosition(update,{anchor,reveal}={}){
  const scroller=$("conversation-scroll"),frame=scroller.getBoundingClientRect();
  const atEnd=!anchor && scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<36;
  if(!anchor && !atEnd) {
    const messages=[...$("messages").querySelectorAll(".user-followup,.human-message,.peer-message")].filter(node=>node.checkVisibility());
    const visible=messages.filter(node=>{const box=node.getBoundingClientRect();return box.bottom>frame.top&&box.top<frame.bottom;});
    // Human correspondence survives all three modes and is the stable anchor.
    anchor=visible.find(node=>node.classList.contains("human-message")) || visible.find(node=>node.classList.contains("user-followup")) || visible[0];
    if(!anchor)anchor=messages.sort((a,b)=>Math.abs(a.getBoundingClientRect().top-frame.top)-Math.abs(b.getBoundingClientRect().top-frame.top))[0];
  }
  const box=anchor?.getBoundingClientRect(),inFrame=box&&box.bottom>frame.top&&box.top<frame.bottom;
  const offset=inFrame ? box.top-frame.top : 12;
  const previous=scroller.style.overflowAnchor;scroller.style.overflowAnchor="none";
  update();renderConversationDetails();
  const target=reveal?.();
  if(target?.checkVisibility())scroller.scrollTop+=target.getBoundingClientRect().top-scroller.getBoundingClientRect().top-12;
  else if(atEnd)scrollToEnd();
  else if(anchor?.isConnected&&anchor.checkVisibility())scroller.scrollTop+=anchor.getBoundingClientRect().top-scroller.getBoundingClientRect().top-offset;
  requestAnimationFrame(()=>{scroller.style.overflowAnchor=previous;});
}

function scheduleConversationDetails(){
  if(state.replaying || conversationRefreshPending)return;
  conversationRefreshPending=true;
  queueMicrotask(()=>{
    conversationRefreshPending=false;
    renderConversationDetails();
  });
}
function renderConversationDetails(){
  if(!state.session || isAllAgentsView())return;
  const timeline=[];
  for(const node of $("messages").children) {
    if(node.classList.contains("agent-run"))timeline.push(...node.querySelector(":scope > .steps").children);
    else if(!node.classList.contains("earlier-history"))timeline.push(node);
  }
  const entries=timeline.filter(node=>!node.classList.contains("conversation-detail-control"));
  const isUser=node=>node.classList.contains("user-followup"),isReply=node=>node.classList.contains("human-message");
  const isDetail=node=>!isUser(node)&&!isReply(node);
  const hiddenByMode=node=>isDetail(node)&&(detailMode==="human" || detailMode==="chat"&&!node.classList.contains("peer-message"));
  const ranges=new Map();let lastUser=-1;
  for(const [index,node] of entries.entries()) {
    if(!isUser(node)&&!isReply(node))continue;
    const user=isUser(node),key=`${user ? "after" : "before"}/${node.dataset.messageId}`;
    let end=user ? entries.findIndex((next,i)=>i>index&&isUser(next)) : index;
    if(end<0)end=entries.length;
    const start=user ? index+1 : lastUser+1;
    let control=node.conversationControl;
    if(!control) {
      const holder=text("div","","conversation-detail-control"),button=text("button","","text-button conversation-detail-button");
      button.type="button";holder.dataset.detailFor=key;holder.append(button);control=node.conversationControl={holder,button};
      button.addEventListener("click",()=>{
        const show=button.getAttribute("aria-expanded")!=="true",selections=conversationRanges.get(composerKey()) || new Map();
        preserveConversationPosition(()=>{
          // The latest choice controls overlapping ranges, including live
          // details appended after the most recent Human input.
          selections.delete(key);selections.set(key,show);conversationRanges.set(composerKey(),selections);
        },{anchor:button,reveal:!user&&show ? ()=>control.firstDetail : undefined});
      });
    }
    if(user)node.after(control.holder);else node.before(control.holder);
    ranges.set(key,{start,end,control,user});if(user)lastUser=index;
  }
  const exposed=new Set();
  for(const [key,show] of conversationRanges.get(composerKey()) || []) {
    const range=ranges.get(key);if(!range)continue;
    for(let i=range.start;i<range.end;i++)if(hiddenByMode(entries[i])){if(show)exposed.add(i);else exposed.delete(i);}
  }
  for(const [index,node] of entries.entries())if(isDetail(node))node.classList.toggle("conversation-detail-visible",exposed.has(index));
  for(const {start,end,control,user} of ranges.values()) {
    const indices=[];for(let i=start;i<end;i++)if(hiddenByMode(entries[i])&&!entries[i].hidden)indices.push(i);
    const expanded=indices.length>0&&indices.every(i=>exposed.has(i));
    control.firstDetail=indices.length ? entries[indices[0]] : null;
    control.button.disabled=!indices.length;control.button.setAttribute("aria-expanded",String(expanded));
    control.button.textContent=`${expanded ? "▾ 收起" : "▸ 展开"}${user ? "后续细节" : "此前细节"}`;
    control.button.title=user ? "从这条用户消息之后，到下一条用户消息之前；新过程继续加入。" : "从上一条用户消息之后，到这条回复之前；没有用户消息时从 Agent 启动开始。";
  }
}

function loopMark(className) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");svg.setAttribute("class", `loop-mark ${className}`);svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");use.setAttribute("href", "#recoding-loop");svg.append(use);
  return svg;
}

function guiConnectionRejected(result) {
  state.guiConflict=result;state.guiConnected=false;
  state.serverChanged=true;state.source?.close();state.source=null;
  controls();
  let dialog=$('gui-connection-rejected');
  if(!dialog){
    dialog=document.createElement('dialog');dialog.id='gui-connection-rejected';
    const heading=document.createElement('h2');heading.textContent='此 Host 已连接其他页面';
    const detail=document.createElement('p');detail.className='gui-conflict-copy';
    const actions=document.createElement('div');actions.className='gui-conflict-actions';
    const takeover=document.createElement('button');takeover.type='button';takeover.id='takeover-gui';
    takeover.className='gui-takeover-button';takeover.textContent='接管此页面';
    takeover.onclick=async()=>{
      const conflict=state.guiConflict;takeover.disabled=true;
      state.documentId=crypto.randomUUID();
      try{
        const response=await fetch('/api/gui/takeover',{method:'POST',
          headers:{'Content-Type':'application/json','X-Agent-Token':conflict.takeover_token,
                   'X-recoding-Page':state.pageId,'X-recoding-Document':state.documentId},
          body:JSON.stringify({instance_id:conflict.instance_id})});
        const value=await response.json();
        if(!response.ok)throw new Error(value.error || `HTTP ${response.status}`);
        location.reload();
      }catch(error){detail.textContent=error.message;takeover.disabled=false;}
    };
    const retry=document.createElement('button');retry.type='button';retry.textContent='重新连接';retry.onclick=()=>location.reload();
    retry.className='text-button';actions.append(takeover,retry);
    dialog.append(heading,detail,actions);dialog.addEventListener('cancel',event=>event.preventDefault());document.body.append(dialog);
  }
  dialog.querySelector('.gui-conflict-copy').textContent=result.error+' 接管会断开旧页面，服务器上的 Session 和任务继续运行。';
  $('takeover-gui').disabled=!result.takeover_token;
  if(!dialog.open)dialog.showModal();
}

function revokeArchiveView() {
  ++state.opening;state.source?.close();state.source=null;state.session=null;state.current=null;
  modelExcerptObserver.disconnect();state.views.clear();state.eventLog.clear();state.imageAssets.clear();
  state.rootHistory.clear();state.userInputs.clear();state.systemInputs.clear();
  $('messages').replaceChildren();$('welcome').hidden=false;inspector();controls();
}

async function api(path, body, {operation, onHeaders} = {}) {
  if(state.serverChanged && path!=="/api/config")throw new Error("服务已重启，请重新连接此页面。");
  const documentId=state.documentId;
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-Agent-Token": state.config.csrf}, body: JSON.stringify(body)};
  options.headers={...options.headers,"X-recoding-Page":state.pageId,"X-recoding-Document":documentId};
  if(operation)options.headers={...options.headers,"X-recoding-Operation":operation};
  const response = await fetch(path, options);
  if(documentId!==state.documentId){response.body?.cancel().catch(()=>{});throw new Error('页面连接已更新，忽略旧请求');}
  if(path!=="/api/config" && serviceInstanceChanged(response.headers.get("X-Agent-Instance"))) {
    response.body?.cancel().catch(()=>{});throw new Error("服务已重启，请重新连接此页面。");
  }
  onHeaders?.(response);
  let result;
  try {result=await response.json();}
  catch {throw Object.assign(new Error(`后端返回了非 JSON 响应（HTTP ${response.status}），请检查服务器或代理。`),{status:response.status,path});}
  if(!response.ok && result.code==='gui_busy')guiConnectionRejected(result);
  if(!response.ok && result.code==='session_busy' && state.session?.read_only)revokeArchiveView();
  if (!response.ok) throw Object.assign(new Error(result.error || `HTTP ${response.status}`),{status:response.status,code:result.code,path});
  if(!state.serverChanged && !state.pageLeaving)state.guiConnected=true;
  return result;
}

function serviceInstanceChanged(instance) {
  if(!instance || !state.config?.instance_id || instance===state.config.instance_id)return false;
  if(state.serverChanged)return true;
  state.serverChanged=true;state.source?.close();state.source=null;
  const op=beginWorkspaceSync("restart");op.retry=()=>location.reload();
  failWorkspaceSync(new Error("检测到新的 Web 服务。点击重新连接，恢复此页面；历史和工作文件仍在服务器上。"),op);
  op.label="Web 服务已重启";renderWorkspaceSync(op);$("retry-workspace-sync").textContent="重新连接";
  if(state.session)state.session.status="disconnected";
  controls();return true;
}
let serviceCheck;
function verifyServiceInstance() {
  if(state.serverChanged)return Promise.resolve(false);
  if(!serviceCheck)serviceCheck=fetch("/api/config",{headers:{"X-recoding-Page":state.pageId,"X-recoding-Document":state.documentId},signal:AbortSignal.timeout(3000)}).then(async response=>{
    if(!response.ok){const error=await response.json();if(error.code==='gui_busy')guiConnectionRejected(error);return false;}
    const config=await response.json();return !serviceInstanceChanged(config.instance_id);
  }).catch(()=>false).finally(()=>{serviceCheck=null;});
  return serviceCheck;
}
function connectSessionEvents() {
  if(!state.session || state.session.read_only || state.serverChanged || document.hidden || state.source)return;
  const version=state.opening,identifier=state.session.id;
  const source=new EventSource(`/api/sessions/${encodeURIComponent(identifier)}/events?after=${state.sequence}&instance=${encodeURIComponent(state.config.instance_id)}&page=${state.pageId}&document=${state.documentId}`);
  state.source=source;
  source.onmessage=message=>{
    if(version!==state.opening)return;
    const event=JSON.parse(message.data);
    if(event.type==='session_stopped'){
      source.close();if(state.source===source)state.source=null;
      state.session.read_only=true;state.session.session_state='stop';state.session.status='stopped';
      inspector();scheduleNavigationRefresh();return;
    }
    dispatch(event);
  };
  source.onerror=()=>{
    if(version!==state.opening || state.source!==source)return;
    verifyServiceInstance();
  };
}

let toastTimer;
function toast(message, action=null) {
  const notice=$("toast");notice.replaceChildren();
  if(action) {
    const button=text("button","","toast-action");button.type="button";
    button.append(text("span",message),text("span","查看消息 ↗","toast-action-hint"));
    button.addEventListener("click",async()=>{
      notice.hidden=true;clearTimeout(toastTimer);
      try{await action();}catch(error){toast(error.message);}
    });
    notice.append(button);
  } else notice.textContent=message;
  notice.classList.toggle("actionable",Boolean(action));notice.hidden=false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6500);
}

function updateSoundButton() {
  const button = $("toggle-sound");
  button.setAttribute("aria-pressed", String(soundEnabled));
  button.setAttribute("aria-label", soundEnabled ? "关闭消息提示音" : "开启消息提示音");
  button.title = soundEnabled ? "消息提示音已开启" : "消息提示音已关闭";
  button.textContent = soundEnabled ? "♪" : "♪̸";
}
function unlockSound() {
  if (!soundEnabled) return;
  try {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    audioContext ||= new Context();
    if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
  } catch {} // Muted/unsupported browsers still show completion badges.
}
function completionSound() {
  if (!soundEnabled || audioContext?.state !== "running") return;
  try {
    const now = audioContext.currentTime;
    for (const [offset, frequency] of [[0, 660], [0.16, 880]]) {
      const oscillator = audioContext.createOscillator(), gain = audioContext.createGain();
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0, now + offset);
      gain.gain.linearRampToValueAtTime(0.12, now + offset + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.001, now + offset + 0.22);
      oscillator.connect(gain); gain.connect(audioContext.destination);
      oscillator.onended = () => {oscillator.disconnect();gain.disconnect();};
      oscillator.start(now + offset); oscillator.stop(now + offset + 0.24);
    }
  } catch {}
}
document.addEventListener("pointerdown", unlockSound, {passive: true});
document.addEventListener("keydown", unlockSound);
$("toggle-sound").addEventListener("click", () => {
  soundEnabled = !soundEnabled;
  try {localStorage.setItem("recoding-completion-sound", soundEnabled ? "on" : "off");} catch {}
  updateSoundButton(); if (soundEnabled) unlockSound();
});
updateSoundButton();

function unreadCount(sessionId,agentId) {
  const unread=unreadHumanMessages.get(sessionId);
  return unread ? [...unread.values()].filter(source=>!agentId || source===agentId).length : 0;
}
function paintMessageBadge(session) {
  const count=unreadCount(session.id);
  for(const row of $("session-list").querySelectorAll(".session-row"))if(row.dataset.id===session.id) {
    row.classList.toggle("has-unread",count>0);
    const badge=row.querySelector(".session-status");
    badge.textContent=session.storage_missing ? "存档缺失" : count ? `${count} 新消息` : sessionStatusLabel(session);
    badge.title=count ? "Agents 发给你的未读消息" : agentStatus(session);
  }
}
function readHumanMessages(agentId=state.agentId) {
  const unread=unreadHumanMessages.get(state.session?.id);
  if(unread)for(const [id,source] of unread)if(source===agentId || agentId==="all")unread.delete(id);
  if(state.session)paintMessageBadge(state.session);
}

function scrollToEnd() {
  const scroller = $("conversation-scroll");
  scroller.scrollTo({top: scroller.scrollHeight, behavior: "instant"});
}

// Keep text visible if a local rendering asset fails to load.
function markdown(source) {
  try { return window.AgentMarkdown.render(source, {imageURL: resolveImageURL}); }
  catch { return text("pre", String(source ?? ""), "markdown-fallback"); }
}

function agentAPI(suffix = "", id = state.agentId) {if(id==="all")id="master";return `/api/sessions/${encodeURIComponent(state.session.id)}${id !== "master" ? `/agents/${encodeURIComponent(id)}` : ""}${suffix}`;}
function assetURL(digest,owner=state.agentId) {return agentAPI(`/images/${digest}?page=${encodeURIComponent(state.pageId)}&document=${state.documentId}`,owner);}
function resolveImageURL(value) {
  if (!state.session || typeof value !== "string") return null;
  let path = value.trim();
  if (/^https?:\/\//i.test(path)) {
    try {const url = new URL(path); return url.username || url.password ? null : url.href;} catch {return null;}
  }
  if (/^data:image\/(png|jpeg);base64,[a-z0-9+/=]+$/i.test(path) && path.length < 7 * 1024 * 1024) return path;
  path = path.replace(/^sandbox:/, "");
  if (path.startsWith("/api/")) return path.startsWith(agentAPI('/images/')) && /^\w+$/.test(path.split("/").pop()) ? path : null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(path) || path.startsWith("//")) return null;
  try {path = decodeURIComponent(path);} catch {return null;}
  const workspace=state.session.directory;
  if(workspace && path.startsWith(workspace+"/"))path=path.slice(workspace.length+1);
  const relative = path.replace(/^\/workspace\//, "").replace(/^\.\//, "");
  if (!relative || relative.startsWith("/") || relative.split("/").includes("..")) return null;
  const digest = state.imageAssets.get(relative);
  return digest ? assetURL(digest) : agentAPI(`/workspace-image?path=${encodeURIComponent(relative)}&page=${encodeURIComponent(state.pageId)}&document=${state.documentId}`);
}
function rememberImage(image) {
  if (image?.path && /^[a-f0-9]{64}$/.test(image.sha256 || "")) {
    const workspace=state.session?.directory;
    const path=workspace && image.path.startsWith(workspace+"/") ? image.path.slice(workspace.length+1) : image.path;
    state.imageAssets.set(path.replace(/^\/workspace\//,""), image.sha256);
  }
}
function imageGallery(pictures,owner=state.agentId) {
  const gallery = text("div", "", "image-gallery");
  for (const picture of pictures) {
    if(owner===state.agentId)rememberImage(picture);
    if(!/^[a-f0-9]{64}$/.test(picture?.sha256 || ""))continue;
    const link = text("a", ""); link.href = assetURL(picture.sha256,owner); link.target = "_blank"; link.rel = "noopener noreferrer";
    const image = document.createElement("img"); image.src = link.href; image.alt = picture.name || picture.path || "图片附件";
    image.loading = "lazy"; image.width = picture.width; image.height = picture.height;
    link.append(image); gallery.append(link);
  }
  return gallery;
}

// Anchor human messages at their journal position; reading a queued input only
// adds a separate receipt beside its Model call. Neither changes model context.
function appendConversationMessage(node,event) {
  const view=event.run_id ? viewFor(event.run_id) : event.run_id===null ? null : state.current;
  (view?.steps || $("messages")).append(node);
  node.dataset.sequence=event.sequence || 0;
  $("welcome").hidden=true;
  scheduleConversationDetails();
}
function messageMarkdown(content,images,owner) {
  const assets=new Map();
  for(const image of images || [])if(/^[a-f0-9]{64}$/.test(image.sha256 || "")) {
    for(const path of [image.path,image.source,image.name].filter(Boolean)) {
      assets.set(path,image.sha256);assets.set(path.replace(/^\/workspace\//,"").replace(/^\.\//,""),image.sha256);
    }
  }
  const imageURL=value=>{
    if(typeof value!=="string")return null;
    let path=value.trim().replace(/^sandbox:/,"");
    try{path=decodeURIComponent(path);}catch{return null;}
    const digest=assets.get(path) || assets.get(path.replace(/^\/workspace\//,"").replace(/^\.\//,""));
    if(digest)return assetURL(digest,owner);
    if(/^https?:\/\//i.test(path)){try{const url=new URL(path);return url.username || url.password ? null : url.href;}catch{return null;}}
    return null;
  };
  try{return AgentMarkdown.render(content,{imageURL});}catch{return text("pre",content,"markdown-fallback");}
}
// All readable correspondence and model prose share the same Markdown/media
// renderer. Only their surrounding cards decide routing and detail visibility.
function renderMessageBody(body,content,images,owner=state.agentId) {
  body.replaceChildren(messageMarkdown(content,images,owner));
  const shown=new Set([...body.querySelectorAll("img")].map(image=>new URL(image.src,location.href).pathname));
  const remaining=(images || []).filter(image=>!shown.has(new URL(assetURL(image.sha256,owner),location.href).pathname));
  if(remaining.length)body.append(imageGallery(remaining,owner));
}
function ccBadge(cc,{copy=false,targets=[]}={}) {
  const recipients=copy ? targets : cc;
  const badge=text('span',copy ? 'CC · 抄送给 ' : 'CC ','peer-role message-cc');
  recipients.forEach((identifier,index)=>{
    if(index)badge.append(document.createTextNode(', '));
    const name=text('span',agentName(identifier));name.dataset.agentName=identifier;badge.append(name);
  });
  if(copy)badge.append(document.createTextNode(' · 知情副本'));
  badge.title=copy ? '原消息发给上方 To 收件人，抄送只供知情，不是发给抄送者的直接任务。' : '发送者选择同步知情的 Agent';
  return badge;
}

function renderHumanMessage(event) {
  if(event.source!==state.agentId)return;
  const identifier=event.message_id || event.id;
  if([...$("messages").querySelectorAll(".human-message")].some(node=>node.dataset.messageId===identifier))return;
  const card=text("article","","human-message conversation-message");card.dataset.messageId=identifier;
  card.tabIndex=-1;
  card.dataset.source=event.source;card.dataset.target="user";card.dataset.timestamp=event.timestamp || 0;
  const heading=text("div","","human-message-heading conversation-heading"),stamp=text("time","","message-time");
  const avatar=text("span","","conversation-avatar");avatar.append(agentIconNode(event.source));
  const author=text("strong",agentName(event.source),"conversation-author");author.dataset.agentName=event.source;
  heading.append(avatar,author);if(event.cc?.length)heading.append(ccBadge(event.cc));heading.append(stamp);
  setMessageTime(stamp,event.timestamp);
  const body=text("div","","human-message-body final-response");renderMessageBody(body,event.content,event.images,event.source);
  card.append(heading,body);
  appendConversationMessage(card,event);
}
function rememberHumanMessage(event,source,live=false) {
  const identifier=event.message_id || event.id;
  if(!identifier)return;
  const known=state.humanMessages.has(identifier),message={...event,source:event.source || source};
  state.humanMessages.set(identifier,message);renderHumanMessage(message);
  if(live && !known) {
    const reading=!document.hidden && (state.agentId===message.source || isAllAgentsView());
    if(!reading) {
      const unread=unreadHumanMessages.get(state.session.id) || new Map();
      unread.set(identifier,message.source);unreadHumanMessages.set(state.session.id,unread);
    }
    const destination={sessionId:state.session.id,agentId:message.source,messageId:identifier};
    toast(`${agentIcon(message.source)} ${agentName(message.source)} 给你发来消息`,()=>revealHumanMessage(destination));completionSound();paintMessageBadge(state.session);inspector();
  }
}
function currentDraft() {
  const id = state.session ? `${state.session.id}/${state.agentId}` : null;
  if (!id) return {text:"",images:[]};
  if (!state.drafts.has(id)) state.drafts.set(id,{text:"",images:[]});
  return state.drafts.get(id);
}
function acceptingAgents() {
  return state.session ? [{id:"master",status:agentStatus(state.session)},...(state.session.agents || [])].filter(agent=>acceptingAgentStates.has(agentStatus(agent))) : [];
}
function renderAttachments() {
  const draft = currentDraft(); $("attachment-tray").replaceChildren(); $("attachment-tray").hidden = !draft.images.length;
  for (const item of draft.images) {
    const card = text("div", "", "attachment-preview"); card.dataset.status = item.status;
    const image = document.createElement("img"); image.src = item.preview; image.alt = item.file.name;
    const label = text("span", item.status === "uploading" ? "上传中…" : item.status === "error" ? item.error : item.name, "attachment-label"); label.title = label.textContent;
    const remove = text("button", "×", "remove-attachment"); remove.type="button"; remove.setAttribute("aria-label",`移除 ${item.file.name}`);
    remove.addEventListener("click",()=>{draft.images=draft.images.filter(value=>value!==item);URL.revokeObjectURL(item.preview);renderAttachments();controls();});
    card.append(image,label,remove);$("attachment-tray").append(card);
  }
}
async function pasteImages(files) {
  if(state.session?.read_only){toast('Session 为 stop 只读状态，请先 Resume 后上传图片。');return;}
  const session = state.session, agentId=state.agentId, uploadURL=agentAPI("/uploads");
  if (!session || state.switching) {toast("请先打开一个 Session，再添加图片。");return;}
  const target=isAllAgentsView() ? state.session : agentId==="master" ? session : state.agentDetail;
  if(!humanInputStates.has(agentStatus(target))){toast("当前 Agent 暂不可接收留言。");return;}

  const draft = currentDraft();
  for (const file of files) {
    if (draft.images.length >= 8) {toast("每条消息最多添加 8 张图片。");break;}
    const item = {file,preview:URL.createObjectURL(file),status:"uploading"}; draft.images.push(item);
    if (state.session?.id===session.id && state.agentId===agentId) {renderAttachments();controls();}
    try {
      // Clipboard bytes travel unchanged to the server. Format/dimensions and
      // all filesystem operations are handled there; previews are presentation.
      const response = await fetch(uploadURL,{method:"POST",headers:{"Content-Type":file.type || "application/octet-stream","X-Agent-Token":state.config.csrf,"X-recoding-Page":state.pageId,"X-recoding-Document":state.documentId,"X-File-Name":encodeURIComponent(file.name)},body:file});
      const result = await response.json();if(!response.ok)throw new Error(result.error || `HTTP ${response.status}`);
      Object.assign(item,result,{status:"ready"});
    } catch(error) {item.status="error";item.error=error.message;toast(error.message);}
    if (state.session?.id===session.id && state.agentId===agentId) {renderAttachments();controls();}
  }
}

const seconds = value => typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${value.toFixed(2)}s` : "—";
const tokenCount = value => Number.isSafeInteger(value) && value >= 0 ? value : undefined;
const countText = value => tokenCount(value)?.toLocaleString() ?? "—";
const messageDateFormat = new Intl.DateTimeFormat("zh-CN", {year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
function timestampMillis(value) {
  const stamp = typeof value === "number" ? value * 1000 : typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(stamp) ? stamp : null;
}
function setMessageTime(node, value, label) {
  const stamp = timestampMillis(value);node.hidden = stamp === null;
  if (stamp === null) return;
  node.dateTime = new Date(stamp).toISOString();node.title = node.dateTime;
  node.textContent = `${label ? label+" · " : ""}${messageDateFormat.format(stamp)}`;
}
function compactTokenCount(value) {
  const count=tokenCount(value);
  if(count===undefined)return "—";
  return count<1000 ? String(count) : count<1000000 ? `${(count/1000).toFixed(1)}k` : `${(count/1000000).toFixed(2)}M`;
}
function agentTokenUsage(id) {
  const usage={...(state.session?.agent_token_usage?.[id] || {})};
  const target=id==="master" ? state.session : id===state.agentId ? state.agentDetail : null;
  const context=target?.context || {};
  if(tokenCount(usage.input_tokens)===undefined)usage.input_tokens=context.last_input_tokens;
  if(tokenCount(usage.compaction_saved_tokens)===undefined)usage.compaction_saved_tokens=context.compaction_saved_tokens;
  return usage;
}
const transportPhases = {preparing: "准备请求", waiting_headers: "等待响应头", headers_received: "收到响应头", reading_body: "接收响应", stream_output: "收到首个模型输出", parsing: "解析响应", complete: "响应已接收", retry_wait: "等待重试", error: "请求失败", cancelled: "请求已取消"};
const modelExcerpts = new WeakMap();
const modelExcerptObserver = new ResizeObserver(entries => {
  for (const entry of entries) {
    const excerpt = modelExcerpts.get(entry.target);
    if (excerpt) sizeModelExcerpt(excerpt);
  }
});

function sizeModelExcerpt({section, content, toggle}) {
  const expanded = section.dataset.expanded === "true";
  toggle.hidden = section.hidden || (!expanded && content.scrollHeight <= content.clientHeight + 1);
  toggle.textContent = expanded ? "收起" : "展开全部";
  toggle.setAttribute("aria-expanded", String(expanded));
}

function observeModelExcerpt(section, content, toggle) {
  const excerpt = {section, content, toggle};
  modelExcerpts.set(content, excerpt);modelExcerptObserver.observe(content);
  toggle.addEventListener("click", () => {
    section.dataset.expanded = section.dataset.expanded === "true" ? "false" : "true";
    sizeModelExcerpt(excerpt);
  });
}

function sizeModelExcerpts(model) {
  sizeModelExcerpt({section:model.summary, content:model.summaryContent, toggle:model.summaryToggle});
  sizeModelExcerpt({section:model.responseDetail, content:model.response, toggle:model.outputToggle});
}

function renderModelSummary(model) {
  const content = typeof model.reasoningContent === "string" ? model.reasoningContent.trim() : "";
  model.summary.hidden = !content;
  if (!content) {
    if (model.summaryContent.hasChildNodes()) model.summaryContent.replaceChildren();
    model.renderedReasoning = null;
  } else if (model.renderedReasoning !== content) {
    model.summaryContent.replaceChildren(markdown(content));
    model.renderedReasoning = content;
  }
  if(!state.replaying)sizeModelExcerpts(model);
}

function modelUsage(model) {
  const usage = {...model.transport.usage, ...model.usage};
  return {
    input: tokenCount(usage.prompt_tokens), output: tokenCount(usage.completion_tokens), total: tokenCount(usage.total_tokens),
    thinking: tokenCount(model.transport.reasoning_tokens) ?? tokenCount(usage.completion_tokens_details?.reasoning_tokens),
    cached: tokenCount(model.transport.cached_tokens) ?? tokenCount(usage.prompt_tokens_details?.cached_tokens),
  };
}

function modelFor(view, event) {
  const step = event.step || view.stats.models || 1;
  if (view.models.has(step)) return view.models.get(step);
  const node = text("section", "", "model-call"), heading = text("div", "", "model-call-heading");
  node.dataset.step=step;node.id=`model-${view.steps.id}-${step}`;
  const name = text("div", "", "model-call-name");
  name.append(text("span", "✦", "model-symbol"), text("strong", `Model call #${step}`), text("span", `Iteration ${step}`, "model-iteration"));
  const modelTag = text("span", "", "model-tag");modelTag.hidden = true;name.append(modelTag);
  const status = text("span", "running", "model-status"), duration = text("span", "", "model-duration");
  heading.append(name, status, duration);
  const summary = text("section", "", "model-summary");
  summary.hidden = true;
  summary.dataset.expanded = "false";
  summary.setAttribute("aria-label", "API 返回的 Thinking 文本");
  summary.title="API 返回的可读 Thinking 文本；较长内容可展开";
  const summaryToggle = text("button", "展开全部", "text-button model-summary-toggle"); summaryToggle.type = "button"; summaryToggle.hidden = true;
  const summaryContent = text("div", "", "model-summary-content final-response");
  summaryContent.id = `reasoning-${view.steps.id}-${step}`;
  summaryToggle.setAttribute("aria-controls", summaryContent.id);
  summary.append(summaryContent, summaryToggle);
  const phase = text("div", "", "model-phase"), metrics = text("div", "", "model-metrics"), values = {};
  for (const [key, label, hint] of [
    ["input", "Input", "API usage.prompt_tokens"],
    ["output", "Output", "API usage.completion_tokens；Thinking 可能包含在其中，不重复相加"],
    ["thinking", "Thinking", "API completion_tokens_details.reasoning_tokens；缺失时不估算"],
    ["cached", "Cache hit", "API prompt_tokens_details.cached_tokens；缺失不等于零"],
    ["speed", "Speed", "API Output tokens / 本次 Model call 总耗时（含等待），不额外加 Thinking；完成且有用量时才计算。"],
  ]) {
    const item = text("div", "", "model-metric"); item.title = hint;
    values[key] = text("strong", "—"); item.append(text("span", label), values[key]); metrics.append(item);
  }
  const timing = text("div", "", "model-timing"), note = text("div", "", "model-usage-note");
  const diagnostics = text("details", "", "model-diagnostics"), diagnosticRows = document.createElement("dl");
  diagnostics.append(text("summary", "详情"), phase, timing, note, diagnosticRows);
  const response = text("div", "", "model-response rich-message-body final-response"), responseDetail=text("section","","model-output");
  responseDetail.hidden=true;responseDetail.dataset.expanded="false";
  responseDetail.setAttribute("aria-label", "本次 Model call 的说明");
  const outputHeading=text("div","","model-output-heading"),outputToggle=text("button","展开全部","text-button model-output-toggle");
  outputToggle.type="button";outputToggle.hidden=true;
  response.id=`output-${view.steps.id}-${step}`;response.dataset.step=step;
  outputToggle.setAttribute("aria-controls",response.id);
  outputHeading.append(text("span","本步说明","model-output-label"),outputToggle);
  responseDetail.append(outputHeading,response);
  node.append(heading, responseDetail, summary, metrics, diagnostics); view.steps.append(node);
  const timestamp = Number.isFinite(event.timestamp) ? event.timestamp * 1000 : Date.now();
  const model = {node, modelTag, modelId: event.model || null, statusNode: status, duration, phase, values, timing, note, diagnosticRows, response, responseDetail, outputToggle,
    summary, summaryContent, summaryToggle, reasoningContent: null,
    started: timestamp - (event.type === "model_start" ? 0 : (event.elapsed_seconds ?? event.elapsed ?? 0) * 1000),
    status: "running", elapsed: null, transport: {}, usage: {}, context: {}, outcome: ""};
  observeModelExcerpt(summary, summaryContent, summaryToggle);
  observeModelExcerpt(responseDetail, response, outputToggle);
  view.models.set(step, model); view.currentModel = model;
  const batch=view.inputBatches.get(step);
  if(batch)view.steps.insertBefore(batch.node,node);
  return model;
}

function inputBatchFor(view,step) {
  if(view.inputBatches.has(step))return view.inputBatches.get(step);
  const node=text("section","","model-inputs"),heading=text("div","","model-inputs-heading");
  node.dataset.step=step;node.setAttribute("aria-label",`Model call #${step} 的追加输入`);
  const target=text("button",`INPUT → Model call #${step}`,"text-button input-target"),count=text("span","","input-count");
  target.type="button";
  target.addEventListener("click",()=>view.models.get(step)?.node.scrollIntoView({block:"nearest"}));
  heading.append(target,count);node.append(heading);
  const batch={node,count,items:new Map()};view.inputBatches.set(step,batch);
  view.steps.insertBefore(node,view.models.get(step)?.node || null);
  return batch;
}

function refreshInputBatch(batch) {
  const identifiers=new Set([...batch.items.values()].flatMap(item=>item.ids));
  batch.count.textContent=`${identifiers.size} messages`;
  batch.node.hidden=!batch.items.size;
}



function placeInput(view,event,node,ids,inputOrder=event.input_order) {
  const batch=inputBatchFor(view,event.step),previous=inputPlacements.get(node);
  if(previous && previous!==batch){previous.items.delete(node);refreshInputBatch(previous);}
  const order=Number.isInteger(inputOrder) ? inputOrder : (event.sequence || 0);
  batch.items.set(node,{ids,order,sequence:event.sequence || 0});inputPlacements.set(node,batch);
  // Queue order is supplied by the loop, not inferred from arrival timestamps.
  const ordered=[...batch.items].sort(([,a],[,b])=>a.order-b.order || a.sequence-b.sequence);
  for(const [card] of ordered)batch.node.append(card);
  const model=view.models.get(event.step);
  if(model)view.steps.insertBefore(batch.node,model.node);
  refreshInputBatch(batch);
  scheduleConversationDetails();
}

function pendingInputArea() {
  let node=$("messages").querySelector(":scope > .pending-inputs");
  if(!node){node=text("section","","pending-inputs");node.append(text("div","QUEUED · 等待 Agent 读取","pending-inputs-heading"));$("messages").append(node);}
  return node;
}

function refreshPendingInputs() {
  const pending=$("messages").querySelector(":scope > .pending-inputs");
  if(pending){pending.hidden=!pending.querySelector(".user-followup,.system-message");if(!pending.hidden)$("messages").append(pending);}
}

function renderModel(model) {
  const transport = model.transport, usage = modelUsage(model);
  model.modelId ||= transport.model || transport.response_model || null;
  model.modelTag.hidden = !model.modelId;
  model.modelTag.textContent = model.modelId || "";
  AgentStatistics.identifyModel(model.node,model.modelId);
  AgentStatistics.identifyModel(model.modelTag, model.modelId);
  model.modelTag.title = `请求模型：${model.modelId || "未知"}${transport.response_model ? ` · API 回报：${transport.response_model}` : ""}`;
  model.node.dataset.status = model.status;
  model.statusNode.textContent = transport.phase === "retry_wait" && model.status === "running"
    ? `retry ${transport.next_attempt}/${transport.max_attempts} · ${transport.retry_delay_seconds}s`
    : `${model.status}${transport.retry_count ? ` · ${transport.retry_count} ${transport.retry_count===1 ? "retry" : "retries"}` : ""}`;
  model.duration.textContent = seconds(model.elapsed ?? (model.status === "running" ? Math.max(0, (Date.now() - model.started) / 1000) : undefined));
  model.phase.textContent = transport.error_category === "empty_response"
    ? "网关在返回模型内容前关闭了连接；未提供上游错误原因"
    : model.outcome || transportPhases[transport.phase] || "等待模型响应";
  renderModelSummary(model);
  for (const key of Object.keys(model.values)) if (key !== "speed") model.values[key].textContent = countText(usage[key]);
  const rate = AgentStatistics.outputRate(usage.output, model.elapsed);
  model.values.speed.textContent = model.status === "done" && rate !== null ? `${rate.toFixed(1)} tok/s` : "—";
  model.note.textContent = usage.thinking === undefined
    ? (model.status === "running" ? "Token 用量等待 API 回报 · Thinking 未提供" : "Thinking 未提供；缺失用量不估算")
    : "Token 用量由 API 回报 · Thinking 与 Output 不重复相加";
  const timings = [];
  if (transport.http_status) timings.push(`HTTP ${transport.http_status} · headers ${seconds(transport.headers_seconds)}`);
  timings.push(`First output ${seconds(transport.first_output_seconds)}`, `First text ${seconds(transport.first_text_seconds)}`);
  model.timing.textContent = timings.join(" · ");
  model.timing.title = "First output 是首个非空正文、推理或工具增量；First text 是首个正文增量。两者都不是纯推理耗时。";
  const context = model.context, rows = [
    ["Model", model.modelId], ["API response model", transport.response_model],
    ["Context", context.messages === undefined ? null : `${context.messages} messages · ${countText(context.context_chars)} chars${context.images ? ` · ${context.images} images` : ""}`],
    ["Total tokens", usage.total], ["Finish reason", transport.finish_reason],
    ["Request ID", transport.request_id], ["Call ID", transport.call_id],
    ["Attempt", transport.attempt ? `${transport.attempt}/${transport.max_attempts}` : null],
    ["Previous failures", transport.failed_attempts?.length ? transport.failed_attempts.map(item=>`#${item.attempt} · ${item.http_status || item.error_category} · ${item.error || ""}`).join("\n") : null],
    ["Request", transport.request_bytes === undefined ? null : `${countText(transport.request_bytes)} B`],
    ["Response", transport.response_bytes === undefined ? null : `${countText(transport.response_bytes)} B`],
    ["SSE events", transport.stream_events], ["Heartbeats", transport.heartbeat_count],
    ["Model output received", transport.model_output_received === undefined ? null : String(transport.model_output_received)],
    ["SSE [DONE] received", transport.sse_done_received === undefined ? null : String(transport.sse_done_received)],
    ["Pending event bytes", transport.pending_event_bytes],
    ["History models", transport.history_models ? Object.entries(transport.history_models).map(([name, count]) => `${name} × ${count}`).join(" · ") : null],
    ["Foreign history", transport.foreign_assistant_messages === undefined ? null
      : `${transport.foreign_assistant_messages} assistant messages · ${transport.foreign_tool_calls ?? 0} tool calls converted`],
    ["Error category", transport.error_category], ["Error", transport.error],
  ];
  model.diagnosticRows.replaceChildren();
  for (const [label, value] of rows) {
    if (value === undefined || value === null || value === "") continue;
    model.diagnosticRows.append(text("dt", label), text("dd", typeof value === "number" ? value.toLocaleString() : value));
  }
}

function finishPendingModels(view, status, timestamp) {
  for(const card of view.budgetReviews?.values() || [])if(card.dataset.status==="pending") {
    card.dataset.status="cancelled";card.textContent=card.textContent.replace("等待你的选择","已取消确认 · Agent 已停止");
  }
  for (const model of view.models.values()) if (model.status === "running") {
    model.status = status;
    model.elapsed = Math.max(0, ((timestamp ? timestamp * 1000 : Date.now()) - model.started) / 1000);
    model.outcome = status === "cancelled" ? "当前 Model call 已取消" : "当前 Model call 未完成";
    renderModel(model);
  }
  for(const [id,card] of view.compactions || [])if(card.node.dataset.status==="running"){
    renderContextCompaction(view,{type:"context_compaction_error",id,cancelled:status==="cancelled",
      error:"Context compression interrupted; original history retained",elapsed:Math.max(0,(timestamp || Date.now()/1000)-card.started)});
  }
}

function viewFor(runId) {
  if(state.views.has(runId))return state.views.get(runId);
  const node=text("article","","agent-run"),heading=text("div","","run-heading");
  const name=agentName(state.agentId);
  const stamp=text("time","","message-time"),status=text("span","running","run-status");
  const author=text("strong",name);author.dataset.agentName=state.agentId;
  heading.append(agentIconNode(state.agentId,"agent-symbol"),author,text("span","Run","run-caption"),stamp,status);
  const steps=text("div","","steps");steps.id=`iterations-${runId}`;node.dataset.runId=runId;
  node.append(heading,steps);$("messages").append(node);
  const view={node,heading,stamp,status,steps,tools:new Map(),models:new Map(),inputBatches:new Map(),peerMessages:new Map(),
    stats:{iterations:0,models:0,tools:0,drawn:0,viewed:0},viewedImages:new Set(),started:Date.now(),phase:"正在开始"};
  state.views.set(runId,view);state.current=view;refreshPendingInputs();$("welcome").hidden=true;
  return view;
}

function controls() {
  const session = state.session, target=composerAgent();
  const browsing=Boolean(session?.read_only);
  const resumeRequired=Boolean(session && (browsing || stoppedAgentStates.has(agentStatus(session))));
  $("composer-body").classList.toggle("is-resume-blocked",resumeRequired);
  $("composer").inert=resumeRequired;
  $("prompt").disabled=resumeRequired;
  $("composer").setAttribute("aria-disabled",String(resumeRequired));
  const status=agentStatus(target),taskRunning=session && (loopIsLive(session) || session.agents?.some(loopIsLive));
  $("discover-sessions").disabled=!state.config || state.serverChanged || state.changingRoot || state.discoveringSessions;
  $("send").hidden = false;$("stop").hidden=false;
  $("stop").disabled=!taskRunning || state.switching || state.serverChanged || state.stopping || session?.status==="stopping";
  const draft = currentDraft();
  const canReceive=isAllAgentsView() ? allRecipientAgents().length>0 : humanInputStates.has(status),queuedOnly=!isAllAgentsView() && !acceptingAgentStates.has(status);
  $("send").disabled=state.switching || draft.submitting || !session || state.serverChanged || !canReceive || (!$("prompt").value.trim() && !draft.images.length) || draft.images.some(item=>item.status!=="ready");
  if(resumeRequired)$("send").disabled=true;
  $("send").replaceChildren(document.createTextNode("发送 "),text("span","↑"));
  $("send").title=canReceive ? queuedOnly ? "保存为未读留言，恢复后读取；不会自动恢复 Agent" : "发送给所选 Agent 的输入队列" : isAllAgentsView() ? "当前没有存活的接收者" : "当前 Agent 暂不可接收留言";
  const recipient=composerRecipient(),name=allRecipientGroups.has(recipient) ? `@${allRecipientGroups.get(recipient)}` : agentName(recipient);
  renderAllRecipientControls();
  $("prompt").placeholder=canReceive ? queuedOnly ? `给 ${name} 留言，恢复后读取…` : `发给 ${name}，或粘贴图片…` : isAllAgentsView() ? "当前没有存活的接收者…" : `${name} 暂不可接收留言…`;
  $("input-delivery-note").hidden=!session || !queuedOnly;
  $("input-delivery-note").textContent=`${name} · 留言将保存为未读，恢复后读取；发送不会自动恢复。`;

  $("rename-session").disabled = state.switching || !session || session.read_only;
  $("close-session").disabled = state.switching || !session;
  $("close-session").textContent=session?.read_only ? '关闭视图' : '关闭环境';
  $("status-dot").className="status-dot"+(status==="sleeping" ? " sleeping" : status==="running" || status==="starting" ? " running" : status==="failed" ? " error" : "");
  const phases={starting:"正在启动常驻 Loop…",running:state.current?.phase || "Loop 正在运行",sleeping:"休眠中 · 新消息会唤醒",stopping:"正在停止 · 可以留言",stopped:"已停止 · 留言在恢复后读取",failed:target?.error || "Loop 失败 · 可以留言"};
  $("runtime-label").textContent=state.switching ? "正在打开 Session…" : session ? phases[status] || status : "新建或选择一个 Session，开始工作";
  $("runtime-label").title=name+" · "+status;
  if(session?.read_only){
    $("runtime-label").textContent='Session · stop · 只读浏览';
    $("prompt").placeholder='只读浏览；Resume Master 后才能输入…';
    $("send").title='Session 为 stop 只读状态，请先 Resume';
    $("input-delivery-note").hidden=false;$("input-delivery-note").textContent='stop 只读：不发送消息、不上传图片、不修改存档。设置仅暂存，Resume 后应用。';
  }
  if(status!=="running" && status!=="sleeping")$("elapsed").textContent="";
  $("session-resume-notice").hidden=!resumeRequired || state.switching;
  $("session-resume-title").textContent=session?.agents?.some(loopIsLive) ? "Master 已停止" : "当前仅浏览 Session";
  $("session-resume-copy").textContent=session?.agents?.some(loopIsLive)
    ? "Master 已停止，其余 Agent 继续运行。是否 Resume Master？"
    : "此 Session 已停止。是否 Resume Master？sub-agent 由 Master 决定恢复。";
  $("continue-task").textContent=state.resuming ? "正在 Resume…" : "Resume Master";
  if(browsing)$("session-resume-copy").textContent="Resume Master 后才能输入和发送消息。sub-agent 由 Master 决定恢复。";
  $("continue-task").disabled=state.switching || state.resuming || state.serverChanged;
}

function positionComposerHelp() {
  const help = $("composer-help");
  if (!help.matches(":popover-open")) return;
  const anchor = $("composer-help-button").getBoundingClientRect();
  const gap = 10, margin = 12;
  const left = Math.max(margin, Math.min(anchor.left, innerWidth - help.offsetWidth - margin));
  const above = anchor.top - help.offsetHeight - gap;
  const top = above >= margin ? above : Math.max(margin, Math.min(anchor.bottom + gap, innerHeight - help.offsetHeight - margin));
  help.style.left = `${left}px`;help.style.top = `${top}px`;
}
$("composer-help").addEventListener("toggle", positionComposerHelp);
$("composer-help-button").addEventListener("click", event => {
  // Open and measure synchronously, so the first frame is already anchored.
  event.preventDefault();
  const help = $("composer-help");
  if (help.matches(":popover-open")) help.hidePopover();
  else {help.showPopover();positionComposerHelp();}
});
window.addEventListener("resize", positionComposerHelp);
window.addEventListener("panelschange", positionComposerHelp);

function modelOptions(select, selected) {
  const choices = state.config.demo ? ["offline-demo"] : [...new Set([...(state.config.models || [state.config.model]), selected].filter(Boolean))];
  const signature = choices.join("\n");
  if (select.dataset.choices !== signature) {
    select.replaceChildren(...choices.map(id => {const option = AgentStatistics.identifyModel(text("option", id), id);option.value = id;return option;}));
    select.dataset.choices = signature;
  }
  select.value = selected;
  AgentStatistics.identifyModel(select, selected);
}

$("session-model").addEventListener("change", event => AgentStatistics.identifyModel(event.currentTarget, event.currentTarget.value));

function renderModelSelection() {
  const session = state.session, selected = state.config.demo ? "offline-demo" : session?.model || state.config.model;
  modelOptions($("model-name"), selected);
  AgentStatistics.identifyModel($("model-name").closest(".model-card"), selected);
  $("model-name").disabled = !session || state.switching || state.config.demo || modelSaves.has(session.id) || !state.config.capabilities?.model_switching;
  const active = session?.active_model;
  $("model-mode").textContent = state.config.demo ? "固定响应 · 真实执行工具" : !session ? "选择 Session 后可切换模型"
    : modelSaves.has(session.id) ? "正在保存模型…" : active && active !== selected ? `当前 ${active} · 下一次 Model call 切换`
    : "全体 Agent · 下次调用生效";
  if (session && !state.config.demo && !modelSaves.has(session.id) && active && active !== selected) {
    const label = AgentStatistics.identifyModel(text("span", active, "model-inline"), active);
    $("model-mode").replaceChildren(document.createTextNode("当前 "), label, document.createTextNode(" · 下次切换"));
  }
  $("model-mode").title = "同一 Session 的所有 Agent 共用此模型与 API。切换对每个 Agent 的下一次 Model call 生效，包括运行中和恢复后的 sub；当前响应继续使用原模型，历史来源保持不变。";
}

let addingNode=false,pendingNodeId=null,pendingNodeSession=null;
function renderRemoteNodes(){
  const session=state.session,list=$("remote-node-list");list.replaceChildren();
  $("add-node").disabled=!session || session.read_only || addingNode || state.switching;
  for(const node of session?.nodes || []){
    if(node.id==="local")continue;
    const row=text("details","","remote-node"),summary=document.createElement("summary");
    const label=text("span",node.name || node.id,"remote-node-name"),status=text("small",node.status,"remote-node-status");
    summary.append(label,status,text("small",node.workspace,"remote-node-path"));row.append(summary);
    row.title=node.workspace;
    const actions=text("div","","remote-node-actions");
    for(const [action,caption] of [["connect","连接"],["remove","移除"]]){
      const button=text("button",caption,"text-button");button.type="button";button.disabled=!!session.read_only;
      button.addEventListener("click",async()=>{button.disabled=true;try{
        const result=await api(`/api/sessions/${session.id}/nodes`,{action,node_id:node.id});
        if(state.session?.id===session.id){state.session.nodes=result.nodes;renderRemoteNodes();}
      }catch(error){toast(error.message);}finally{button.disabled=!!state.session?.read_only;}});
      actions.append(button);
    }
    row.append(actions);list.append(row);
  }
}
$("add-node").addEventListener("click",()=>{$("node-form").reset();$("node-progress").textContent="";$("node-dialog").showModal();});
async function dismissNode(){
  if(addingNode && pendingNodeId && pendingNodeSession){
    try{await api(`/api/sessions/${pendingNodeSession}/nodes`,{action:"cancel_add",node_id:pendingNodeId});}catch(error){toast(error.message);}
  }
  $("node-dialog").close();
}
for(const id of ["dismiss-node","cancel-node"])$(id).addEventListener("click",dismissNode);
$("node-dialog").addEventListener("cancel",event=>{if(addingNode){event.preventDefault();dismissNode();}});
$("node-form").addEventListener("submit",async event=>{
  event.preventDefault();const session=state.session;if(!session || session.read_only || addingNode)return;
  pendingNodeId="node-"+crypto.randomUUID().replaceAll("-","").slice(0,8);pendingNodeSession=session.id;
  const body={node_id:pendingNodeId,name:$("node-name").value.trim(),ssh_target:$("node-target").value.trim(),workspace:$("node-workspace").value.trim(),
    execution_mode:$("node-mode").value,python:$("node-python").value.trim()};
  if($("node-ssh-config").value.trim())body.ssh_config=$("node-ssh-config").value.trim();
  addingNode=true;$("submit-node").disabled=true;$("cancel-node").disabled=false;renderRemoteNodes();
  $("node-progress").textContent="正在验证 SSH 与目录、部署 Runtime 并检查能力…";
  try{
    const result=await api(`/api/sessions/${session.id}/nodes`,body);
    if(state.session?.id===session.id){state.session.nodes=result.nodes;renderRemoteNodes();}
    $("node-dialog").close();
  }catch(error){$("node-progress").textContent=error.message;}
  finally{addingNode=false;pendingNodeId=pendingNodeSession=null;$("submit-node").disabled=false;$("cancel-node").disabled=false;renderRemoteNodes();}
});
function markToolNode(tool,node){
  if(!tool || !node?.node_id || node.node_id==="local")return;
  const summary=tool.node.querySelector("summary");let badge=summary.querySelector(".tool-node");
  if(!badge){badge=text("span","","tool-node");summary.insertBefore(badge,tool.status);}
  badge.textContent=node.node_name || node.node_id;badge.title=`${node.node_name || node.node_id} · Node ${node.node_id}`;
}

function overlaySessionSettings(session,values){
  Object.assign(session,values);session.config={...session.config,...values};session.settings={...session.settings,...values};
  if(session.team)session.team={...session.team,...values};
}
$("execution-mode").addEventListener("change",event=>{
  if(state.session?.read_only)stageSessionSettings({execution_mode:event.target.value});
});
function stageSessionSettings(values){
  const session=state.session;if(!session)return;
  state.pendingSettings.set(session.id,{...state.pendingSettings.get(session.id),...values});
  overlaySessionSettings(session,values);inspector();
}
$("model-name").addEventListener("change", async () => {
  const session = state.session, selected = $("model-name").value;
  if (!session || modelSaves.has(session.id)) return;
  if(session.read_only){stageSessionSettings({model:selected});return;}
  modelSaves.add(session.id);renderModelSelection();
  try {
    const result = await api(`/api/sessions/${encodeURIComponent(session.id)}/model`, {model: selected});
    session.model = result.model;
    session.active_model ??= result.active_model;
  } catch (error) {toast(error.message);}
  finally {modelSaves.delete(session.id);if (state.session?.id === session.id) inspector();}
});
$("agent-limit-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const session=state.session,input=$("agent-limit"),max=Number(input.value);
  if(!session || state.savingAgentLimit || !input.reportValidity() || !Number.isInteger(max) || max<0 || max>99)return;
  if(session.read_only){stageSessionSettings({max_subagents:max});return;}
  state.savingAgentLimit=true;renderTeam();
  try {
    const result=await api(`/api/sessions/${encodeURIComponent(session.id)}/agent-limit`,{max_subagents:max});
    session.max_subagents=result.max_subagents;session.config={...session.config,max_subagents:result.max_subagents};
    if(session.team)session.team={...session.team,max_subagents:result.max_subagents};
    if(state.session===session)toast(result.queued ? `并发上限已改为 ${result.max_subagents}；系统通知已排队。` : `并发上限 ${result.max_subagents}`);
  }catch(error){toast(error.message);}
  finally{state.savingAgentLimit=false;if(state.session===session)inspector();}
});
$("agent-limit").addEventListener("change",()=>$("agent-limit-form").requestSubmit());

$("idle-sleep-form").addEventListener("submit",async event=>{
  event.preventDefault();
  const session=state.session,input=$("idle-sleep"),seconds=Number(input.value);
  input.setCustomValidity(Number.isFinite(seconds)&&seconds>=.1&&seconds<=3600 ? "" : "请输入 0.1–3600 的秒数");
  if(!session || state.savingIdleSleep || !input.reportValidity())return;
  if(session.read_only){stageSessionSettings({idle_sleep_seconds:seconds});return;}
  state.savingIdleSleep=true;renderTeam();
  try {
    const result=await api(`/api/sessions/${encodeURIComponent(session.id)}/idle-sleep`,{idle_sleep_seconds:seconds});
    session.idle_sleep_seconds=result.idle_sleep_seconds;
    if(session.team)session.team={...session.team,idle_sleep_seconds:result.idle_sleep_seconds};
    if(state.session===session)toast(`默认空闲休眠已改为 ${result.idle_sleep_seconds}s。`);
  }catch(error){toast(error.message);}
  finally{state.savingIdleSleep=false;if(state.session===session)inspector();}
});
$("idle-sleep").addEventListener("change",()=>$("idle-sleep-form").requestSubmit());
$("idle-sleep").addEventListener("input",()=>$("idle-sleep").setCustomValidity(""));

function renderSessionSpeed(){
  const created=timestampMillis(state.session?.created_at);
  const samples=[...modelRates.values()].map(sample=>({...sample,agent_name:`${agentIcon(sample.agent_id || "master")} ${agentName(sample.agent_id || "master")}`}));
  AgentStatistics.renderSpeed($("session-speed"),samples,state.session?.id,{startedAt:created===null ? undefined : created/1000});
}
function inspector() {
  const session = state.session;
  renderModelSelection();renderRemoteNodes();
  const mode=session?.execution_mode || session?.config?.execution_mode || "sandbox";
  $("execution-mode").value=mode;
  $("execution-mode").disabled=!session?.read_only || state.switching || state.resuming;
  $("execution-mode-note").textContent=mode==="host" ? "Host 无隔离；当前用户权限。停止后可切换" : "Sandbox 隔离执行；停止后可切换";
  if (!session) {modelRates.clear();renderSessionSpeed();}
  $("session-store-path").textContent = session?.session_directory || "对话、图片和设置保存在 .recoding。";
  $("copy-archive").disabled=!session?.session_directory;
  $("session-title").textContent = session?.name || "选择一个 Session";
  $("session-title").title = session?.directory || "";
  $("session-workspace").textContent = session?.directory || state.config.session_root;
  $("session-workspace").title = session?.directory || state.config.session_root;
  $("workspace-path").textContent = session?.directory || state.config.session_root;
  controls();
  renderTeam();
  renderToolInventory();
  renderBackgroundJobs();
  renderContextMaintenance();
}

function inventorySelection() {
  const id=state.agentId==="all" ? "master" : state.agentId;
  return {id,target:id==="master" ? state.session : state.agentDetail,key:state.session ? `${state.session.id}/${id}` : null};
}
const activeJobStates=new Set(["submitting","starting","queued","running","stopping","cancelling","unresponsive"]);
const jobRows=new Map();
let jobsSession=null;

// Jobs are live execution resources, not an execution-history browser. A persistent
// process occupies one row even with several queued calls; an idle one is ready.
function currentJobs(session) {
  if(!session)return [];
  const jobs=[];
  for(const kind of ["python","bash"]) {
    const pool=session[kind+"_pool"] || {},processes=new Map(Object.entries(pool.processes || {}));
    const executions=new Map((session.executions || []).filter(task=>task.kind===kind).map(task=>[task.id,task]));
    for(const task of Object.values(pool.executions || {}))executions.set(task.id,task);
    const byProcess=new Map();
    for(const task of executions.values()) {
      if(!activeJobStates.has(task.status))continue;
      const id=task.process_id || task.id;
      if(!byProcess.has(id))byProcess.set(id,[]);
      byProcess.get(id).push(task);
      if(!processes.has(id))processes.set(id,{id,name:task.name,owner:task.owner || task.source,
        creator:task.submitted_by || task.source,lifetime:task.lifetime || "temporary",status:task.status,created_at:task.created_at});
    }
    for(const process of processes.values()) {
      if(["closed","failed"].includes(process.status))continue;
      const tasks=(byProcess.get(process.id) || []).sort((a,b)=>a.created_at-b.created_at);
      const execution=tasks.find(task=>task.id===process.active_execution) || tasks.find(task=>task.status!=="queued") || tasks[0];
      const phase=execution || process.active_execution || process.queued>0 || activeJobStates.has(process.status) ? "run" : process.status==="ready" && process.lifetime!=="temporary" ? "ready" : null;
      if(phase)jobs.push({key:`${kind}/${process.id}`,kind,process,execution,phase,queued:Math.max(process.queued || 0,tasks.filter(task=>task.status==="queued").length)});
    }
  }
  return jobs.sort((a,b)=>(a.phase===b.phase ? (b.process.created_at || 0)-(a.process.created_at || 0) : a.phase==="run" ? -1 : 1));
}

function jobLogView(task,sessionId) {
  const area=text("div","","job-log"),meta=text("small",task.id,"job-id"),output=text("pre","","job-output"),button=text("button","读取输出","text-button");
  button.type="button";let stdout=0,stderr=0,loading=false,loaded=false;
  async function load() {
    if(loading)return;loading=true;button.disabled=true;
    try {
      const query=new URLSearchParams({stdout_offset:stdout,stderr_offset:stderr,max_bytes:16000});
      const data=await api(`/api/sessions/${encodeURIComponent(sessionId)}/agents/master/executions/${task.id}/output?${query}`);
      if(state.session?.id!==sessionId || !area.isConnected)return;
      if(!loaded)output.textContent="";
      if(data.stdout)output.textContent+=data.stdout;
      if(data.stderr)output.textContent+=(output.textContent ? "\n[stderr]\n" : "[stderr]\n")+data.stderr;
      if(data.execution.error)output.textContent+="\n"+data.execution.error;
      if(output.textContent.length>64000)output.textContent="[较早输出已折叠，可从日志文件读取]\n"+output.textContent.slice(-64000);
      stdout=data.stdout_offset;stderr=data.stderr_offset;loaded=true;
      meta.textContent=`${task.id.slice(0,8)} · stdout ${stdout} B / stderr ${stderr} B`;
      button.textContent=data.stdout_has_more || data.stderr_has_more ? "读取下一段" : "读取新输出";
      if(!output.textContent)output.textContent="（暂无输出）\n";
    } catch(error){output.textContent=error.message;}finally{loading=false;button.disabled=false;}
  }
  button.addEventListener("click",load);area.append(meta,output,button);
  return {node:area,load:()=>{if(!loaded)load();}};
}

function refreshExecutionTimes() {
  if(document.hidden)return;
  for(const row of jobRows.values()) {
    const task=row.execution;
    if(!task) {row.elapsed.textContent="空闲";row.elapsed.title="当前没有调用，不计进程存活时间";continue;}
    const start=timestampMillis(task.started_at);
    if(start===null) {
      row.elapsed.textContent=task.status==="queued" ? "排队中" : "启动中";
      row.elapsed.title="本次调用尚未开始执行，不计排队时间";continue;
    }
    const end=timestampMillis(task.finished_at) ?? Date.now(),elapsed=Math.max(0,(end-start)/1000);
    row.elapsed.textContent=elapsed<60 ? `${elapsed.toFixed(1)}s` : elapsed<3600 ? `${Math.floor(elapsed/60)}m${Math.floor(elapsed%60)}s` : `${Math.floor(elapsed/3600)}h${Math.floor(elapsed%3600/60)}m`;
    row.elapsed.title="本次 Execution 执行耗时；不含排队和 Process 空闲时间";
  }
}

function renderBackgroundJobs() {
  const container=$("background-jobs"),sessionId=state.session?.id;
  container.hidden=!sessionId;
  if(jobsSession!==sessionId) {
    jobsSession=sessionId;$("jobs-panel").open=false;jobRows.clear();$("job-list").replaceChildren();
  }
  const jobs=currentJobs(state.session),live=new Set(jobs.map(job=>job.key));
  $("jobs-run-count").textContent=jobs.filter(job=>job.phase==="run").length;
  $("jobs-ready-count").textContent=jobs.filter(job=>job.phase==="ready").length;
  for(const [key,row] of jobRows)if(!live.has(key)){row.node.remove();jobRows.delete(key);}
  const list=$("job-list");list.querySelector(".jobs-empty")?.remove();
  for(const job of jobs) {
    let row=jobRows.get(job.key);
    if(!row) {
      const node=text("details","","job-row"),summary=document.createElement("summary"),kind=text("span","","job-kind"),name=text("span","","job-name"),status=text("small","","job-state"),meta=text("small","","job-id");
      const tags=text("span","","job-call-meta"),mode=text("span","—","job-call-mode"),lifetime=text("span","","job-lifetime"),elapsed=text("span","","job-call-elapsed");
      tags.append(mode,lifetime,elapsed);summary.append(kind,name,status,tags);node.append(summary,meta);
      row={node,kind,name,status,meta,mode,lifetime,elapsed,execution:null,executionId:null,log:null};jobRows.set(job.key,row);
      node.addEventListener("toggle",()=>{if(node.open)row.log?.load();});
    }
    const process=job.process,task=job.execution;
    row.node.dataset.status=job.phase;row.node.dataset.processId=process.id;row.node.dataset.kind=job.kind;
    row.kind.textContent=(job.kind==="python" ? "Python" : "Bash")+(process.node_id && process.node_id!=="local" ? ` · ${process.node_name || process.node_id}` : "");row.name.textContent=process.name || task?.name || row.kind.textContent;
    row.status.textContent=job.phase;row.status.title=task?.status || process.status;
    row.execution=task || null;
    row.mode.textContent=task && ["sync","async"].includes(task.mode) ? task.mode : "—";
    row.mode.dataset.mode=task?.mode || "";
    row.mode.title=task ? "本次调用的等待方式：sync 等待结果；async 提交后返回" : "当前没有调用，sync/async 不属于进程属性";
    const lifetime=process.lifetime || task?.lifetime;
    row.lifetime.textContent=lifetime==="persistent" ? "有状态" : lifetime==="temporary" ? "无状态" : "—";
    row.lifetime.title=lifetime==="persistent" ? "显式持久进程，保留状态供后续调用复用" : "临时进程，本次调用结束后回收";
    row.meta.textContent=`${process.id.slice(0,8)} · owner ${agentIcon(process.owner)} ${agentName(process.owner)}${job.queued ? ` · ${job.queued} 排队` : ""}`;
    row.meta.title=`process ${process.id} · creator ${agentName(process.creator || process.owner)} · ${task?.status || process.status}`;
    if(row.executionId!==(task?.id || null)) {
      row.log?.node.remove();row.executionId=task?.id || null;row.log=task ? jobLogView(task,sessionId) : null;
      if(row.log){row.node.append(row.log.node);if(row.node.open)row.log.load();}
    }
  }
  // Move only rows whose order changed, preserving expanded logs and read cursors.
  jobs.forEach((job,index)=>{const node=jobRows.get(job.key).node;if(list.children[index]!==node)list.insertBefore(node,list.children[index] || null);});
  if(!jobs.length)list.append(text("p","暂无 run / ready 进程","jobs-empty"));
  refreshExecutionTimes();
}
function renderToolInventory() {
  const {id,target,key}=inventorySelection(),report=target?.tool_inventory;
  const container=$("tool-inventory-content"),button=$("refresh-tools"),label=$("tool-inventory-agent");
  label.textContent=target ? agentName(id) : "";
  label.title=target ? `${isAllAgentsView() ? "All agents 页面显示 " : ""}${agentName(id)} 的执行环境检查结果` : "";
  if(report?.checked_at>=(inventoryRefreshes.get(key) || Infinity))inventoryRefreshes.delete(key);
  button.disabled=!target || state.session?.read_only || !acceptingAgentStates.has(agentStatus(target)) || inventoryRefreshes.has(key) || state.serverChanged || state.switching;
  button.textContent=inventoryRefreshes.has(key) ? "检查中…" : "↻ 检查";
  if(container.report===report && container.dataset.agentKey===key && container.dataset.agentStatus===agentStatus(target))return;
  container.report=report;container.dataset.agentKey=key;container.dataset.agentStatus=agentStatus(target);
  if(!report){container.textContent=target ? acceptingAgentStates.has(agentStatus(target)) ? "等待首次检查 · 不阻塞 Agent" : "尚无检查记录 · Resume 后检查" : "打开 Session 后显示检查结果";return;}
  const expanded=container.querySelector(".inventory-details")?.open;
  container.replaceChildren();container.dataset.level=report.level || "unknown";
  if(report.status!=="ready") {
    container.append(text("p","检查未完成，暂不评分；不会按缺包处理。","inventory-note"),text("small",report.error || "探针暂不可用","inventory-error"));return;
  }
  const details=text("details","","inventory-details");details.open=Boolean(expanded);
  const summary=text("summary","","inventory-summary"),score=text("strong",`${report.score}/100`,"inventory-score");
  summary.append(score,text("span",({severe:"缺失较多",partial:"部分缺失",good:"较完整"})[report.level] || "能力覆盖率"),text("span","详情","inventory-more"));details.append(summary);
  const body=text("div","","inventory-body");
  body.append(text("p",report.assessment,"inventory-note"));
  for(const group of report.categories || []) {
    const row=text("div","","inventory-category");
    row.append(text("strong",`${group.name} ${group.score}/${group.maximum}`));
    for(const item of group.capabilities || [])row.append(text("div",`${item.available.length ? "✓" : "○"} ${item.name} · ${item.available.length ? item.available.join(" / ") : item.alternatives.join(" / ")}`,item.available.length ? "inventory-present" : "inventory-missing"));
    body.append(row);
  }
  if(report.level==="severe") {
    const advice=text("div","","inventory-host-advice");
    advice.append(text("strong","建议用户在 Host 配置常用工具"),text("p",`请为 ${report.python} 安装或提供：${(report.host_packages || []).join(", ")}。`));
    advice.append(text("small","Session 自行安装仍然可用。Host 安装后刷新检查；首次建立 user-site 时，需重新启动运行环境以挂载。已导入旧包的 Pool 进程按需删除并重新创建。"));body.append(advice);
  }
  const packages=text("details","","inventory-packages");packages.append(text("summary",`${countText(report.package_count)} 个发行版条目 · 包版本与来源${report.packages_truncated ? "（仅展示前 512 项）" : ""}`));
  const list=text("div","","inventory-package-list");
  for(const item of report.packages || [])list.append(text("div",`${item.name} ${item.version} · ${item.scope}`));
  packages.append(list);body.append(packages);
  if(report.startup_hooks_skipped)body.append(text("p","未执行 .pth 启动代码；依赖这些钩子的包可能无法被发现。","inventory-note"));
  details.append(body);container.append(details);
  const bar=text("div","","inventory-bar"),fill=text("span","");fill.style.width=`${Math.max(0,Math.min(100,report.score))}%`;bar.append(fill);container.append(bar);
  if(report.level==="severe")container.append(text("p","常用扩展工具缺失较多，建议在 Host 安装；展开查看缺失项和建议包。","inventory-warning"));
  const stamp=text("small",`${new Date(report.checked_at*1000).toLocaleTimeString()} 检查 · ${acceptingAgentStates.has(agentStatus(target)) ? "每 5 分钟" : "最近结果，Agent 已停止"}`,"inventory-checked");
  stamp.title=new Date(report.checked_at*1000).toLocaleString();container.append(stamp);
}
$("refresh-tools").addEventListener("click",async()=>{
  const {id,key}=inventorySelection();if(!key)return;
  inventoryRefreshes.set(key,Date.now()/1000);renderToolInventory();
  try{await api(agentAPI("/tool-inventory",id),{});}
  catch(error){inventoryRefreshes.delete(key);toast(error.message);}
  finally{renderToolInventory();setTimeout(()=>{inventoryRefreshes.delete(key);renderToolInventory();},10000);}
});

function renderContextMaintenance() {
  const panel=$("context-maintenance"),active=(state.session?.context_compactions || []).filter(item=>["queued","running","cancelling"].includes(item.status));
  panel.replaceChildren();panel.hidden=!active.length;
  for(const item of active){
    const row=text("div","","context-maintenance-row"),copy=text("div","","context-maintenance-copy");
    copy.append(text("strong",`${item.status==="queued" ? "等待后台压缩" : item.status==="cancelling" ? "正在取消压缩" : "正在后台压缩"} · ${agentName(item.agent_id) || item.name}`),
      text("small","Agent 继续工作 · 摘要就绪后在下一次 Model call 使用"));
    const cancel=text("button","取消","text-button");cancel.type="button";cancel.disabled=state.session.read_only || item.status==="cancelling";
    cancel.setAttribute("aria-label",`取消 ${item.name} 的上下文压缩`);
    cancel.addEventListener("click",async()=>{
      cancel.disabled=true;
      try{await api(`/api/sessions/${encodeURIComponent(state.session.id)}/context-compaction`,{request_id:item.request_id});}
      catch(error){toast(error.message);cancel.disabled=false;}
    });
    row.append(copy,cancel);panel.append(row);
  }
}


$("copy-archive").addEventListener("click",async()=>{
  if(!state.session?.session_directory)return;
  try{await navigator.clipboard.writeText(state.session.session_directory);toast("Session archive 路径已复制");}
  catch{toast("请选中存档路径后手动复制。");}
});

function agentTabContent(node, agent) {
  if(agent.id==="all"){
    node.append(text("span","◎","agent-tab-icon"),text("strong","All agents"));
    node.title="汇总本 Session 的人机消息与 Agent 间消息";return;
  }
  const model=agent.active_model || agent.model;
  const modelName=AgentStatistics.modelPresentation(model).short_label || model;
  const brief=agent.id==="master" ? "常驻协调 Agent · 显式消息通信" : "独立常驻 Loop · 可与其他 Agent 通信";
  const copy=text("span","","agent-tab-copy"),meta=text("span","","agent-tab-meta");
  meta.append(text("small",agent.status,"agent-tab-status"));
  if(modelName)meta.append(AgentStatistics.identifyModel(text("small",modelName,"model-inline"),model));
  copy.append(text("strong",agent.name),meta,text("span",brief || "独立子任务","agent-tab-brief"));
  node.append(agentIconNode(agent.id,"agent-tab-icon"));
  node.append(copy);node.title=[agent.name,agent.status,model,brief].filter(Boolean).join(" · ");
}

function layoutAgentTabs() {
  const layout=state.agentTabLayout;if(!layout)return;
  const {subTabs,selected}=layout;
  const current=[...subTabs.children].find(tab=>tab.dataset.agentId===selected);
  if(current) {
    const top=current.offsetTop,bottom=top+current.offsetHeight;
    if(top<subTabs.scrollTop)subTabs.scrollTop=top;
    else if(bottom>subTabs.scrollTop+subTabs.clientHeight)subTabs.scrollTop=bottom-subTabs.clientHeight;
  }
}
let agentTabsResize;
new ResizeObserver(()=>{cancelAnimationFrame(agentTabsResize);agentTabsResize=requestAnimationFrame(layoutAgentTabs);}).observe($("agent-tabs"));

$("toggle-agent-settings").addEventListener("click",()=>{
  const panel=$("agent-settings-panel");panel.hidden=!panel.hidden;
  $("toggle-agent-settings").setAttribute("aria-expanded",String(!panel.hidden));
});

function renderTeam() {
  const session = state.session, agents = session?.agents || [], selected = state.agentId || "master";
  $("agent-navigation").hidden = !session;
  $("agent-directory").hidden = !session;
  if($("agent-settings-panel").dataset.session!==(session?.id || "")){
    $("agent-settings-panel").dataset.session=session?.id || "";
    $("agent-settings-panel").hidden=true;$("toggle-agent-settings").setAttribute("aria-expanded","false");
  }
  renderAllMessageFilters();
  if(!session){$("agent-tabs").replaceChildren();$("agent-list").replaceChildren();$("composer-wrap").hidden=true;return;}
  const limit=Number.isInteger(session?.team?.max_subagents) ? session.team.max_subagents : session?.config?.max_subagents ?? session?.max_subagents ?? 16;
  if(document.activeElement!==$("agent-limit") && !state.savingAgentLimit)$("agent-limit").value=limit;
  $("agent-limit").disabled=!session || state.switching || state.savingAgentLimit || state.serverChanged;
  $("save-agent-limit").disabled=$("agent-limit").disabled;
  const idle=session?.team?.idle_sleep_seconds ?? session?.idle_sleep_seconds ?? session?.config?.idle_sleep_seconds ?? 10;
  if(document.activeElement!==$("idle-sleep") && !state.savingIdleSleep)$("idle-sleep").value=idle;
  $("idle-sleep").disabled=!session || state.switching || state.savingIdleSleep || state.serverChanged;
  $("save-idle-sleep").disabled=$("idle-sleep").disabled;
  $("composer-wrap").hidden = !session;
  const live = liveAgentStates;
  const ordered=[...agents].sort((a,b)=>(timestampMillis(a.created_at) || 0)-(timestampMillis(b.created_at) || 0));
  const active=ordered.filter(agent=>live.has(agent.status)), ended=ordered.filter(agent=>!live.has(agent.status));
  const master={id:"master",name:"Master",icon:agentIcon("master"),status:agentStatus(session),created_at:session?.created_at,model:session?.active_model || session?.model};
  const current=ended.find(agent=>agent.id===selected);
  const tabs=$("agent-tabs"), subRows=current ? [...active,current] : active,tabRows=[{id:"all",name:"All agents",status:"messages"},master,...subRows];
  const tabSignature=JSON.stringify([selected,...tabRows.map(agent=>[agent.id,agent.name,agent.icon,agent.status,agent.model])]);
  if(tabs.dataset.signature!==tabSignature) {
    const previousTop=tabs.querySelector(".agent-sub-tabs")?.scrollTop || 0;tabs.replaceChildren();
    const subTabs=text("div","","agent-sub-tabs");subTabs.setAttribute("role","presentation");subTabs.hidden=!subRows.length;
    for(const agent of tabRows) {
      const tab=text("button","","agent-tab");tab.type="button";tab.dataset.agentId=agent.id;tab.dataset.status=agent.status;
      tab.setAttribute("role","tab");tab.setAttribute("aria-selected",String(agent.id===selected));
      agentTabContent(tab,agent);
      tab.addEventListener("click",()=>selectAgent(agent.id).catch(error=>toast(error.message)));
      (["master","all"].includes(agent.id) ? tabs : subTabs).append(tab);
    }
    tabs.append(subTabs);tabs.dataset.signature=tabSignature;
    subTabs.scrollTop=previousTop;state.agentTabLayout={subTabs,selected};layoutAgentTabs();
  }
  const signature=JSON.stringify([selected,master,...active,...ended].map(row=>typeof row==="string"?row:[row.id,row.name,row.icon,row.status,row.created_at,agentTokenUsage(row.id),unreadCount(session.id,row.id)]));
  const list=$("agent-list");
  if(list.dataset.signature!==signature) {
    const top=list.scrollTop;list.replaceChildren();
    const appendAgent=agent=>{
      const row=text("button","","agent-list-item");row.type="button";row.dataset.agentId=agent.id;row.dataset.status=agent.status;
      row.setAttribute("aria-current",String(agent.id===selected));
      row.dataset.resumable=String(Boolean(agent.resumable));
      row.title=`${agent.name} · ${agent.status}${agent.resumable ? " · 由 Master 恢复此 Agent" : ""}${agent.error ? `\n${agent.error}` : ""}`;
      const main=text("span","","agent-list-main"),identity=text("span","","agent-list-identity"),tokens=text("span","","agent-token-usage");
      identity.append(agentIconNode(agent.id),text("strong",agent.name));
      const usage=agentTokenUsage(agent.id);
      for(const [metric,label,value,estimated] of [["input","Input",usage.input_tokens,false],["saved","已压缩",usage.compaction_saved_tokens,true]]) {
        const item=text("span",`${label} ${tokenCount(value)!==undefined && estimated ? "≈" : ""}${compactTokenCount(value)}`,"agent-token-metric");
        item.dataset.metric=metric;
        item.title=metric==="input" ? `最近一次正常 Model call 的 API Input tokens：${countText(value)}；含缓存命中，不是累计量` : `已压缩约 ${countText(value)} tokens：原始历史与当前有效请求投影的差值；按同一本地口径估算，不重复累加。— 表示尚无记录`;
        tokens.append(item);
      }
      main.append(identity,tokens);
      const unread=unreadCount(session.id,agent.id);
      if(unread){identity.append(text("span",`${unread} 新消息`,"agent-unread"));row.classList.add("has-unread");}
      row.append(text("span","","agent-list-dot"),main,text("small",`${agent.status}${agent.resumable ? " ↻" : ""}`));
      row.addEventListener("click",()=>selectAgent(agent.id).catch(error=>toast(error.message)));list.append(row);
    };
    appendAgent(master);
    for(const [label,group] of [["LIVE",[...active].reverse()],["STOPPED",[...ended].reverse()]])if(group.length) {
      list.append(text("div",`${label} · ${group.length}`,"agent-list-label"));group.forEach(appendAgent);
    }
    list.dataset.signature=signature;list.scrollTop=top;
  }
  $("agent-running-count").textContent=active.length+Number(loopIsLive(session));
  $("agent-running-count").parentElement.title="含 Master；启动、执行、休眠及停止回收中的 Agent 均计入，已停止和失败的不计入。";
  $("agent-count").textContent=1+agents.length;
  $("agent-count").parentElement.title="当前 Session 累计创建，包含 Master、已停止和失败的 Agent；Resume 不重复计数。";
  const descriptor=agents.find(agent => agent.id === selected);
  $("viewing-agent-name").replaceChildren(agentIconNode(selected),document.createTextNode(descriptor?.name || "Master"));
  $("viewing-agent-status").textContent=descriptor?.resumable ? `${descriptor.status} · 由 Master 恢复` : descriptor?.status || master.status;
  $("agent-current").hidden=tabRows.some(agent=>agent.id===selected);
  const totals=session?.team;
  $("team-summary").hidden=!totals;
  if (totals && $("team-summary").dataset.signature !== JSON.stringify(totals)) {
    const expanded=$("team-summary").querySelector("details")?.open;
    $("team-summary").dataset.signature=JSON.stringify(totals);
    const details=text("details","","team-summary-details");details.open=Boolean(expanded);
    const heading=text("summary","","team-summary-heading");
    heading.append(text("span","ALL AGENTS · 累计","section-label"),text("span",`${totals.active}/${totals.max_subagents} live`,"team-live"),text("span","详情","team-more"));
    details.append(heading);
    const grid=text("div","","team-stat-grid");
    for (const [label,value] of [["常驻 sub-agents / 上限",`${totals.active} / ${totals.max_subagents}`],
      ["已创建 / 已停止 / 失败",`${totals.created} / ${totals.stopped || 0} / ${totals.failed || 0}`],["Iterations",totals.iterations],
      ["Model calls",totals.model_calls],["Tool calls",totals.tool_calls],["累计出图 / 看图",`${totals.drawn} / ${totals.viewed}`],
      ["累计 Input / Output",`${countText(totals.prompt_tokens)} / ${countText(totals.completion_tokens)}`],["累计 Model 耗时",seconds(totals.model_elapsed)],["累计 Tool 耗时",seconds(totals.tool_elapsed)]]) {
      const item=text("div","");item.append(text("strong",value),text("span",label));grid.append(item);
    }
    grid.title="Session 自创建以来所有 Agent、所有运行记录的累计量；包含已停止 Agent，恢复不会清零。并发累计耗时可能超过实际墙钟时间。";
    details.append(grid);
    const brief=text("div","","team-token-totals");
    for(const [label,key] of [["INPUT tokens","prompt_tokens"],["OUTPUT tokens","completion_tokens"]]) {
      const metric=text("div","","team-token-total");metric.dataset.metric=key;
      metric.append(text("strong",countText(totals[key])),text("span",label));brief.append(metric);
      if(key==="prompt_tokens") {
        const known=tokenCount(totals.cache_known_input_tokens) ?? 0,cached=tokenCount(totals.cached_tokens) ?? 0;
        const unknown=Math.max(tokenCount(totals.cache_unknown_input_tokens) ?? 0,(tokenCount(totals.prompt_tokens) ?? 0)-known);
        const cache=text("div","","team-input-cache");
        const hit=text("span",`Cache hit ${countText(cached)}${known ? ` · ${(cached/known*100).toFixed(1)}%` : ""}`,"team-cache-hit");
        hit.title=unknown ? "命中率仅针对 API 已报告缓存数据的 Input tokens。" : "API 报告的累计缓存命中 tokens / 累计 Input tokens。";
        const miss=text("span",`${unknown ? "已知 Non-cache" : "Non-cache"} ${countText(totals.uncached_tokens ?? 0)}`,"team-cache-miss");
        miss.title="非缓存 Input = 同一调用的 prompt_tokens − cached_tokens；不把缺失缓存报告当作零命中。";
        cache.append(hit,miss);
        if(unknown)cache.append(text("span",`缓存未报告 ${countText(unknown)} input`,"team-cache-unknown"));
        metric.append(cache);
      }
    }
    brief.title="Session 自创建以来，所有 Agent 的 API Input / Output tokens 累计；Thinking 不重复加到 Output。展开查看调用、工具、图片和耗时统计。";
    $("team-summary").replaceChildren(details,brief);
  }
}

function toolArguments(event) {
  if(event.arguments && typeof event.arguments==="object")return event.arguments;
  try{return JSON.parse(event.arguments || "{}");}catch{return {};}
}
function applyLoopEvent(agent,event) {
  if(!agent)return;
  if(event.type==="tool_inventory")agent.tool_inventory=event.inventory;
  if(event.type==="execution_updated" && event.execution) {
    agent.executions=[event.execution,...(agent.executions || []).filter(task=>task.id!==event.execution.id)].sort((a,b)=>b.created_at-a.created_at).slice(0,100);
  }
  for(const key of ["context","active_model"])if(event[key])agent[key]=event[key];
  if(event.type==="session_ready")Object.assign(agent,{status:"starting",gpu:event.gpu,history:event.history});
  if(["loop_started","loop_start","model_start","tool_end"].includes(event.type) && agent.status!=="stopping")Object.assign(agent,{status:"running",sleep:null,error:null});
  if(event.type==="loop_stopped")Object.assign(agent,{status:event.status,error:event.error || null,sleep:null});
  if(event.type==="loop_end")Object.assign(agent,{status:event.reason==="error" ? "failed" : "stopped",sleep:null});
  if(event.type==="session_closed" && !stoppedAgentStates.has(agent.status))Object.assign(agent,{status:event.error ? "failed" : "stopped",error:event.error,sleep:null});
  if(["session_error","loop_error"].includes(event.type))Object.assign(agent,{status:event.error ? "failed" : "stopping",error:event.error || null,sleep:null});
  if(event.type==="tool_start" && event.name==="sleep" && agent.status!=="stopping") {
    const args=toolArguments(event),started=event.timestamp || Date.now()/1000;
    agent.status="sleeping";agent.sleep={call_id:event.call_id,seconds:args.seconds,started,
      deadline:Number.isFinite(args.seconds) ? started+args.seconds : null,wake_on_message:true};
  }
  if(event.type==="model_changed")agent.model=event.model;
}

function applyMainEvent(event) {applyLoopEvent(state.session,event);}

const managementTools=new Set(["create_agent","stop_agent","resume_agent","rename_agent","set_agent_icon"]);
const stateTools=new Set(["list_agents"]);
const observationTools=new Set(["read_global_messages","listen_global_messages"]);
const messageTools=new Set(["send_agent_message","broadcast_agent_message","send_user_message"]);
const coordinationTools=new Set([...managementTools,...stateTools,...observationTools,...messageTools,"sleep"]);
function agentName(identifier) {
  return identifier === "broadcast" ? "全部其他运行 Agents" : identifier === "system" ? "System" : identifier === "user" ? "You" : identifier === "master" ? "Master" : state.session?.agents?.find(agent=>agent.id===identifier)?.name || String(identifier || "Agent").slice(0,8);
}
function agentIcon(identifier) {
  return identifier==="master" ? state.session?.master_icon || "🧭" : state.session?.agents?.find(agent=>agent.id===identifier)?.icon || "🤖";
}
function agentIconNode(identifier,className="") {
  const icon=text("span",agentIcon(identifier),`agent-icon ${className}`.trim());
  icon.dataset.agentIcon=identifier;icon.setAttribute("aria-hidden","true");return icon;
}
function refreshAgentIdentities(force=false) {
  const signature=JSON.stringify([state.session?.id,state.session?.master_icon,(state.session?.agents || []).map(agent=>[agent.id,agent.name,agent.icon])]);
  if(!force && signature===state.agentIdentitySignature)return;
  state.agentIdentitySignature=signature;
  for(const node of document.querySelectorAll("[data-agent-icon]"))node.textContent=agentIcon(node.dataset.agentIcon);
  for(const node of document.querySelectorAll("[data-agent-name]"))node.textContent=agentName(node.dataset.agentName);
  if(!force)renderSessionSpeed();
}
function messageRoute(source,target,{revealSource=null}={}) {
  const route=text("span","","message-route");
  for(const [index,identifier] of [source,target].entries()) {
    if(index)route.append(text("span","→","message-arrow"));
    if(Array.isArray(identifier)){const group=text("span","","message-recipients");for(const [i,id] of identifier.entries()){if(i)group.append(document.createTextNode(", "));const route=messageRoute(source,id);group.append(route.lastChild);}route.append(group);continue;}
    const realAgent=!["user","system","broadcast"].includes(identifier);
    const label=text(realAgent ? "button" : "strong",agentName(identifier),`message-party ${index ? "message-recipient" : "message-sender"}`);
    if(realAgent) {
      const name=text("span",agentName(identifier));name.dataset.agentName=identifier;
      label.replaceChildren(agentIconNode(identifier),name);
      label.type="button";label.classList.add("text-button");
      label.addEventListener("click",e=>{e.preventDefault();e.stopPropagation();
        (index===0 && revealSource ? revealSource() : selectAgent(identifier)).catch(error=>toast(error.message));});
    }
    route.append(label);
  }
  return route;
}
function coordinationBrief(name,args,result) {
  if(result?.error)return `ERROR · ${result.error}`;
  if(name==="create_agent")return `CREATE · ${result?.name || args.name || "Agent"} · independent loop`;
  if(name==="rename_agent")return `RENAME · ${agentName(args.agent_id)} → ${result?.name || args.name}`;
  if(name==="set_agent_icon")return `ICON · ${agentName(args.agent_id)} → ${result?.icon || args.icon}`;
  if(name==="send_agent_message")return `SEND · ${agentName(state.agentId)} → ${(args.agent_ids || [args.agent_id]).map(agentName).join(", ")} · ${result ? "已入队 · " : ""}${args.message || ""}`;
  if(name==="send_user_message")return `TO YOU · ${agentName(state.agentId)} → You · ${args.message || ""}`;
  if(name==="broadcast_agent_message")return `BROADCAST · ${agentName(state.agentId)} → 全部其他运行 Agents${result ? ` · ${result.recipients?.length || 0} recipients` : ""} · ${args.message || ""}`;
  if(name==="stop_agent")return `STOP · ${agentName(args.agent_id)}`;
  if(name==="resume_agent")return `RESUME · ${agentName(args.agent_id)} · 保留对话 · 重建运行环境`;
  if(name==="list_agents")return `AGENTS · ${result ? `${result.agents?.length || 0} · 仅状态元数据` : "读取当前 Agent 列表"}`;
  if(name==="read_global_messages")return result ? `读取全局消息 · ${result.messages?.length ?? 1} 条 · 只读，不消费收件箱` : "读取全局消息 · 正文按需进入当前 context";
  if(name==="listen_global_messages")return result ? `全局监听 · ${result.reason} · 游标 ${result.read_after}` : `监听全局新消息 · 最长 ${args.seconds ?? 30}s · 不读取正文`;
  if(name==="sleep") {
    if(!result)return `${args.seconds ?? 0}s · 新消息可唤醒`;
    return result.reason==="message" ? "WAKE · 收到新消息" : `${result.reason || "done"} · 休眠结束`;
  }

  return name;
}
function renderPeerMessage(view,{key,source,targets,content,images,timestamp,messageId,messageIds,broadcast=false,cc=[],delivery,to},before=null) {
  if(view.peerMessages.has(key))return view.peerMessages.get(key);
  const outgoing=source===state.agentId,node=text("article","","peer-message conversation-message");
  node.dataset.direction=outgoing ? "outgoing" : "incoming";node.dataset.messageId=messageId || key;
  node.dataset.messageIds=(messageIds || [messageId || key]).join(",");node.tabIndex=-1;
  node.dataset.source=source;node.dataset.target=Array.isArray(targets) ? targets.join(",") : targets;
  const heading=text("div","","conversation-heading peer-message-heading"),stamp=text("time","","message-time");
  const avatar=text("span","","conversation-avatar");avatar.append(agentIconNode(source));
  const role=text("span",`${outgoing ? "me" : "others"} · ${broadcast ? "广播" : "Agent 对话"}`,"peer-role");
  const copy=delivery==='cc';
  if(copy){node.classList.add('cc-message');role.textContent=`${outgoing ? 'me' : 'others'} · Agent 抄送`;}
  heading.append(avatar,messageRoute(source,copy ? to : targets),role);
  if(cc.length)heading.append(ccBadge(cc,{copy,targets:Array.isArray(targets) ? targets : [targets]}));
  heading.append(stamp);setMessageTime(stamp,timestamp);
  const body=text("div","","final-response peer-message-body");renderMessageBody(body,content,images);
  node.append(heading,body);view.steps.insertBefore(node,before);
  const message={node,body};view.peerMessages.set(key,message);return message;
}
function renderAgentActivity(view,event) {
  if(event.type!=="agent_messages")return;
  const batch=inputBatchFor(view,event.step);
  for(const message of event.messages) {
    const chat=renderPeerMessage(view,{key:`in:${message.id}`,messageId:message.id,source:message.source,targets:message.target,
      content:typeof message.content==="string" ? message.content : JSON.stringify(message.content),images:message.images,timestamp:message.timestamp || event.timestamp,
      cc:message.cc,delivery:message.delivery,to:message.to},batch.node);
    // The conversation stays at its arrival/read boundary. A separate compact
    // receipt identifies the consuming Model call without repeating its prose.
    const receipt=text("details","","peer-input-receipt");receipt.dataset.messageId=message.id;
    const summary=text("summary","READ · "),sender=text("span",agentName(message.source));sender.dataset.agentName=message.source;
    summary.append(agentIconNode(message.source),document.createTextNode(" "),sender,document.createTextNode(`${message.delivery==='cc' ? ' → '+message.to.map(agentName).join(', ')+' · CC → me' : ' → me'} · Model call #${event.step}`));receipt.append(summary);
    const jump=text("button","查看这条 Agent 对话 ↑","text-button");jump.type="button";
    jump.addEventListener("click",()=>chat.node.scrollIntoView({block:"center",behavior:"instant"}));receipt.append(jump);
    placeInput(view,event,receipt,[message.id],message.input_order);
  }
}

function renderContextCompaction(view,event){
  view.compactions ||= new Map();
  let card=view.compactions.get(event.id);
  if(!card){
    const node=text("details","","context-compaction"),heading=text("summary","Context compression"),meta=text("div","","compaction-meta");
    const memory=text("div","","final-response compaction-memory");node.append(heading,meta,memory);view.steps.append(node);
    card={node,heading,meta,memory,started:event.timestamp || Date.now()/1000};view.compactions.set(event.id,card);
  }
  card.event={...card.event,...event};const value=card.event;
  card.node.dataset.status=event.cancelled ? "cancelled" : event.type==="context_compaction_end" ? "done" : event.type==="context_compaction_error" ? "error" : "running";
  const sizes=`${countText(value.before?.context_chars)} → ${countText(value.after?.context_chars)} chars`;
  card.heading.textContent=`Context compression · ${card.node.dataset.status}${value.after ? ` · ${sizes}` : ""}${value.elapsed ? ` · ${seconds(value.elapsed)}` : ""}`;
  const usage=value.usage || {};
  card.meta.textContent=[value.model,`原始消息 1–${Math.max(0,(value.prefix_count || 1)-1)} · 原历史保留`,
    value.after ? `${value.after.messages} request messages · ${value.after.images} images retained` : value.phase || "正在生成历史记忆…",
    usage.prompt_tokens!==undefined ? `Input ${countText(usage.prompt_tokens)} · Output ${countText(usage.completion_tokens)}` : "",
    value.error ? `${value.error}\n继续使用此前有效 context，未删除历史。` : ""].filter(Boolean).join("\n");
  if(value.summary)card.memory.replaceChildren(markdown(value.summary));
  if(card.node.dataset.status==="running")view.phase="Context compression · 正在整理较早步骤";
}

function dispatch(event, live = true) {
  if (event.type === "resync") {install(event.snapshot,{agentId:state.agentId});return;}
  if (event.sequence <= state.sequence) return;
  state.sequence=event.sequence;state.rootHistory.set(event.sequence,event);
  state.rootBacklog.set(event.sequence,event);while(state.rootBacklog.size>800)state.rootBacklog.delete(state.rootBacklog.keys().next().value);
  if(event.type==="system_message_queued" && event.target!=="master") {
    if(state.agentId===event.target)renderSystemInput(event);
    return;
  }
  if(event.type==="nodes_updated"){
    state.session.nodes=event.nodes || [];if(live)renderRemoteNodes();return;
  }
  if(event.type==='tool_service_updated' && ['python_pool','bash_pool'].includes(event.service)) {
    const pool=state.session[event.service] ||= {processes:{},executions:{}};
    pool[event.kind==='process' ? 'processes' : 'executions'][event.record.id]=event.record;
    if(event.kind==='execution') {
      applyLoopEvent(state.session,{type:'execution_updated',execution:event.record});
      if(state.agentId!=="master" && [event.record.source,event.record.owner].includes(state.agentId))
        applyLoopEvent(state.agentDetail,{type:'execution_updated',execution:event.record});
    }
    if(live)inspector();return;
  }
  if(event.type === "agents_updated") {
    state.session.agents=event.agents;state.session.team=event.team;
    if(event.agent_token_usage)state.session.agent_token_usage=event.agent_token_usage;
    if(event.master_icon)state.session.master_icon=event.master_icon;
    const current=event.agents.find(agent=>agent.id===state.agentId);
    if(current && state.agentDetail)Object.assign(state.agentDetail,{name:current.name,icon:current.icon,status:current.status,error:current.error,sleep:current.sleep});
    if(live){inspector();refreshAgentIdentities();if(isAllAgentsView())renderAllMessages();}return;
  }
  if(event.type==="context_compaction_session") {
    // An old running event is not a live maintenance job after a restart.
    // The opening snapshot is authoritative; never mutate its nested objects.
    if(live) {
      state.session.context_compactions=[...(state.session.context_compactions || []).filter(item=>item.agent_id!==event.operation.agent_id),event.operation];
      if(event.operation.status==="failed")toast(`后台压缩已放弃，Agent 继续使用原上下文：${event.operation.error}`);
      renderContextMaintenance();
    }
    return;
  }
  const source=event.type === "agent_event" ? event.agent_id : "master", observation=source === "master" ? event : event.event;
  if(source !== "master") {
    const buffer=state.agentBacklog.get(source)||new Map();buffer.set(observation.sequence,observation);
    while(buffer.size>800)buffer.delete(buffer.keys().next().value);state.agentBacklog.set(source,buffer);
    const descriptor=state.session.agents?.find(agent=>agent.id===source);applyLoopEvent(descriptor,observation);
    if(source===state.agentId)applyLoopEvent(state.agentDetail,observation);
  } else {
    applyMainEvent(event);
    const op=workspaceSync.operation;
    if(live && syncActive(op) && op.waitingSession===state.session.id) {
      if(event.type==="loop_started")finishWorkspaceSync(null,op);
      else if(event.type==="session_error" || event.type==="loop_stopped" && event.status==="failed")failWorkspaceSync(new Error(event.error || "运行环境启动失败"),op);
    }
  }
  const sample=AgentStatistics.modelRate(observation);
  if(sample){modelRates.set(`${sample.run_id}/${sample.step}`,{...sample,agent_id:source,agent_name:source === "master" ? "Master" : state.session.agents?.find(a=>a.id===source)?.name});if(live)renderSessionSpeed();}
  if(observation.type==="human_message")rememberHumanMessage(observation,source,live);
  if(isAllAgentsView() && live && (observation.type==="human_message" || observation.type==="user_input_queued" ||
      observation.type==="tool_end" && ["send_agent_message","broadcast_agent_message"].includes(observation.name)))scheduleAllMessages();
  if(source===state.agentId)renderAgentEvent(observation,live);
  else if(live)inspector();
}

function replayHistory(events, render) {
  const previous=state.replaying;state.replaying=true;
  try {for(const event of events)render(event,false);}
  finally {state.replaying=previous;if(!previous)renderConversationDetails();}
}

function renderUserInput(event){
  let row=state.userInputs.get(event.message_id);
  if(row?.node.dataset.status==="applied" && event.type==="user_input_queued")return;
  if(!row){
    const node=text("section","","user-followup conversation-message"),heading=text("div","","user-followup-heading conversation-heading");
    const status=text("small",""),stamp=text("time","","message-time"),kind=text("strong","","message-kind");
    const recipient=event.target || event.agent_id || state.agentId,recipientLabel=text("span","给 ","conversation-recipient");
    const recipientName=text("span",agentName(recipient));recipientName.dataset.agentName=recipient;
    recipientLabel.append(agentIconNode(recipient),recipientName);
    heading.append(text("span","你","conversation-avatar"),kind,recipientLabel,status,stamp);
    const body=text("div","","final-response");body.append(markdown(event.content));
    if(event.attachments?.length)body.append(imageGallery(event.attachments));
    node.dataset.messageId=event.message_id;node.tabIndex=-1;
    node.append(heading,body);row={node,status,stamp,kind};state.userInputs.set(event.message_id,row);
    appendConversationMessage(node,event);
    setMessageTime(stamp,event.timestamp);
  }
  const applied=event.type==="user_input_applied";
  if(applied && event.run_id && event.step) {
    if(!row.receipt) {
      const receipt=text("details","","user-input-receipt"),summary=document.createElement("summary");
      receipt.dataset.messageId=event.message_id;
      const preview=String(event.content || "图片消息").replace(/\s+/g," ");
      summary.append(text("strong","READ"),messageRoute("user",event.target || state.agentId),
        text("span",preview.length>180 ? preview.slice(0,180)+"…" : preview,"receipt-preview"));
      const body=text("div","","final-response");body.append(markdown(event.content));
      if(event.attachments?.length)body.append(text("small",`${event.attachments.length} 张用户图片已作为输入`,"receipt-attachments"));
      const original=text("button","↑ 查看发送时的消息","text-button receipt-original");original.type="button";
      original.addEventListener("click",()=>{row.node.scrollIntoView({block:"center"});row.node.focus({preventScroll:true});});
      body.append(original);receipt.append(summary,body);row.receipt=receipt;
    }
    placeInput(viewFor(event.run_id),event,row.receipt,[event.message_id]);
  }
  row.node.dataset.status=applied ? "applied" : "queued";
  row.kind.textContent="你";
  const paused=!acceptingAgentStates.has(agentStatus(state.agentId==="master" ? state.session : state.agentDetail));
  row.status.textContent=applied ? "已读" : paused ? "未读 · 待恢复" : "等待送达";
  row.status.title=applied ? `已由 Model call #${event.step} 读取` : paused ? "已保存为未读，恢复后的下一次 Model call 前读取" : "已保存到队列，将在下一次 Model call 前读取";
}

function renderSystemInput(event) {
  let row=state.systemInputs.get(event.message_id);
  if(row?.node.dataset.status==="applied" && event.type==="system_message_queued")return;
  if(!row) {
    const node=text("section","","system-message"),heading=text("div","","system-message-heading"),kind=text("strong","","message-kind"),status=text("small",""),stamp=text("time","","message-time");
    heading.append(kind,messageRoute("system",event.target || "master"),status,stamp);
    const body=text("div","","final-response");body.append(markdown(event.content));
    node.dataset.messageId=event.message_id;node.append(heading,body);
    if(event.configuration?.lifecycle)node.classList.add("lifecycle-message");
    row={node,kind,status,stamp};state.systemInputs.set(event.message_id,row);
  }
  const applied=event.type==="system_message_applied";
  if(applied && event.run_id && event.step)placeInput(viewFor(event.run_id),event,row.node,[event.message_id]);
  else if(!row.node.isConnected)pendingInputArea().append(row.node);
  row.node.dataset.status=applied ? "applied" : "queued";
  row.kind.textContent=applied ? "SYSTEM INPUT" : "SYSTEM QUEUED";
  row.status.textContent=applied ? `INPUT → Model call #${event.step}` : "已入队 · 下一次 Model call 前接收";
  setMessageTime(row.stamp,event.timestamp,"发送");refreshPendingInputs();
}

async function selectAgent(identifier,{messageNavigation}={}) {
  if(messageNavigation===undefined)state.messageNavigation=(state.messageNavigation || 0)+1;
  if(!state.session || identifier === state.agentId)return;
  if(identifier==="all")return selectAllAgents();
  currentDraft().text=$("prompt").value;
  const version=++state.agentOpening, sessionId=state.session.id;
  const snapshot=await api(agentAPI("",identifier));
  if(version!==state.agentOpening || state.session?.id!==sessionId)return;
  if(identifier === "master") {
    const extra=[...state.rootBacklog.values()].filter(event=>event.sequence>snapshot.sequence);
    install(snapshot);for(const event of extra)dispatch(event,false);scrollToEnd();return;
  }
  installChild(snapshot,identifier);
}

async function loadEarlierHistory() {
  if(isAllAgentsView())return loadEarlierAllMessages();
  const sessionId=state.session?.id,identifier=state.agentId,version=state.agentOpening;
  const history=identifier==="master" ? state.rootHistory : state.eventLog;
  const before=Math.min(...history.keys());if(!Number.isFinite(before))return false;
  const scroller=$("conversation-scroll"),top=scroller.scrollTop,height=scroller.scrollHeight;
  const result=await api(agentAPI(`/history?before=${before}`,identifier));
  if(state.session?.id!==sessionId || state.agentId!==identifier || state.agentOpening!==version)return false;
  const events=[...result.events,...history.values()].sort((a,b)=>a.sequence-b.sequence);
  if(identifier==="master")install({...state.session,model_rates:[...modelRates.values()],events,
    sequence:state.sequence,history_truncated:result.has_more},{tail:false});
  else installChild({...state.agentDetail,events,sequence:state.viewSequence,history_truncated:result.has_more},identifier,false);
  scroller.scrollTo({top:top+scroller.scrollHeight-height,behavior:"instant"});
  return result.events.length>0;
}

async function revealAgentMessage({sessionId,agentId,messageId,messageIds=[messageId]}) {
  const navigation=state.messageNavigation=(state.messageNavigation || 0)+1;
  if(state.session?.id!==sessionId && !await openSession({action:"browse",id:sessionId}))return;
  if(state.messageNavigation!==navigation)return;
  await selectAgent(agentId,{messageNavigation:navigation});
  const opening=state.agentOpening,pageOpening=state.opening;
  const current=()=>state.messageNavigation===navigation && state.agentOpening===opening && state.opening===pageOpening && state.session?.id===sessionId && state.agentId===agentId;
  const ids=new Set(messageIds);
  const find=()=>[...$("messages").querySelectorAll(".human-message,.peer-message")].find(node=>node.dataset.source===agentId &&
    [node.dataset.messageId,...(node.dataset.messageIds || "").split(",")].some(id=>ids.has(id)));
  let card=current() && find();
  while(current() && !card && $("messages").querySelector(".earlier-history")) {
    if(!await loadEarlierHistory())break;
    card=find();
  }
  if(!current())return;
  if(!card)throw new Error("未找到这条消息，存档可能已被删除。");
  for(const node of $("messages").querySelectorAll(".message-target"))node.classList.remove("message-target");
  card.classList.add("message-target","message-navigation-target");
  card.scrollIntoView({block:"center",behavior:"instant"});card.focus({preventScroll:true});
  readHumanMessages(agentId);inspector();
  setTimeout(()=>card.classList.remove("message-target"),2500);
}
function revealHumanMessage(destination) {return revealAgentMessage(destination);}

function installChild(snapshot, identifier, tail=true) {
  if(state.session)currentDraft().text=$("prompt").value;
  modelExcerptObserver.disconnect();state.agentId=identifier;state.agentDetail={...snapshot};state.viewSequence=0;
  state.userInputs.clear();state.systemInputs.clear();$("prompt").value=currentDraft().text;renderAttachments();loadComposerPreferences();
  state.views.clear();state.current=null;state.eventLog.clear();state.imageAssets.clear();
  $("messages").replaceChildren();$("welcome").hidden=true;
  const extra=[...(state.agentBacklog.get(identifier)?.values()||[])].filter(event=>event.sequence>snapshot.sequence);
  replayHistory([...snapshot.events,...extra],renderAgentEvent);
  for(const event of state.rootBacklog.values()) {
    if(event.type==="system_message_queued" && event.target===identifier)renderSystemInput(event);
  }
  Object.assign(state.agentDetail,{status:agentStatus(snapshot),sleep:snapshot.sleep,error:snapshot.error});
  if(snapshot.history_truncated) {
    const earlier=text("button","↑ 加载更早的记录","earlier-history");earlier.type="button";
    earlier.addEventListener("click",async()=>{
      earlier.disabled=true;
      try {
        await loadEarlierHistory();
      }catch(error){toast(error.message);earlier.disabled=false;}
    });
    $("messages").prepend(earlier);
  }
  readHumanMessages();inspector();if(tail)scrollToEnd();
}

$("continue-task").addEventListener("click",async()=>{
  if(!state.session || state.resuming)return;
  const session=state.session;state.resuming=true;controls();
  try {
    const result=await api(agentAPI("/continue","master"),{settings:state.pendingSettings.get(session.id) || {}});
    state.pendingSettings.delete(session.id);
    if(state.session===session){session.status=result.master_status;session.session_state=result.status;session.read_only=false;session.sleep=null;session.error=null;connectSessionEvents();scheduleNavigationRefresh();}
  }catch(error){toast(error.message);}finally{state.resuming=false;inspector();}
});

function renderAgentEvent(event, live = true) {
  const display = state.agentId === "master" ? state.session : state.agentDetail;
  if (event.sequence <= state.viewSequence) return;
  scheduleConversationDetails();
  state.viewSequence = event.sequence; state.eventLog.set(event.sequence, event);
  if(["tool_inventory","execution_updated"].includes(event.type)){applyLoopEvent(display,event);if(live)inspector();return;}
  if(event.type==="human_message"){rememberHumanMessage(event,event.source || state.agentId,live);return;}
  if(["user_input_queued","user_input_applied"].includes(event.type)){
    renderUserInput(event);if(live)inspector();return;
  }
  if(["system_message_queued","system_message_applied"].includes(event.type)){
    renderSystemInput(event);if(live)inspector();return;
  }
  applyLoopEvent(display,event);
  if(event.type==="session_updated") {
    Object.assign(state.session,{name:event.name,pinned:event.pinned});
    if(live)listSessions().catch(error=>toast(error.message));
  } else if(event.type==="model_changed") {
    state.session.model=event.model;
    if(live)listSessions().catch(error=>toast(error.message));
  } else if(event.type==="session_error") {
    for(const view of state.views.values())finishPendingModels(view,"error",event.timestamp);
    if(live && state.deleting!==state.session.id)toast(display.error || "Loop 启动失败");
  } else if (event.run_id) {
    const view = viewFor(event.run_id); state.current = view;
    if (Number.isInteger(event.images_drawn)) view.stats.drawn = event.images_drawn;
    if (Number.isInteger(event.images_viewed)) view.stats.viewed = event.images_viewed;
    if (Number.isInteger(event.steps)) view.stats.iterations = view.stats.models = event.steps;
    if (Number.isInteger(event.tools)) view.stats.tools = event.tools;
    switch (event.type) {
      case "context_compaction_start":
      case "context_compaction_transport":
      case "context_compaction_end":
      case "context_compaction_error":
        renderContextCompaction(view,event);break;
      case "agent_messages": renderAgentActivity(view,event);break;
      case "loop_start": view.started=event.timestamp*1000;setMessageTime(view.stamp,event.timestamp,"启动");break;
      case "model_start": {
        view.stats.iterations = event.step; view.stats.models++; view.phase = `Iteration ${event.step} · Model call #${event.step}`;
        const model = modelFor(view, event); model.context = event; renderModel(model); break;
      }
      case "model_transport": {
        const model = modelFor(view, event);
        model.transport = {...event};
        view.phase = `Model call #${event.step} · ${transportPhases[event.phase] || event.phase}`;
        if (event.phase === "error" || event.phase === "cancelled") {
          model.status = event.phase; model.elapsed = Math.max(0, ((event.timestamp * 1000 || Date.now()) - model.started) / 1000);
        } else {
          model.status = "running";model.elapsed = null;
        }
        renderModel(model); break;
      }
      case "model_end": {
        const model = modelFor(view, event); model.status = "done"; model.elapsed = event.elapsed ?? null; model.usage = event.usage || {};
        model.modelId ||= event.model || null;
        view.stats.iterations = Math.max(view.stats.iterations, event.step || 0);
        view.stats.models = Math.max(view.stats.models, view.models.size);
        model.reasoningContent = [event.reasoning_content,event.reasoning,event.thinking]
          .find(value=>typeof value==="string" && value.trim());
        const names = [...new Set((event.calls || []).map(call => call.function?.name).filter(Boolean))];
        model.outcome = event.tool_calls ? `${event.tool_calls} Tool call${event.tool_calls === 1 ? "" : "s"}${names.length ? ` · ${names.join(" / ")}` : ""}` : "Model output · 无工具调用";
        const hasText=typeof event.content==="string" && event.content.trim();
        const content=hasText ? event.content : event.step_note;
        if (typeof content === "string" && content.trim()) {
          for(const image of event.images || [])rememberImage(image);
          renderMessageBody(model.response,content,event.images);
          model.responseDetail.dataset.source=hasText ? "content" : event.step_note_source || "tool_arguments";
          model.responseDetail.title=hasText ? "模型正文" : event.step_note_source==="tool_message"
            ? "工具 message 参数中的模型正文（未另附本步说明）" : "模型通过工具参数返回的本步说明";
          model.responseDetail.hidden=false;
        }
        renderModel(model);
        const sample = AgentStatistics.modelRate({...event, model: event.model || model.modelId,
          agent_id:state.agentId,agent_name:state.agentId === "master" ? "Master" : state.agentDetail?.name});
        if (sample) modelRates.set(`${sample.run_id}/${sample.step}`, sample);
        if (live) renderSessionSpeed();
        break;
      }
      case "tool_start": {
        view.stats.tools++; view.phase = `Tool call #${event.number} · ${event.name}`;
        const detail = text("details", "", "tool-call"); detail.dataset.status = "running";
        const summary = document.createElement("summary"), status = text("span", "running", "tool-state");
        summary.append(text("span", event.name, "tool-name"), status);
        const args=toolArguments(event);
        if(["python","bash"].includes(event.name)) {
          const mode=event.execution_mode ?? args.mode ?? "sync";
          const badge=text("span",mode,"tool-mode");badge.dataset.mode=mode;
          badge.title=mode==="async" ? "异步提交：立即返回 execution.id" : "同步调用：等待本次执行结束";
          summary.insertBefore(badge,status);
        }
        const argumentsText=args.code ?? args.command ?? JSON.stringify(args,null,2);
        let communication=null;
        if(coordinationTools.has(event.name)) {
          const management=managementTools.has(event.name),observing=observationTools.has(event.name),listing=stateTools.has(event.name)||observing,sleeping=event.name==="sleep",human=event.name==="send_user_message";
          detail.classList.add("coordination-call",management ? "agent-management" : listing ? "agent-state-call" : sleeping ? "agent-sleep" : human ? "agent-human-call" : "agent-message-call");detail.dataset.action=event.name;
          communication=text("span",messageTools.has(event.name) ? args.message || "" : coordinationBrief(event.name,args),"coordination-brief");
          if(messageTools.has(event.name)) {
            const broadcast=event.name==="broadcast_agent_message",target=event.name==="send_user_message" ? "user" : broadcast ? "broadcast" : args.agent_ids || args.agent_id;
            detail.dataset.source=state.agentId;detail.dataset.target=Array.isArray(target) ? target.join(",") : target;
            if(broadcast)detail.classList.add("agent-broadcast-call");
            summary.replaceChildren(text("span",broadcast ? "BROADCAST" : human ? "TO YOU" : "SEND","coordination-category"),messageRoute(state.agentId,target),communication,status);
          }
          else summary.replaceChildren(text("span",management ? "AGENT CONTROL" : observing ? "GLOBAL MESSAGES" : listing ? "AGENTS" : event.origin==="system" ? "SYSTEM SLEEP" : "SLEEP","coordination-category"),text("span",sleeping ? agentName(state.agentId) : event.name,"tool-name"),communication,status);
          if(event.name==="listen_global_messages")status.textContent="listening";
          if(sleeping){status.textContent="sleeping";detail.dataset.origin=event.origin || "model";}
          summary.title=coordinationBrief(event.name,args);
        }
        const source = text("code", event.source_preview ?? argumentsText), pre = text("pre", "", "tool-code");
        const language = event.source_language || (event.name === "python" ? "python" : event.name === "bash" ? "bash" : "json");
        AgentCode.highlight(source, language);
        let metadata=null,messageBody=null;
        pre.append(source);detail.append(summary);
        if(messageTools.has(event.name)) {
          if(event.name==="send_user_message") {
            messageBody=text("div","","rich-message-body final-response");
            renderMessageBody(messageBody,args.message || "");detail.append(messageBody);
          }
          metadata=text("details","","message-tool-info");metadata.append(text("summary","参数与投递详情"),pre);detail.append(metadata);
        } else if(event.name==="view_image") {
          metadata=text("details","","image-tool-info");metadata.append(text("summary","路径与图像信息"),pre);detail.append(metadata);
        } else detail.append(pre);
        view.steps.append(detail);
        if (event.source_truncated) {
          const note = text("button", `代码预览 · ${countText(event.source_length)} chars · 展开加载完整源码`, "text-button tool-source-note");
          note.type = "button";detail.append(note);
          const sourceBase = agentAPI("/tool-source");
          let loading = false, loaded = false;
          const loadSource = async () => {
            if (loading || loaded) return;
            loading = true;note.disabled = true;note.textContent = "正在加载完整源码…";
            try {
              const query = new URLSearchParams({run_id: event.run_id, call_id: event.call_id, sequence: event.sequence});
              const full = await api(`${sourceBase}?${query}`);
              source.textContent = full.source;
              AgentCode.highlight(source, full.language);
              if(messageTools.has(event.name)) {
                const tool=view.tools.get(event.call_id);
                tool.args=JSON.parse(full.source);
                tool.communication.textContent=tool.args.message || "";
                const target=event.name==="send_user_message" ? "user" : event.name==="broadcast_agent_message" ? "broadcast" : tool.args.agent_ids || tool.args.agent_id;
                detail.querySelector(".message-route").replaceWith(messageRoute(detail.dataset.source,target));
                detail.dataset.target=Array.isArray(target) ? target.join(",") : target;
                if(messageBody)renderMessageBody(messageBody,tool.args.message,tool.images,detail.dataset.source);
                if(tool.peerCard)renderMessageBody(tool.peerCard.body,tool.args.message,tool.images,detail.dataset.source);
              }
              loaded = true;note.hidden = true;
            } catch (error) {note.textContent = `完整源码加载失败 · 点击重试：${error.message}`;}
            finally {loading = false;note.disabled = false;}
          };
          detail.addEventListener("toggle", () => {if (detail.open) loadSource();});
          note.addEventListener("click", loadSource);
        }
        view.tools.set(event.call_id, {node: detail, status, elapsed: null,communication,args,metadata,messageBody,name:event.name,started:event.timestamp});
        markToolNode(view.tools.get(event.call_id),{node_id:args.node_id,node_name:state.session.nodes?.find(n=>n.id===args.node_id)?.name});
        if(event.name==="transfer_files"){
          const nodes=[...new Set([args.source_node || "local",args.destination_node || "local"].filter(id=>id!=="local"))];
          if(nodes.length)markToolNode(view.tools.get(event.call_id),{node_id:nodes.join(" → "),node_name:nodes.map(id=>state.session.nodes?.find(node=>node.id===id)?.name || id).join(" → ")});
        }
        if(event.name==="sleep")view.phase="休眠中 · 新消息可唤醒";
        break;
      }
      case "tool_node": {markToolNode(view.tools.get(event.call_id),event);break;}
      case "tool_end": {
        const tool = view.tools.get(event.call_id), result = event.result;
        if (Number.isInteger(event.number)) view.stats.tools = Math.max(view.stats.tools, event.number);
        if (result.image) rememberImage(result.image);
        if (event.name === "view_image" && result.image?.sha256 && !result.error) {
          view.viewedImages.add(result.image.sha256);
          if (!Number.isInteger(event.images_viewed)) view.stats.viewed = view.viewedImages.size;
        }
        if (tool) {
          markToolNode(tool,result);
          tool.elapsed = event.elapsed;
          tool.node.dataset.status = result.error || (result.exit_code ?? 0) !== 0 || (result.timed_out && !tool.communication) ? "error" : "done";
          tool.status.textContent = `${result.timed_out && tool.communication ? "timeout" : tool.node.dataset.status} · ${seconds(event.elapsed)}`;
          if(tool.communication) {
            tool.communication.textContent=messageTools.has(event.name) ? tool.args.message || "" : coordinationBrief(event.name,tool.args,result);
            tool.node.querySelector("summary").title=coordinationBrief(event.name,tool.args,result);
            if(["send_agent_message","send_user_message"].includes(event.name) && !result.error)tool.status.textContent=`已入队 · ${seconds(event.elapsed)}`;
            if(["send_agent_message","broadcast_agent_message"].includes(event.name) && result.recipients && !result.error)tool.status.textContent=`${result.recipients?.length || 0} recipients · ${seconds(event.elapsed)}`;
            if(event.name==="sleep" && !result.error) {
              tool.status.textContent=`${result.reason==="message" ? "wake" : result.reason || "done"} · ${seconds(event.elapsed)}`;
              view.phase=result.reason==="message" ? "新消息唤醒 · 准备读取输入" : "休眠结束 · Loop 继续";
            }
          }
          if(event.name==="view_image" && result.image?.sha256 && !result.error) {
            const preview=imageGallery([result.image]);preview.classList.add("tool-image-preview");
            preview.append(text("span",result.image.path || tool.args.path,"tool-image-caption"));
            tool.node.insertBefore(preview,tool.node.children[1]);
            tool.node.open=true;
          }
          const outputTarget=tool.metadata || tool.node;
          if(tool.messageBody) {
            tool.images=result.images;
            renderMessageBody(tool.messageBody,tool.args.message || "",tool.images,tool.node.dataset.source);
          }
          if(tool.metadata && result.error)tool.metadata.open=true;
          if(["send_agent_message","broadcast_agent_message"].includes(event.name) && result.recipients && !result.error) {
            if(result.recipients.length)tool.peerCard=renderPeerMessage(view,{key:`out:${event.call_id}`,source:state.agentId,
              messageIds:[...result.recipients,...(result.cc || [])].map(recipient=>recipient.message_id),
              cc:(result.cc || []).map(recipient=>recipient.agent_id),
              targets:result.recipients.map(recipient=>recipient.agent_id),content:tool.args.message || "",images:result.images,
              timestamp:event.timestamp,broadcast:event.name==="broadcast_agent_message"});
            const recipients=text("div","","broadcast-recipients");recipients.append(text("div",`实际接收方 · ${result.recipients?.length || 0}`,"broadcast-recipient-label"));
            for(const recipient of result.recipients || []) {
              const row=text("div","","broadcast-recipient");row.dataset.agentId=recipient.agent_id;
              row.append(messageRoute(state.agentId,recipient.agent_id),text("small",recipient.message_id,"broadcast-message-id"));recipients.append(row);
            }
            for(const copy of result.cc || [])recipients.append(text('small',`CC · ${agentName(copy.agent_id)} · ${copy.message_id}`,'message-receipt'));
            outputTarget.append(recipients);break;
          }
          if(event.name==="send_user_message" && !result.error) {
            outputTarget.append(text("small","消息已送达人类收件区。","message-receipt"));break;
          }
          if(event.name==="list_agents" && !result.error) {
            const list=text("div","","agent-status-table");
            for(const agent of result.agents || []) {
              const row=text("button","","agent-status-row");row.type="button";
              row.append(text("strong",agent.name || agent.id),text("span",agent.status),AgentStatistics.identifyModel(text("small",agent.model || "","model-inline"),agent.model));
              row.addEventListener("click",()=>selectAgent(agent.id).catch(error=>toast(error.message)));list.append(row);
            }
            outputTarget.append(list);break;
          }
          const output = [result.stdout, result.stderr, result.error].filter(Boolean).join("\n") || JSON.stringify(result, null, 2);
          if (/(^|\n)\s*#{1,6}\s|\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)/.test(output)) {
            const formatted = text("div", "", "tool-formatted final-response"); formatted.append(markdown(output));
            const raw = document.createElement("details"); raw.className = "tool-raw";
            raw.append(text("summary", "原始输出"), text("pre", output, "tool-output"));
            outputTarget.append(formatted, raw);
          } else outputTarget.append(text("pre", output, "tool-output"));
        }
        break;
      }
      case "loop_error": {
        finishPendingModels(view,event.reason==="cancelled" ? "cancelled" : "error",event.timestamp);
        const error=text("div",event.error || "Agent 已停止；已提交的历史保留。","loop-error");
        view.steps.append(error);view.status.textContent=event.error || event.reason==="error" ? "failed" : "stopped";break;
      }
      case "loop_end":
      case "loop_stopped":
        finishPendingModels(view,event.error ? "error" : "cancelled",event.timestamp);
        for(const tool of view.tools.values())if(tool.node.dataset.status==="running") {
          tool.node.dataset.status="stopped";tool.status.textContent="stopped";
          tool.elapsed=Math.max(0,event.timestamp-(tool.started || event.timestamp));
        }
        view.status.textContent=event.error || event.reason==="error" ? "failed" : "stopped";
        break;

    }
  }
  if(live)inspector();
  if(live && state.agentId==="master" && ["tool_end","loop_end","loop_error"].includes(event.type))scheduleNavigationRefresh();
}

function install(snapshot, {tail = true, agentId = "master"} = {}) {
  const replayStarted=performance.now();
  if(state.session)currentDraft().text=$("prompt").value;
  state.agentId = "master";state.agentDetail = null;state.viewSequence = 0;
  state.userInputs.clear();state.systemInputs.clear();
  modelExcerptObserver.disconnect();
  if(state.session?.id!==snapshot.id){state.rootBacklog.clear();state.agentBacklog.clear();state.humanMessages.clear();}
  state.rootHistory.clear();state.session = {...snapshot}; state.sequence = 0; state.views.clear(); state.current = null; state.eventLog.clear(); state.imageAssets.clear();
  modelRates.clear();
  for (const sample of snapshot.model_rates || []) modelRates.set(`${sample.run_id}/${sample.step}`, sample);
  $("prompt").value=currentDraft().text;renderAttachments();loadComposerPreferences();
  $("messages").replaceChildren(); $("welcome").hidden = false;
  $("session-title").textContent = snapshot.name; $("session-title").title = snapshot.directory;
  replayHistory(snapshot.events || [],dispatch);
  state.session.status=agentStatus(snapshot);state.session.sleep=snapshot.sleep;state.sequence=snapshot.sequence;
  state.session.session_state=snapshot.status;delete state.session.master_status;
  Object.assign(state.session,{error:snapshot.error,name:snapshot.name,pinned:snapshot.pinned,context:snapshot.context,
    execution_mode:snapshot.execution_mode,model:snapshot.model,active_model:snapshot.active_model,master_icon:snapshot.master_icon,agents:snapshot.agents,team:snapshot.team});
  if(snapshot.read_only)overlaySessionSettings(state.session,state.pendingSettings.get(snapshot.id) || {});
  if (snapshot.history_truncated) {
    const earlier = text("button", "↑ 加载更早的记录", "earlier-history"); earlier.type = "button";
    earlier.addEventListener("click", async () => {
      earlier.disabled = true;
      try {
        await loadEarlierHistory();
      } catch (error) { toast(error.message); earlier.disabled = false; }
    });
    $("messages").prepend(earlier);
  }
  readHumanMessages();inspector();refreshAgentIdentities(true);
  renderSessionSpeed();
  if (tail) scrollToEnd();
  state.historyTiming={events:snapshot.events?.length || 0,elapsed_seconds:(performance.now()-replayStarted)/1000};
  if (agentId !== "master") selectAgent(agentId).catch(error => toast(error.message));
}

function sessionRow(session) {
  const row = text("div", "", "session-row"); row.dataset.id=session.id;
  AgentStatistics.identifyModel(row, session.model);
  row.dataset.status=session.storage_missing ? "missing" : agentStatus(session);
  const item = text("button", "", "session-item" + (session.id === state.session?.id ? " selected" : ""));
  if(session.id===state.session?.id)item.setAttribute("aria-current","page");
  AgentStatistics.identifyModel(item, session.model);
  item.type="button";item.title=`${session.name}\n${session.directory}${session.model ? `\n模型：${session.model}` : ""}`;
  item.disabled=!!session.busy;
  if(session.busy)item.title+='\n其他 Host正在独占运行；Stop 后才可打开';
  const completed = unreadCount(session.id)>0;
  row.classList.toggle("has-unread", completed);
  item.append(AgentStatistics.identifyModel(text("span",session.pinned ? "⌃" : "▤","session-glyph"), session.model),text("strong",session.name),text("small",session.storage_missing ? "存档缺失" : completed ? `${unreadCount(session.id)} 新消息` : sessionStatusLabel(session), "session-status"));
  item.addEventListener("click",()=>openSession({action:"browse",id:session.id}));
  row.append(item);
  {
    const actions=text("div","","session-actions");
    const pin=text("button",session.pinned ? "★" : "☆","session-pin"),more=text("button","⋯","session-more");
    pin.type=more.type="button";pin.title=session.pinned ? "取消置顶" : "置顶 Session";pin.setAttribute("aria-label",pin.title);pin.setAttribute("aria-pressed",String(!!session.pinned));
    pin.disabled=more.disabled=session.status==='stop' || !!session.busy;
    more.title="Session 操作";more.setAttribute("aria-label",`${session.name} 的更多操作`);more.setAttribute("aria-haspopup","menu");
    pin.addEventListener("click",async()=>{pin.disabled=true;try{await updateSession(session,{pinned:!session.pinned});}catch(error){toast(error.message);pin.disabled=false;}});
    more.addEventListener("click",()=>showSessionMenu(session,more));actions.append(pin,more);row.append(actions);
  }
  return row;
}
async function updateSession(session,fields) {
  const info=await api(`/api/sessions/${encodeURIComponent(session.id)}/metadata`,fields);
  if(state.session?.id===info.id){Object.assign(state.session,{name:info.name,pinned:info.pinned});inspector();}
  await listSessions();return info;
}
function renameSession(session) {
  state.renaming=session;$("rename-name").value=session.name;$("rename-error").textContent="";
  $("rename-dialog").showModal();$("rename-name").focus();$("rename-name").select();
}
function showSessionMenu(session,button) {
  state.menuSession=session;
  const menu=$("session-menu"),bounds=button.getBoundingClientRect();
  menu.style.left=`${Math.max(8,Math.min(innerWidth-160,bounds.right-140))}px`;
  menu.style.top=`${Math.max(8,Math.min(innerHeight-100,bounds.bottom+4))}px`;
  menu.showPopover();
}
const restartServerMessage="服务器尚未加载 Session 删除接口。请在运行 recoding-web.py 的服务器上重启 Web 服务，再刷新页面；仅刷新浏览器无效。";
function serverWarning(message) {
  let banner=$("server-warning");
  if(!banner){banner=text("div","","server-warning");banner.id="server-warning";banner.setAttribute("role","status");document.querySelector(".topbar").after(banner);}
  banner.textContent=message;banner.hidden=!message;
}
async function deleteOnServer(session) {
  const config=await api("/api/config");
  if(!config.capabilities?.session_delete)throw new Error(restartServerMessage);
  if(state.config.instance_id&&config.instance_id!==state.config.instance_id)throw new Error("后端服务已重启或连接到了另一台服务器，请刷新页面后再删除。");
  try {
    const result=await api(`/api/sessions/${encodeURIComponent(session.id)}/delete`,{});
    if(result.deleted!==session.id)throw new Error("服务器未确认删除该 Session，请刷新列表核对。");
    return result;
  } catch(error) {
    if(error.status===404&&error.code!=="session_not_found")throw new Error(restartServerMessage);
    throw error;
  }
}
$("menu-rename-session").addEventListener("click",()=>{$("session-menu").hidePopover();renameSession(state.menuSession);});
$("menu-delete-session").addEventListener("click",()=>{
  $("session-menu").hidePopover();state.pendingDelete=state.menuSession;
  $("delete-name").textContent=state.pendingDelete.name;$("delete-path").textContent=state.pendingDelete.session_directory;
  $("delete-error").textContent="";$("delete-dialog").showModal();$("cancel-delete").focus();
});
$("cancel-delete").addEventListener("click",()=>$("delete-dialog").close());
$("delete-form").addEventListener("submit",async event=>{
  event.preventDefault();const session=state.pendingDelete;$("confirm-delete").disabled=true;state.deleting=session.id;
  try{
    await deleteOnServer(session);
    for(const [key,draft] of state.drafts)if(key.startsWith(`${session.id}/`)){
      for(const item of draft.images || [])URL.revokeObjectURL(item.preview);state.drafts.delete(key);
    }
    if(state.session?.id===session.id){
      ++state.opening;state.source?.close();state.source=null;state.session=null;state.current=null;modelExcerptObserver.disconnect();state.views.clear();state.eventLog.clear();state.imageAssets.clear();state.switching=false;
      $("prompt").value="";renderAttachments();$("messages").replaceChildren();$("welcome").hidden=false;
      localStorage.removeItem("recoding-session");inspector();
    }
    $("delete-dialog").close();await listSessions();toast("Session 已删除，workspace 文件保留。");
  }catch(error){$("delete-error").textContent=error.message;}
  finally{state.deleting=null;$("confirm-delete").disabled=false;}
});
$("rename-session").addEventListener("click",()=>renameSession(state.session));
$("dismiss-rename").addEventListener("click",()=>$("rename-dialog").close());
$("rename-form").addEventListener("submit",async event=>{
  event.preventDefault();$("rename-submit").disabled=true;
  try{await updateSession(state.renaming,{name:$("rename-name").value.trim()});$("rename-dialog").close();}
  catch(error){$("rename-error").textContent=error.message;}
  finally{$("rename-submit").disabled=false;}
});

function scheduleNavigationRefresh() {
  clearTimeout(navigationTimer);
  navigationTimer = setTimeout(() => {if(state.config && !state.serverChanged && !document.hidden)listSessions().catch(() => {});}, 200);
}
const syncActive = op => op && workspaceSync.operation===op && op.status==="running";
function beginWorkspaceSync(kind="scan", path) {
  // A background catalog refresh must not replace foreground Resume progress.
  if(kind==="scan" && (syncActive(workspaceSync.operation) ||
      (workspaceSync.operation?.kind!=="scan" && !$("workspace-sync").hidden && $("workspace-sync").dataset.state==="done")))return null;
  clearTimeout(workspaceSync.showTimer);clearTimeout(workspaceSync.hideTimer);
  const op={kind,started:Date.now(),performanceStarted:performance.now(),status:"running",phases:[],requests:[],path:path || state.config?.session_root || "正在连接服务器…"};
  workspaceSync.operation=op;
  $("workspace-sync-timing").open=false;
  const show=()=>{if(syncActive(op)){$("workspace-sync").hidden=false;renderWorkspaceSync(op);}};
  if(kind!=="scan" || !workspaceSync.loaded || $("workspace-sync").dataset.state==="error")show();
  else workspaceSync.showTimer=setTimeout(show,600);
  return op;
}
function syncPhase(op, label, detail="") {
  if(!syncActive(op))return;
  if(op.phase){op.phase.elapsed_seconds=(Date.now()-op.phase.started)/1000;op.phases.push(op.phase);}
  op.phase=label ? {label,detail,started:Date.now()} : null;
  renderWorkspaceSync(op);
}
function syncSteps(op) {
  return [...op.phases,...op.requests.flatMap(request=>request.data?.steps || []),
    ...op.requests.flatMap(request=>request.data?.runtime?.steps || [])];
}
function renderWorkspaceSync(op) {
  if(workspaceSync.operation!==op)return;
  const banner=$("workspace-sync");banner.dataset.state=op.status==="running" ? "syncing" : op.status;
  const compact=op.kind==="open" || Boolean(op.waitingSession) || Boolean(op.retry && op.kind==="startup");
  banner.dataset.mode=compact ? "session" : "scan";
  $("workspace-sync-label").textContent=op.label || (op.kind==="open" ? "正在恢复 Session…" : "正在同步当前工作目录…");
  $("workspace-sync-path").textContent=op.path;$("workspace-sync-path").title=op.path;
  $("workspace-sync-path").hidden=compact;
  $("workspace-sync-detail").hidden=op.status!=="error";$("retry-workspace-sync").hidden=op.status!=="error";
  const request=op.requests.at(-1), data=request?.data;
  let step=op.phase, age=step ? (Date.now()-step.started)/1000 : 0;
  if(!step && op.status==="running") {
    const progress=request?.receiving ? null : request?.pending ? data : data?.runtime;
    step=progress?.steps?.at(-1);
    age=step ? step.elapsed_seconds+(step.status==="running" ? (Date.now()-request.received)/1000 : 0) : (Date.now()-(request?.started || op.started))/1000;
    if(!step)step={label:request?.receiving ? "接收响应并解码" : op.waitingSession ? "等待 Agent worker 就绪" : "等待请求送达服务器",detail:request?.pollError || ""};
  }
  $("workspace-sync-stage").textContent=op.status==="running" ? `${step?.label || "准备中"} · ${seconds(age)}` : "";
  $("workspace-sync-stage").title=step?.detail || "";
  const steps=syncSteps(op).filter(step=>step.status!=="running");
  $("workspace-sync-timing").hidden=!steps.length;
  const signature=JSON.stringify(steps);
  if(signature!==op.stepsSignature){
    $("workspace-sync-steps").replaceChildren(...steps.map(step=>{const item=text("span",`${step.label} ${seconds(step.elapsed_seconds)}`);item.title=step.detail || step.label;return item;}));
    op.stepsSignature=signature;
  }
  $("workspace-sync-elapsed").textContent=`${(((op.ended || Date.now())-op.started)/1000).toFixed(1)}s`;
  banner.title=steps.map(step=>`${step.label} ${seconds(step.elapsed_seconds)}`).join(" · ");
}
async function pollSyncProgress(op, request) {
  clearTimeout(request.timer);
  if(document.hidden || state.serverChanged || request.polling)return;
  request.polling=true;
  // This endpoint uses its own tiny lock; scans/Resume cannot block it.
  try {
    const response=await fetch(`/api/progress?id=${encodeURIComponent(request.id)}`,{headers:{"X-recoding-Page":state.pageId,"X-recoding-Document":state.documentId},signal:AbortSignal.timeout(3000)});
    if(serviceInstanceChanged(response.headers.get("X-Agent-Instance"))){response.body?.cancel().catch(()=>{});return;}
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    const result=await response.json();
    if(result.operation){request.data=result.operation;request.received=Date.now();}
    request.pollError="";
  } catch {request.pollError="进度连接暂不可用，仍在等待原请求";}
  finally {request.polling=false;}
  if(workspaceSync.operation!==op)return;
  renderWorkspaceSync(op);
  if(op.status!=="running")return;
  if(request.pending || request.data?.status==="running" || (request===op.requests.at(-1) && op.waitingSession))
    request.timer=setTimeout(()=>pollSyncProgress(op,request),400);
}
async function syncRequest(op, path, body) {
  if(!syncActive(op))return api(path,body);
  syncPhase(op,null);
  const request={id:crypto.randomUUID(),started:Date.now(),pending:true};op.requests.push(request);
  pollSyncProgress(op,request);
  try {
    return await api(path,body,{operation:request.id,onHeaders:response=>{
      if(!syncActive(op))return;
      request.receiving=true;
      const bytes=Number(response.headers.get("Content-Length"));
      syncPhase(op,"接收响应并解码",bytes ? `${(bytes/1024).toFixed(0)} KiB HTTP 响应` : "");
    }});
  } finally {
    request.pending=false;request.receiving=false;
    if(syncActive(op))syncPhase(op,null);
  }
}
function finishWorkspaceSync(sessions, op=workspaceSync.operation) {
  if(!syncActive(op))return;
  clearTimeout(workspaceSync.showTimer);op.ended=Date.now();op.status="done";
  op.label=op.kind==="open" || op.waitingSession ? "Session 已就绪" : `当前工作目录已同步 · ${sessions?.length ?? 0} Sessions`;
  renderWorkspaceSync(op);
  recordBrowserTiming(op);
  $("session-count").title=$("workspace-sync").title;
  if(!$("workspace-sync").hidden)workspaceSync.hideTimer=setTimeout(()=>{if(workspaceSync.operation===op)$("workspace-sync").hidden=true;},1800);
}
function failWorkspaceSync(error, op=workspaceSync.operation) {
  if(!syncActive(op))return;
  clearTimeout(workspaceSync.showTimer);clearTimeout(workspaceSync.hideTimer);
  op.ended=Date.now();op.status="error";op.label=op.kind==="open" || op.waitingSession || op.kind==="startup" && op.retry ? "Session 恢复失败" : "当前工作目录同步失败";
  $("workspace-sync").hidden=false;$("workspace-sync-detail").textContent=error.message;
  renderWorkspaceSync(op);
  recordBrowserTiming(op);
}
function recordBrowserTiming(op){
  if(state.serverChanged || !state.config?.csrf || op.timingReported)return;
  if(op.kind==="scan" && op.status!=="error" && op.ended-op.started<500)return;
  op.timingReported=true;
  // Only numeric timings and same-origin URL paths leave the browser. Queries,
  // prompts, drafts, images and Session names are never included.
  const entries=[...(op.kind==="startup" ? performance.getEntriesByType("navigation") : []),
    ...performance.getEntriesByType("resource").filter(entry=>entry.startTime>=op.performanceStarted)];
  const samples=entries.filter(entry=>{
    const url=new URL(entry.name,location.href);
    return url.origin===location.origin && !["/api/diagnostics","/api/progress"].includes(url.pathname);
  }).slice(-40).map(entry=>({path:new URL(entry.name,location.href).pathname,start_ms:entry.startTime,
    duration_ms:entry.duration,queue_connect_ms:Math.max(0,entry.requestStart-entry.fetchStart),
    dns_ms:Math.max(0,entry.domainLookupEnd-entry.domainLookupStart),connect_ms:Math.max(0,entry.connectEnd-entry.connectStart),
    ttfb_ms:Math.max(0,entry.responseStart-entry.requestStart),transfer_ms:Math.max(0,entry.responseEnd-entry.responseStart),
    server_ms:entry.serverTiming?.find(metric=>metric.name==="app")?.duration}));
  fetch("/api/diagnostics",{method:"POST",keepalive:true,
    headers:{"Content-Type":"application/json","X-Agent-Token":state.config.csrf,"X-recoding-Page":state.pageId,"X-recoding-Document":state.documentId},
    body:JSON.stringify({kind:op.kind,page:state.pageId,elapsed_ms:op.ended-op.started,
      operations:op.requests.map(request=>request.id),samples})}).catch(()=>{});
}
$("retry-workspace-sync").addEventListener("click",()=>{
  const retry=workspaceSync.operation?.retry;
  if(retry)retry();else if(state.config)listSessions({operation:beginWorkspaceSync("scan")}).catch(()=>{});else start();
});
function listSessions({operation,keepOpen=false}={}) {
  if(state.changingRoot)return Promise.resolve([]);
  if (!listingRequest) {
    const op=operation || (!workspaceSync.loaded ? beginWorkspaceSync() : null);
    $("session-list").setAttribute("aria-busy","true");
    listingRequest=renderSessionList(op).then(sessions=>{
      workspaceSync.loaded=true;
      if(op && !keepOpen)finishWorkspaceSync(sessions,op);
      return sessions;
    }).catch(error=>{if(op)failWorkspaceSync(error,op);throw error;}).finally(()=>{
      $("session-list").setAttribute("aria-busy","false");listingRequest=null;
    });
  }
  return listingRequest;
}
async function renderSessionList(op) {
  const expanded=[...expandedDirectories].filter(([,open])=>open).map(([directory])=>directory);
  const started=performance.now(),result=await syncRequest(op,`/api/sessions?expanded=${encodeURIComponent(JSON.stringify(expanded))}`),received=performance.now();
  if(op)syncPhase(op,"渲染 Session 列表");
  if(result.root){state.config.session_root=result.root;state.config.root_generation=result.root_generation;$("root-path").textContent=result.root;$("root-path").title=result.root;}
  state.navigationTree=result.directory_tree || [];
  const sessions=renderSessionNavigation(result.sessions);
  if(op)syncPhase(op,null);
  workspaceSync.metrics={...result.sync,request_seconds:(received-started)/1000,render_seconds:(performance.now()-received)/1000};
  return sessions;
}

$("discover-sessions").addEventListener("click",async()=>{
  if(!state.config || state.changingRoot || state.serverChanged || state.discoveringSessions)return;
  const generation=state.config.root_generation,button=$("discover-sessions");
  state.discoveringSessions=true;button.textContent="发现中…";button.setAttribute("aria-busy","true");controls();
  try {
    // Wait for an older poll, then request a fresh catalog for this click.
    if(listingRequest)await listingRequest;
    if(generation!==state.config.root_generation || state.changingRoot)return;
    const sessions=await listSessions();
    if(generation!==state.config.root_generation || state.changingRoot)return;
    directorySessionContents.clear();expandedSessionGroups.clear();expandedDirectories.set(state.config.session_root,true);
    renderSessionNavigation(sessions);
  }catch(error){toast(error.message);}
  finally{state.discoveringSessions=false;button.textContent="发现全部";button.removeAttribute("aria-busy");controls();}
});

async function changeWorkspace(directory){
  if(state.changingRoot)return;
  if(state.session)currentDraft().text=$("prompt").value;
  state.changingRoot=true;state.switching=true;++state.opening;
  state.source?.close();state.source=null;controls();
  const pending=listingRequest;if(pending)await pending.catch(()=>{});
  const op=beginWorkspaceSync("workspace",directory);op.label="正在打开工作目录…";
  try{
    const result=await syncRequest(op,"/api/workspace",{directory});
    state.config.session_root=result.root;state.config.root_generation=result.root_generation;
    state.session=null;state.agentId="master";state.agentDetail=null;state.sequence=state.viewSequence=0;
    modelExcerptObserver.disconnect();state.views.clear();state.userInputs.clear();state.systemInputs.clear();state.eventLog.clear();state.imageAssets.clear();
    state.rootBacklog.clear();state.agentBacklog.clear();state.current=null;state.navigationSignature=null;expandedDirectories.clear();directorySessionContents.clear();expandedSessionGroups.clear();localSessionContents.clear();nonSessionFolders.clear();
    $("prompt").value="";renderAttachments();$("messages").replaceChildren();$("welcome").hidden=false;
    $("root-path").textContent=result.root;$("root-path").title=result.root;
    localStorage.removeItem("recoding-session");
    sessionStorage.removeItem('recoding-session');
    state.navigationTree=result.directory_tree || [];
    renderSessionNavigation(result.sessions);workspaceSync.loaded=true;inspector();finishWorkspaceSync(result.sessions,op);
  }catch(error){failWorkspaceSync(error,op);toast(error.message);connectSessionEvents();}
  finally{state.changingRoot=false;state.switching=false;controls();}
}
function renderSessionNavigation(sessions) {
  const current=sessions.find(item=>item.id===state.session?.id);
  if(current?.busy){
    currentDraft().text=$("prompt").value;revokeArchiveView();
    toast('此 Session 已由其他 Host独占，Stop 后可重新打开。');
  }else if(current?.status==='stop' && state.session && !state.session.read_only){
    state.source?.close();state.source=null;state.session.read_only=true;state.session.session_state='stop';state.session.status='stopped';inspector();
  }
  for (const session of sessions) paintMessageBadge(session);
  const ids = new Set(sessions.map(session => session.id));
  for(const id of unreadHumanMessages.keys())if(!ids.has(id))unreadHumanMessages.delete(id);
  $("session-count").textContent = sessions.length;
  const navigationRoot=state.config.session_root || "/",required=new Map(),localSessions=new Map();
  for(const session of sessions) {
    if(!localSessions.has(session.directory))localSessions.set(session.directory,[]);
    localSessions.get(session.directory).push(session);
    let directory=session.directory;
    while(directory===navigationRoot || directory.startsWith(navigationRoot==="/" ? "/" : navigationRoot+"/")) {
      if(!required.has(directory))required.set(directory,[]);
      required.get(directory).push(session.id);
      if(directory===navigationRoot)break;
      directory=directory.slice(0,directory.lastIndexOf("/")) || "/";
    }
  }
  // Discovery wins over an old collapsed default. An unchanged poll respects
  // a deliberate collapse; additions/moves reopen exactly the affected paths.
  const contents=new Map([...required].map(([path,ids])=>[path,ids.sort().join(",")]));
  for(const [path,ids] of contents)if(directorySessionContents.get(path)!==ids)expandedDirectories.set(path,true);
  for(const path of directorySessionContents.keys())if(!contents.has(path)&&path!==navigationRoot)expandedDirectories.set(path,false);
  directorySessionContents=contents;
  const localContents=new Map([...localSessions].map(([path,items])=>[path,items.map(item=>item.id).sort().join(",")]));
  for(const [path,ids] of localContents)if(localSessionContents.get(path)!==ids)expandedSessionGroups.set(path,true);
  for(const path of expandedSessionGroups.keys())if(!localContents.has(path))expandedSessionGroups.delete(path);
  localSessionContents=localContents;
  const signature = JSON.stringify({root:state.config.session_root,tree:state.navigationTree,expanded:[...expandedDirectories],sessionGroups:[...expandedSessionGroups],nonSessionFolders:[...nonSessionFolders],selected:state.session?.id,sessions:sessions.map(({id,name,directory,pinned,pinned_at,status,active_agents,created_at,storage_missing,model})=>({id,name,directory,pinned,pinned_at,status,active_agents,created_at,storage_missing,model}))});
  if (signature === state.navigationSignature && !state.revealDirectory) return sessions;
  state.navigationSignature = signature;
  const scrollTop = $("session-list").scrollTop;
  $("session-list").replaceChildren();
  const active=sessions.filter(sessionIsActive).sort((a,b)=>(b.created_at || "").localeCompare(a.created_at || "") || a.id.localeCompare(b.id));
  if(active.length){const section=text("section","","running-sessions");section.setAttribute("aria-label","运行中的 Sessions");section.append(text("div",`运行中 · ${active.length}`,"section-label"));for(const session of active)section.append(sessionRow(session));$("session-list").append(section);}
  const pinned=sessions.filter(session=>session.pinned&&!sessionIsActive(session)).sort((a,b)=>(b.pinned_at || "").localeCompare(a.pinned_at || ""));
  if(pinned.length){const section=text("section","","pinned-sessions");section.append(text("div","PINNED","section-label"));for(const session of pinned)section.append(sessionRow(session));$("session-list").append(section);}
  const tree=new Map((state.navigationTree || []).map(entry=>[entry.directory,entry]));
  const folders = [...new Set([navigationRoot,...sessions.map(session=>session.directory),...[...tree.values()].flatMap(entry=>[entry.directory,...entry.children])])]
    .filter(path=>path===navigationRoot || path.startsWith(navigationRoot==="/" ? "/" : navigationRoot+"/"));
  const parts=folders.map(path=>path.split("/").filter(Boolean)),shared=navigationRoot.split("/").filter(Boolean);
  const root={directory:"/"+shared.join("/"),children:new Map()};
  for(const path of parts){let node=root;for(let depth=shared.length;depth<path.length;depth++){const directory="/"+path.slice(0,depth+1).join("/");if(!node.children.has(directory))node.children.set(directory,{directory,children:new Map()});node=node.children.get(directory);}}
  const visibleDirectories = new Set();
  function branch(node, parentDirectory = root.directory) {
    const directory=node.directory;
    visibleDirectories.add(directory);
    const wrapper=text("section","","workspace-branch"),group=text("details","","workspace-group");group.dataset.directory=directory;
    wrapper.append(group);
    const reveal=state.revealDirectory;
    const revealHere = reveal===directory || reveal?.startsWith(directory==="/" ? "/" : directory+"/");
    const open = !!revealHere || (expandedDirectories.has(directory) ? expandedDirectories.get(directory) : node===root);
    expandedDirectories.set(directory, open); group.open = open;
    group.addEventListener("toggle",()=>{
      if (!group.isConnected || expandedDirectories.get(directory)===group.open) return;
      expandedDirectories.set(directory,group.open);
      if(group.open && !tree.has(directory))scheduleNavigationRefresh();
    });
    const label=directory===parentDirectory ? directory.split("/").filter(Boolean).pop() || "/" : directory.slice(parentDirectory==="/" ? 1 : parentDirectory.length+1);
    const heading = document.createElement("summary"), name = text("span", (directory===state.config.session_root ? "⌂ " : "▱ ")+label);
    name.title = directory;
    const add = text("button", "+", "workspace-add"); add.type = "button"; add.title = `在 ${directory} 新建 Session`;
    add.disabled = directory === "/";
    add.addEventListener("click", event=>{event.preventDefault();event.stopPropagation();newSession(directory);});
    heading.addEventListener("click",event=>{
      if(event.target.closest("button"))return;
      // Record the user's choice before a pending navigation response can
      // replace this details node and reveal the just-opened Session again.
      expandedDirectories.set(directory,!group.open);state.revealDirectory=null;
      if(!group.open && !tree.has(directory))scheduleNavigationRefresh();
    });
    heading.append(name, add); group.append(heading);
    const own=localSessions.get(directory) || [];
    if(own.length){
      // A workspace's Sessions are independent of its subdirectory branches.
      // PINNED is a shortcut, not a replacement for the Session's own location.
      const local=text("details","","directory-sessions");local.dataset.directory=directory;
      local.open=reveal===directory || expandedSessionGroups.get(directory)!==false;
      expandedSessionGroups.set(directory,local.open);
      const summary=text("summary",`Sessions · ${own.length}`);summary.title="显示或隐藏本目录的 Session，不影响子目录";
      summary.addEventListener("click",()=>{expandedSessionGroups.set(directory,!local.open);state.revealDirectory=null;});
      local.addEventListener("toggle",()=>{if(local.isConnected)expandedSessionGroups.set(directory,local.open);});
      local.append(summary);
      for(const session of own)local.append(sessionRow(session));
      group.append(local);
    }
    if(node===root && !sessions.length)group.append(text("div","尚未发现 Session · 点击 + 创建","session-empty"));
    if(open && !tree.has(directory))group.append(text("div","正在读取目录…","session-empty"));
    if(tree.get(directory)?.error)group.append(text("div","暂时无法读取此目录","session-empty"));
    if(node.children.size){
      const ordered=[...node.children.values()].sort((a,b)=>a.directory.localeCompare(b.directory));
      const ordinary=ordered.filter(child=>!directorySessionContents.has(child.directory));
      const showOrdinary=node===root || nonSessionFolders.has(directory);
      if(node!==root && ordinary.length){
        const toggle=text("button",`${showOrdinary ? "Hide" : "Display"} non-session folders · ${ordinary.length}`,"text-button non-session-toggle");
        toggle.type="button";toggle.setAttribute("aria-expanded",String(showOrdinary));toggle.dataset.directory=directory;
        toggle.addEventListener("click",()=>{
          if(nonSessionFolders.has(directory))nonSessionFolders.delete(directory);else nonSessionFolders.add(directory);
          renderSessionNavigation(sessions);
        });group.append(toggle);
      }
      const children=text("div","","directory-children");
      for(const child of ordered)if(showOrdinary || directorySessionContents.has(child.directory))children.append(branch(child,directory));
      group.append(children);
    }
    return wrapper;
  }
  $("session-list").append(branch(root));
  for (const directory of expandedDirectories.keys()) if (!visibleDirectories.has(directory)) expandedDirectories.delete(directory);
  $("session-list").scrollTop = scrollTop;
  state.revealDirectory = null;
  return sessions;
}

async function openSession(reference, {operation,reveal=true,startupSessions=null}={}) {
  if(state.session)currentDraft().text=$("prompt").value;
  const version = ++state.opening;
  state.source?.close();state.source=null;
  state.switching = true; controls();
  const op=operation || beginWorkspaceSync("open",reference.directory || reference.path || state.config.session_root);
  op.label=reference.action==="create" ? "正在创建 Session…" : "正在打开 Session 历史…";
  op.retry=()=>openSession(reference);renderWorkspaceSync(op);
  try {
    const requestStarted=performance.now();
    const snapshot = await syncRequest(op,"/api/sessions", reference);
    const received=performance.now();
    if (version !== state.opening) return;
    op.path=snapshot.directory;op.waitingSession=snapshot.id;
    syncPhase(op,"渲染 Session 历史",`${snapshot.events?.length || 0} 条事件`);
    // Let the browser paint this phase before synchronous DOM replay starts.
    await new Promise(resolve=>{setTimeout(resolve,80);requestAnimationFrame(()=>setTimeout(resolve,0));});
    if(version!==state.opening)return;
    state.switching = false;
    state.agentBacklog.clear();state.rootBacklog.clear();state.source?.close(); install(snapshot,{agentId:"all"});state.revealDirectory=reveal ? snapshot.directory : null;
    syncPhase(op,null);
    state.openTiming={request_seconds:(received-requestStarted)/1000,...state.historyTiming};
    readHumanMessages();
    localStorage.setItem("recoding-session", JSON.stringify({id:snapshot.id,path:snapshot.session_directory}));
    sessionStorage.setItem('recoding-session',JSON.stringify({id:snapshot.id,path:snapshot.session_directory}));
    document.body.classList.remove("sidebar-open"); syncPanelButtons();
    state.source=null;connectSessionEvents();
    if(snapshot.status==="failed" && reference.action!=="browse")failWorkspaceSync(new Error(snapshot.error || "Loop 启动失败；恢复后才能接收新消息"),op);
    else if(agentStatus(snapshot)!=="starting")finishWorkspaceSync(null,op);
    await listSessions();
    return true;
  } catch (error) {
    if(startupSessions && version===state.opening &&
        ["session_not_found","unsupported_session"].includes(error.code)) {
      // A browser bookmark is only a hint; the server catalog remains usable.
      localStorage.removeItem("recoding-session");sessionStorage.removeItem("recoding-session");
      delete op.retry;delete op.waitingSession;op.path=state.config.session_root;
      serverWarning("上次打开的 Session 已移动、删除或格式不兼容，请从当前目录列表重新选择。");
      finishWorkspaceSync(startupSessions,op);
    } else {
      failWorkspaceSync(error,op);
      if ($("session-dialog").open) $("dialog-error").textContent = error.message;
      else toast(error.message);
    }
    return false;
  }
  finally { if (version === state.opening) { state.switching = false; controls();connectSessionEvents(); } }
}

$("composer").addEventListener("submit", async event => {
  event.preventDefault();
  if ($("send").disabled || !state.session) return;
  const prompt = $("prompt").value;
  const submittedSession=state.session,submittedAgent=state.agentId,recipient=composerRecipient(),url=isAllAgentsView() ? agentAPI("/messages","master") : agentAPI("/input");
  const draft = currentDraft(), pictures = [...draft.images];
  draft.submitting=true;controls();
  try {
    await api(url,{prompt,attachments:pictures.map(item=>item.id),...(submittedAgent==="all" ? {recipient} : {})});
    draft.images=draft.images.filter(item=>!pictures.includes(item));for(const item of pictures)URL.revokeObjectURL(item.preview);
    if(draft.text===prompt)draft.text="";
    if (state.session?.id === submittedSession.id && state.agentId===submittedAgent && $("prompt").value === prompt) {
      $("prompt").value = "";
      composerLayout.resize();
    }
    if(state.session?.id===submittedSession.id && state.agentId===submittedAgent)renderAttachments();
    if(submittedAgent==="all" && state.session?.id===submittedSession.id && isAllAgentsView())
      syncAllMessages().catch(error=>toast(`消息已发送，汇总刷新失败：${error.message}`));
  } catch (error) { toast(error.message); }
  finally{draft.submitting=false;}
  controls();
});
$("prompt").addEventListener("input", () => {
  currentDraft().text=$("prompt").value;
  composerLayout.resize();
  controls();
});
$("prompt").addEventListener("paste",event=>{
  const files=[...(event.clipboardData?.items || [])].filter(item=>item.kind==="file"&&item.type.startsWith("image/")).map(item=>item.getAsFile()).filter(Boolean);
  if(files.length){event.preventDefault();pasteImages(files);const content=event.clipboardData.getData("text/plain");if(content){$("prompt").setRangeText(content,$("prompt").selectionStart,$("prompt").selectionEnd,"end");$("prompt").dispatchEvent(new Event("input"));}}
});
// Avoid navigating away when files are dropped. Only clipboard paste uploads images.
const dragFiles=event=>[...(event.dataTransfer?.types || [])].includes("Files");
document.addEventListener("dragover",event=>{if(dragFiles(event))event.preventDefault();});
document.addEventListener("drop",event=>{if(dragFiles(event))event.preventDefault();});
$("prompt").addEventListener("compositionstart", () => { state.composing = true; });
$("prompt").addEventListener("compositionend", () => { state.composing = false; });
$("prompt").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !state.composing && event.keyCode !== 229) { event.preventDefault(); $("composer").requestSubmit(); }
});
$("stop").addEventListener("click", async () => {
  if(!state.session || $("stop").disabled)return;
  const name = state.session.id;
  state.stopping=true;controls();
  $("runtime-label").textContent = "正在取消当前步骤…";
  try {
    await api(`/api/sessions/${encodeURIComponent(name)}/cancel`, {});
    if(state.session?.id===name && loopIsLive(state.session))state.session.status="stopping";
  } catch (error) { toast(error.message); }
  finally {state.stopping=false;controls();}
});
$("close-session").addEventListener("click", async () => {
  currentDraft().text=$("prompt").value;
  const name = state.session.id;
  try {
    if(!state.session.read_only)await api(`/api/sessions/${encodeURIComponent(name)}/close`, {});
    state.source?.close(); state.session = null; state.current = null;
    $("prompt").value="";renderAttachments();
    localStorage.removeItem("recoding-session");sessionStorage.removeItem('recoding-session'); inspector(); await listSessions();
  } catch (error) { toast(error.message); }
});
function sessionDialog(mode, directory) {
  if (!state.config) return;
  state.dialogMode = mode;
  const resume = mode === "resume";
  $("create-fields").hidden = resume; $("resume-fields").hidden = !resume;
  $("session-name").required = !resume; $("session-directory").required = !resume; $("resume-path").required = resume;
  $("session-dialog-title").textContent = resume ? "打开 Session 存档" : "新建 Session";
  $("session-dialog-note").textContent = resume ? "选择任意已有存档，打开后仅浏览；点击 Resume Master 才会启动。" : "选择工作目录，对话和图片保存在其 .recoding 下。";
  $("session-submit").textContent = resume ? "打开并浏览 ↗" : "创建并打开 ↗";
  const now = new Date(), pad = value => String(value).padStart(2, "0");
  $("session-name").value = `session-${now.getFullYear()}${pad(now.getMonth()+1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  $("session-directory").value = directory || state.session?.directory || state.config.session_root;
  modelOptions($("session-model"), state.config.demo ? "offline-demo" : state.config.model);
  $("session-model").disabled = state.config.demo;
  $("session-execution-mode").value=state.config.defaults?.execution_mode || "sandbox";
  $("dialog-error").textContent = "";
  $("dialog-path").textContent = "存档位置：工作目录/.recoding/独立Session文件夹";
  $("session-dialog").showModal(); (resume ? $("resume-path") : $("session-name")).focus();
}
function newSession(directory) {sessionDialog("create", directory);}
$("new-session").addEventListener("click", ()=>newSession());
$("resume-session").addEventListener("click", ()=>sessionDialog("resume"));
$("dismiss-dialog").addEventListener("click", ()=>$("session-dialog").close());
$("session-form").addEventListener("submit", async event=>{
  event.preventDefault();
  const reference = state.dialogMode === "resume"
    ? {action:"browse",path:$("resume-path").value.trim()}
    : {action:"create",name:$("session-name").value.trim(),directory:$("session-directory").value.trim(),execution_mode:$("session-execution-mode").value,
       ...(!state.config.demo ? {model:$("session-model").value} : {})};
  $("session-submit").disabled = true;
  try { if (await openSession(reference)) $("session-dialog").close(); }
  finally { $("session-submit").disabled = false; }
});
async function browseDirectory(path) {
  const picker = state.picker, request = (picker.request || 0) + 1; picker.request = request;
  $("use-directory").disabled = true;
  $("directory-error").textContent = "";
  try {
    const result = await api("/api/directories" + (path ? `?path=${encodeURIComponent(path)}` : ""));
    if (state.picker !== picker || picker.request !== request) return;
    state.picker.directory = result.directory; state.picker.parent = result.parent;
    $("directory-path").value = result.directory; $("directory-list").replaceChildren();
    for (const entry of result.entries) {
      const row = text("button", "", "directory-entry"); row.type="button";
      row.append(text("span",entry.session ? "▤" : "▱"),text("strong",entry.name),text("small",entry.session ? "Session" : "›"));
      row.addEventListener("click", ()=>browseDirectory(entry.path)); $("directory-list").append(row);
    }
    if (!result.entries.length) $("directory-list").append(text("div","此目录下没有子目录。","session-empty"));
    if (result.truncated) $("directory-error").textContent="仅显示前 500 个子目录，可直接输入完整路径。";
    $("use-directory").disabled = false;
  } catch(error) {if (state.picker === picker && picker.request === request) $("directory-error").textContent=error.message;}
}
function chooseDirectory(target) {
  if(!state.config || state.serverChanged)return;
  state.picker={target,directory:null,parent:null};
  $("directory-title").textContent=target==="resume" ? "选择 Session 存档文件夹" : "选择工作目录";
  $("directory-dialog").showModal();
  browseDirectory(target==="workspace" ? $("session-directory").value : target==="resume" ? $("resume-path").value || state.config.session_root : state.config.session_root);
}
$("choose-workspace").addEventListener("click",()=>chooseDirectory("workspace"));
$("choose-session").addEventListener("click",()=>chooseDirectory("resume"));
$("browse-workspace").addEventListener("click",()=>chooseDirectory("sidebar"));
$("open-workspace").addEventListener("click",()=>chooseDirectory("root"));
$("directory-form").addEventListener("submit",event=>{event.preventDefault();browseDirectory($("directory-path").value);});
$("directory-up").addEventListener("click",()=>browseDirectory(state.picker.parent));
$("dismiss-directory").addEventListener("click",()=>$("directory-dialog").close());
$("use-directory").addEventListener("click",()=>{
  const {target,directory}=state.picker;if(!directory)return;
  $("directory-dialog").close();
  if(target==="sidebar")newSession(directory);
  else if(target==="root")changeWorkspace(directory);
  else $(target==="resume" ? "resume-path" : "session-directory").value=directory;
});
// Layout/sound preferences belong to the browser; task and filesystem state
// always come from the server. Hiding a pane never closes a Session/runtime.
let hiddenPanels = {};
try {hiddenPanels = JSON.parse(localStorage.getItem("recoding-hidden-panels")) || {};} catch {}
for (const panel of ["sidebar", "inspector"]) document.body.classList.toggle(`${panel}-hidden`, hiddenPanels[panel] === true);
function syncPanelButtons() {
  const sidebar = innerWidth <= 700 ? document.body.classList.contains("sidebar-open") : !document.body.classList.contains("sidebar-hidden");
  const inspector = innerWidth <= 1120 ? document.body.classList.contains("inspector-open") : !document.body.classList.contains("inspector-hidden");
  $("menu").setAttribute("aria-expanded", String(sidebar));
  $("toggle-inspector").setAttribute("aria-expanded", String(inspector));
}
function togglePanel(panel, hide = false) {
  const overlay = innerWidth <= (panel === "sidebar" ? 700 : 1120);
  if (overlay) {
    document.body.classList.toggle(`${panel}-open`, hide ? false : !document.body.classList.contains(`${panel}-open`));
    if (!hide) document.body.classList.remove(`${panel === "sidebar" ? "inspector" : "sidebar"}-open`);
  } else {
    hiddenPanels[panel] = hide || !document.body.classList.contains(`${panel}-hidden`);
    document.body.classList.toggle(`${panel}-hidden`, hiddenPanels[panel]);
    try {localStorage.setItem("recoding-hidden-panels", JSON.stringify(hiddenPanels));} catch {}
  }
  syncPanelButtons(); window.dispatchEvent(new Event("panelschange"));
}
$("menu").addEventListener("click", () => togglePanel("sidebar"));
$("hide-sidebar").addEventListener("click", () => togglePanel("sidebar", true));
$("toggle-inspector").addEventListener("click", () => togglePanel("inspector"));
$("sidebar-shade").addEventListener("click", () => togglePanel("sidebar", true));
window.addEventListener("resize", syncPanelButtons);
syncPanelButtons();
document.addEventListener("keydown", event => {
  if(event.key==='Escape' && $("all-message-filter-panel").matches(':popover-open')){
    $("all-message-filter-panel").hidePopover();$("all-message-filter").focus();event.preventDefault();return;
  }
  if(event.key==="Escape"&&$("message-recipients").matches(":popover-open")){$("message-recipients").hidePopover();$("message-recipient").focus();event.preventDefault();return;}
  if(event.key==="Escape"&&$("composer-help").matches(":popover-open")){$("composer-help").hidePopover();$("composer-help-button").focus();event.preventDefault();return;}
  if(event.key==="Escape"&&$("session-menu").matches(":popover-open")){$("session-menu").hidePopover();event.preventDefault();return;}
  if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
    togglePanel("sidebar", true); return;
  }
  if (event.key === "Escape" && document.body.classList.contains("inspector-open")) {
    togglePanel("inspector", true); return;
  }
  if (event.key === "Escape" && !event.isComposing && !state.composing && !$("stop").disabled && !document.querySelector("dialog[open]")) $("stop").click();
  if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName) && !document.querySelector("dialog[open]")) newSession();
});
setInterval(() => {
  if(!$("workspace-sync").hidden && syncActive(workspaceSync.operation))renderWorkspaceSync(workspaceSync.operation);
  const target=state.agentId==="master" ? state.session : state.agentDetail;
  if (state.current && loopIsLive(target)) {
    if(agentStatus(target)==="sleeping") {
      const remaining=target.sleep?.deadline;
      $("elapsed").textContent=Number.isFinite(remaining) ? `休眠 · 剩余 ${Math.max(0,remaining-Date.now()/1000).toFixed(1)}s` : "休眠中";
    } else $("elapsed").textContent = `${((Date.now() - state.current.started) / 1000).toFixed(1)}s`;
    for (const model of state.current.models.values()) if (model.status === "running") {
      model.duration.textContent = seconds(Math.max(0, (Date.now() - model.started) / 1000));
    }
    for(const tool of state.current.tools.values())if(tool.name==="sleep" && tool.node.dataset.status==="running") {
      tool.status.textContent=`sleeping · ${seconds(Math.max(0,Date.now()/1000-tool.started))}`;
    }
  }
}, 250);
setInterval(()=>{if(state.session && !state.serverChanged)refreshExecutionTimes();},1000);
setInterval(()=>{if(state.session && !document.hidden && !state.serverChanged)AgentStatistics.advanceSpeed($("session-speed"));},1000);
setInterval(() => { if (state.config && !state.serverChanged && !document.hidden) listSessions().catch(() => {}); }, 2000);
document.addEventListener("visibilitychange", async () => {
  if(document.hidden){state.source?.close();state.source=null;return;}
  if(!state.config || !await verifyServiceInstance())return;
  AgentStatistics.advanceSpeed($("session-speed"));
  connectSessionEvents();scheduleNavigationRefresh();
  const op=workspaceSync.operation,request=op?.requests.at(-1);
  if(syncActive(op) && request)pollSyncProgress(op,request);
});
function releaseGuiConnection(){
  state.pageLeaving=true;state.source?.close();state.source=null;
  if(!state.guiConnected || !state.config?.csrf || state.serverChanged)return;
  state.guiConnected=false;
  const body=JSON.stringify({page:state.pageId,document:state.documentId,csrf:state.config.csrf,instance_id:state.config.instance_id});
  try{
    if(navigator.sendBeacon?.('/api/gui/release',new Blob([body],{type:'application/json'})))return;
  }catch{}
  fetch('/api/gui/release',{method:'POST',headers:{'Content-Type':'application/json'},body,keepalive:true}).catch(()=>{});
}
// Some desktop tab/window closes deliver beforeunload but no pagehide. Both
// paths are best effort and share an idempotent, document-fenced release.
window.addEventListener('beforeunload',releaseGuiConnection);
window.addEventListener('pagehide',releaseGuiConnection);
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});

// Browser-only layout preferences; independent of Agent events and Session state.
function initListLayout(listId,handleId,key,defaultHeight,bodyClass) {
  const list=$(listId),handle=$(handleId);
  let height=null,drag=null;
  try{const saved=JSON.parse(localStorage.getItem(key));if(Number.isFinite(saved))height=saved;}catch{}
  const maximum=()=>Math.max(48,Math.floor(Math.min(520,innerHeight*.55)));
  const clamp=value=>Math.round(Math.max(48,Math.min(maximum(),value)));
  function paint() {
    list.style.setProperty(`--${listId}-cap`,`${height===null ? Math.min(defaultHeight,maximum()) : maximum()}px`);
    if(height===null)list.style.removeProperty(`--${listId}-height`);else list.style.setProperty(`--${listId}-height`,`${clamp(height)}px`);
    handle.setAttribute("aria-valuemin","48");handle.setAttribute("aria-valuemax",maximum());
    handle.setAttribute("aria-valuenow",clamp(list.getBoundingClientRect().height));
  }
  function save(){try{localStorage.setItem(key,JSON.stringify(height));}catch{}}
  function finish(cancelled) {
    if(!drag)return;const previous=drag;drag=null;
    if(cancelled)height=previous.preferred;else save();
    handle.classList.remove("dragging");document.body.classList.remove(bodyClass);
    if(handle.hasPointerCapture(previous.pointerId))handle.releasePointerCapture(previous.pointerId);paint();
  }
  handle.addEventListener("pointerdown",event=>{
    if(event.button!==0||!event.isPrimary||drag)return;
    event.preventDefault();handle.focus({preventScroll:true});
    drag={pointerId:event.pointerId,y:event.clientY,height:list.getBoundingClientRect().height,preferred:height};
    handle.setPointerCapture(event.pointerId);handle.classList.add("dragging");document.body.classList.add(bodyClass);
  });
  handle.addEventListener("pointermove",event=>{if(drag?.pointerId===event.pointerId){height=clamp(drag.height+event.clientY-drag.y);paint();}});
  handle.addEventListener("pointerup",()=>finish(false));handle.addEventListener("pointercancel",()=>finish(true));handle.addEventListener("lostpointercapture",()=>finish(true));
  handle.addEventListener("dblclick",()=>{finish(true);height=null;paint();save();});
  handle.addEventListener("keydown",event=>{
    if(drag)return;
    if(["ArrowUp","ArrowDown"].includes(event.key))height=clamp(list.getBoundingClientRect().height+(event.key==="ArrowDown"?1:-1)*(event.shiftKey?40:10));
    else if(event.key==="Home")height=48;else if(event.key==="End")height=maximum();else if(event.key==="Enter")height=null;else return;
    event.preventDefault();paint();save();
  });
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&drag){event.preventDefault();event.stopImmediatePropagation();finish(true);}},true);
  window.addEventListener("blur",()=>finish(true));window.addEventListener("resize",()=>{finish(true);paint();});
  new ResizeObserver(paint).observe(list);paint();
}
initListLayout("agent-list","agent-list-resizer","recoding-agent-list-height",128,"resizing-agents");
initListLayout("job-list","job-list-resizer","recoding-job-list-height",160,"resizing-jobs");

function initComposerLayout() {
  const handle=$("composer-resizer"),input=$("prompt");
  let height=160,drag=null;
  const bounds=()=>{
    const max=Math.max(24,Math.floor(Math.min(320,innerHeight*.4,$("conversation-panel").clientHeight*.5)));
    return {min:24,max};
  };
  const clamp=value=>{const range=bounds();return Math.round(Math.max(range.min,Math.min(range.max,value)));};
  function paint(){
    const range=bounds(),size=clamp(height);
    input.style.maxHeight=`${size}px`;
    const scroll=input.scrollTop;input.style.height="0px";
    const content=input.scrollHeight;
    input.style.height=`${Math.max(24,Math.min(size,content))}px`;
    input.style.overflowY=content>size ? "auto" : "hidden";
    input.scrollTop=scroll;
    handle.setAttribute("aria-valuemin",range.min);handle.setAttribute("aria-valuemax",range.max);handle.setAttribute("aria-valuenow",size);
    handle.setAttribute("aria-valuetext",`最大 ${size} 像素`);
  }
  function save(){saveComposerPreference({height});}
  function finish(cancelled){
    if(!drag)return;
    const previous=drag;drag=null;
    if(cancelled)height=previous.preferred;else save();
    handle.classList.remove("dragging");document.body.classList.remove("resizing-composer");
    if(handle.hasPointerCapture(previous.pointerId))handle.releasePointerCapture(previous.pointerId);
    paint();
  }
  handle.addEventListener("pointerdown",event=>{
    if(event.button!==0||!event.isPrimary||drag)return;
    event.preventDefault();handle.focus({preventScroll:true});
    drag={pointerId:event.pointerId,y:event.clientY,height:clamp(height),preferred:height};
    handle.setPointerCapture(event.pointerId);handle.classList.add("dragging");document.body.classList.add("resizing-composer");
  });
  handle.addEventListener("pointermove",event=>{if(drag?.pointerId===event.pointerId){height=clamp(drag.height+drag.y-event.clientY);paint();}});
  handle.addEventListener("pointerup",()=>finish(false));handle.addEventListener("pointercancel",()=>finish(true));handle.addEventListener("lostpointercapture",()=>finish(true));
  handle.addEventListener("dblclick",()=>{height=160;paint();save();});
  handle.addEventListener("keydown",event=>{
    if(drag)return;const range=bounds(),step=event.shiftKey?50:10;
    if(event.key==="ArrowUp"||event.key==="ArrowDown")height=clamp(clamp(height)+(event.key==="ArrowUp"?step:-step));
    else if(event.key==="Home"||event.key==="End")height=event.key==="Home"?range.min:range.max;
    else if(event.key==="Enter")height=160;else return;
    event.preventDefault();paint();save();
  });
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&drag){event.preventDefault();event.stopPropagation();finish(true);}},true);
  window.addEventListener("blur",()=>finish(true));window.addEventListener("resize",()=>{finish(true);paint();});
  new ResizeObserver(paint).observe($("conversation-panel"));
  new ResizeObserver(paint).observe($("runtime-label").parentElement);
  paint();
  return {resize:paint,load(){
    finish(true);
    const saved=composerPreference();height=Number.isFinite(saved.height) ? saved.height : 160;
    paint();
  }};
}
const composerLayout=initComposerLayout();

function initLatestButton() {
  const scroller=$("conversation-scroll"),button=$("jump-to-latest");
  let scheduled=false;
  const paint=()=>{
    scheduled=false;
    button.hidden=!state.session || scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<80;
  };
  const schedule=()=>{if(!scheduled){scheduled=true;requestAnimationFrame(paint);}};
  scroller.addEventListener("scroll",schedule,{passive:true});
  const observer=new ResizeObserver(schedule);observer.observe(scroller);observer.observe($("messages"));
  button.addEventListener("click",()=>{scrollToEnd();paint();});
  schedule();
}
initLatestButton();

function initColumnResizers() {
  const root = document.documentElement, storageKey = "recoding-column-widths";
  const handles = {sidebar: $("sidebar-resizer"), inspector: $("inspector-resizer")};
  const minimum = {sidebar: 220, inspector: 240}, maximum = {sidebar: 420, inspector: 520};
  const conversationMinimum = 360, gutter = 6;
  let preferred = {}, widths = {}, drag = null;
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    for (const panel of Object.keys(handles)) if (Number.isFinite(saved?.[panel])) {
      preferred[panel] = Math.min(maximum[panel], Math.max(minimum[panel], saved[panel]));
    }
  } catch {} // Storage can be unavailable, or contain an older/invalid value.

  const viewport = () => root.clientWidth;
  const defaults = () => ({sidebar: viewport() >= 1550 ? 265 : viewport() <= 1120 ? 220 : 248,
    inspector: viewport() >= 1550 ? 310 : 285});
  const visible = panel => viewport() > (panel === "sidebar" ? 700 : 1120) && !document.body.classList.contains(`${panel}-hidden`);
  const clamp = (value, min, max) => Math.round(Math.max(min, Math.min(max, value)));
  function bounds(panel) {
    const other = panel === "sidebar" ? "inspector" : "sidebar";
    const available = viewport() - conversationMinimum - gutter - (visible(other) ? widths[other] + gutter : 0);
    return {min: minimum[panel], max: Math.max(minimum[panel], Math.min(maximum[panel], available))};
  }
  function paint() {
    const fallback = defaults(), wanted = {...fallback, ...preferred};
    // Shrinking the viewport fits the visible panes without replacing saved preferences.
    const sidebarMax = viewport() - conversationMinimum - gutter - (visible("inspector") ? minimum.inspector + gutter : 0);
    widths.sidebar = clamp(wanted.sidebar, minimum.sidebar, Math.min(maximum.sidebar, Math.max(minimum.sidebar, sidebarMax)));
    const inspectorMax = viewport() - conversationMinimum - gutter - (visible("sidebar") ? widths.sidebar + gutter : 0);
    widths.inspector = visible("inspector")
      ? clamp(wanted.inspector, minimum.inspector, Math.min(maximum.inspector, inspectorMax)) : wanted.inspector;
    for (const [panel, handle] of Object.entries(handles)) {
      root.style.setProperty(`--${panel}-width`, `${widths[panel]}px`);
      const limit = bounds(panel);
      handle.setAttribute("aria-valuemin", limit.min);
      handle.setAttribute("aria-valuemax", limit.max);
      handle.setAttribute("aria-valuenow", widths[panel]);
      handle.setAttribute("aria-valuetext", `${widths[panel]} 像素`);
    }
  }
  function save() {
    try { localStorage.setItem(storageKey, JSON.stringify(preferred)); } catch {}
  }
  function change(panel, value, base = widths) {
    const limit = bounds(panel);
    // A deliberate edit starts from the currently visible layout.
    preferred = {...base, [panel]: clamp(value, limit.min, limit.max)};
    paint();
  }
  function finish(cancelled) {
    if (!drag) return;
    const previous = drag; drag = null;
    if (cancelled) { preferred = previous.preferred; paint(); } else save();
    const handle = handles[previous.panel];
    handle.classList.remove("dragging"); document.body.classList.remove("resizing-columns");
    if (handle.hasPointerCapture(previous.pointerId)) handle.releasePointerCapture(previous.pointerId);
  }
  for (const [panel, handle] of Object.entries(handles)) {
    handle.addEventListener("pointerdown", event => {
      if (event.button !== 0 || !event.isPrimary || !visible(panel) || drag) return;
      event.preventDefault(); handle.focus({preventScroll: true});
      drag = {panel, pointerId: event.pointerId, x: event.clientX, width: widths[panel],
        widths: {...widths}, preferred: {...preferred}};
      handle.setPointerCapture(event.pointerId);
      handle.classList.add("dragging"); document.body.classList.add("resizing-columns");
    });
    handle.addEventListener("pointermove", event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const delta = (event.clientX - drag.x) * (panel === "sidebar" ? 1 : -1);
      change(panel, drag.width + delta, drag.widths);
    });
    handle.addEventListener("pointerup", event => { if (drag?.pointerId === event.pointerId) finish(false); });
    handle.addEventListener("pointercancel", () => finish(true));
    handle.addEventListener("lostpointercapture", () => finish(true));
    handle.addEventListener("dblclick", () => { delete preferred[panel]; paint(); save(); });
    handle.addEventListener("keydown", event => {
      if (!visible(panel) || drag) return;
      const limit = bounds(panel), step = event.shiftKey ? 50 : 10;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        change(panel, widths[panel] + (event.key === "ArrowRight" ? step : -step) * (panel === "sidebar" ? 1 : -1));
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault(); change(panel, event.key === "Home" ? limit.min : limit.max);
      } else if (event.key === "Enter") {
        event.preventDefault(); delete preferred[panel]; paint();
      } else return;
      save();
    });
  }
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && drag) {
      event.preventDefault(); event.stopPropagation(); finish(true);
    }
  }, true);
  window.addEventListener("blur", () => finish(true));
  window.addEventListener("resize", () => { finish(true); paint(); });
  window.addEventListener("panelschange", () => { finish(true); paint(); });
  paint();
}
initColumnResizers();

async function start() {
  const op=beginWorkspaceSync("startup");
  syncPhase(op,"连接后端并读取配置");
  try {
    await prepareGuiIdentity();
    state.config = await api("/api/config");
    AgentStatistics.configureModels(state.config.model_profiles,state.config.model_style);
    if(state.config.api_version!==4 || !state.config.capabilities?.persistent_agent_loops)throw new Error("此页面需要原生常驻 Agent 服务（API 4）；请重启当前版本的 recoding-web.py。");
    op.path=state.config.session_root;syncPhase(op,null);
    if(!state.config.capabilities?.session_delete)serverWarning(restartServerMessage);
    renderModelSelection();
    $("root-path").textContent = state.config.session_root; $("root-path").title = state.config.session_root;
    if (state.config.demo) {
      $("mode-badge").textContent = "OFFLINE DEMO"; $("mode-badge").classList.add("demo");
      $("model-mode").textContent = "固定响应 · 真实执行工具";
    } else if (!state.config.key_configured) {
      $("composer-note").textContent = "RECODING_API_KEY 未设置；请填写项目 .env 或设置环境变量后重启服务，或使用 --demo。";
      $("composer-note").hidden = false;
    }
    const sessions = await listSessions({operation:op,keepOpen:true});
    let saved;
    try {saved=JSON.parse(sessionStorage.getItem('recoding-session') || localStorage.getItem("recoding-session"));} catch {}
    if(saved?.id && sessions.some(session=>session.id===saved.id)) await openSession({action:"browse",id:saved.id},{operation:op,reveal:false,startupSessions:sessions});
    else if(saved?.path) await openSession({action:"browse",path:saved.path},{operation:op,reveal:false,startupSessions:sessions});
    else finishWorkspaceSync(sessions,op);
    controls();
  } catch (error) { failWorkspaceSync(error,op);toast(`无法连接后端服务：${error.message}`); }
}
initAllAgentsComposer();
initAllMessageFilters();
start();

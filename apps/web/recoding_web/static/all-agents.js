"use strict";

// A correspondence-only projection. It never becomes an Agent or model context.
const allAgentViews=new Map();
const allRecipientGroups=new Map([["all","all"],["all_except_master","all（除了 master）"]]);
const isAllAgentsView=()=>state.agentId==="all";
function allView(){
  const id=state.session.id;
  if(!allAgentViews.has(id))allAgentViews.set(id,{messages:new Map(),cards:new Map(),loaded:false,more:false});
  return allAgentViews.get(id);
}
function allRecipient(){
  const saved=composerPreference().recipient || "master";
  const agents=acceptingAgents();
  if(allRecipientGroups.has(saved) || agents.some(agent=>agent.id===saved))return saved;
  return agents.some(agent=>agent.id==="master") ? "master" : agents[0]?.id || "all";
}
function allRecipientAgents(recipient=allRecipient()){
  return acceptingAgents().filter(agent=>recipient==="all" || recipient==="all_except_master" && agent.id!=="master" || agent.id===recipient);
}
function composerRecipient(){return isAllAgentsView() ? allRecipient() : state.agentId;}
function composerAgent(){
  const id=composerRecipient();
  if(isAllAgentsView() && allRecipientGroups.has(id))return allRecipientAgents(id)[0] || null;
  return ["master","all"].includes(id) ? state.session : state.session?.agents?.find(agent=>agent.id===id);
}
function renderAllRecipientControls(){
  const button=$("message-recipient");button.hidden=!state.session || !isAllAgentsView();
  if(button.hidden)return;
  const recipient=allRecipient();button.disabled=state.switching || state.serverChanged || currentDraft().submitting;
  button.replaceChildren(text("span","@"));
  if(!allRecipientGroups.has(recipient))button.append(agentIconNode(recipient));
  button.append(text("span",allRecipientGroups.get(recipient) || agentName(recipient),"recipient-name"),text("span","▾"));
  button.title=recipient==="all" ? "发给所有存活的 Agents（含存活的 Master）" : recipient==="all_except_master" ? "发给所有存活的 sub-agents，排除 Master" : `发给 ${agentName(recipient)}`;
  const panel=$("message-recipients"),agents=acceptingAgents().map(agent=>({...agent,name:agentName(agent.id)}));
  const signature=JSON.stringify([recipient,agents.map(agent=>[agent.id,agent.name,agentIcon(agent.id)])]);
  if(panel.dataset.signature!==signature){
    panel.replaceChildren(text("strong","发送给","recipient-heading"));
    const groups=text("div","","recipient-groups");panel.append(groups);
    const choices=[...[...allRecipientGroups].map(([id,label])=>({id,name:`@${label}`})),...agents];
    for(const agent of choices){
      const row=text("button","","recipient-option");row.type="button";row.dataset.recipient=agent.id;
      row.setAttribute("aria-pressed",String(recipient===agent.id));
      const identity=text("span","","recipient-identity");
      if(!allRecipientGroups.has(agent.id))identity.append(agentIconNode(agent.id));
      identity.append(text("span",agent.name));row.append(identity,text("small",""));
      row.addEventListener("click",()=>{
        saveComposerPreference({recipient:agent.id});panel.hidePopover();controls();$("prompt").focus();
      });(allRecipientGroups.has(agent.id) ? groups : panel).append(row);
    }
    if(!agents.length)panel.append(text("p","当前没有存活的 Agent","recipient-empty"));
    panel.dataset.signature=signature;
  }
  for(const row of panel.querySelectorAll(".recipient-option")){
    const id=row.dataset.recipient,group=allRecipientGroups.has(id),targets=allRecipientAgents(id);
    row.disabled=group && !targets.length;
    row.querySelector("small").textContent=group ? `${targets.length} live` : agents.find(agent=>agent.id===id)?.status || "";
  }
}
function positionMessageRecipients(){
  const panel=$("message-recipients");if(!panel.matches(":popover-open"))return;
  const anchor=$("message-recipient").getBoundingClientRect(),margin=12;
  panel.style.left=`${Math.max(margin,Math.min(anchor.left,innerWidth-panel.offsetWidth-margin))}px`;
  panel.style.top=`${Math.max(margin,anchor.top-panel.offsetHeight-8)}px`;
}
function initAllAgentsComposer(){
  $("message-recipients").addEventListener("toggle",()=>{
    $("message-recipient").setAttribute("aria-expanded",String($("message-recipients").matches(":popover-open")));
    positionMessageRecipients();
  });
  window.addEventListener("resize",positionMessageRecipients);
}

const messageFilterFields=[['senders','发送者'],['recipients','接收者']];
function allMessageFilters(){
  const saved=composerPreference().messageFilters || {};
  return Object.fromEntries(messageFilterFields.map(([key])=>[key,Array.isArray(saved[key]) ?
    [...new Set(saved[key].filter(id=>typeof id==='string' && (key!=='senders' || id!=='user')))] : null]));
}
function allMessageMatches(group,filters=allMessageFilters()){
  if(group.source==='user')return true;
  return (filters.senders===null || filters.senders.includes(group.source)) &&
    (filters.recipients===null || group.targets.some(id=>filters.recipients.includes(id)));
}
function setAllMessageFilter(key,selection){
  if(!state.session || !isAllAgentsView())return;
  preserveConversationPosition(()=>{
    const filters=allMessageFilters();
    if(key)filters[key]=selection;
    else for(const [field] of messageFilterFields)filters[field]=null;
    saveComposerPreference({messageFilters:filters});renderAllMessages();
  });
}
function messageFilterPeople(){
  return [{id:'user',name:'我'},...acceptingAgents().map(agent=>({id:agent.id,name:agentName(agent.id)}))];
}
function renderAllMessageFilters(){
  const button=$('all-message-filter'),panel=$('all-message-filter-panel');
  button.hidden=!state.session || !isAllAgentsView();
  if(button.hidden){
    if(panel.matches(':popover-open'))panel.hidePopover();
    return;
  }
  const filters=allMessageFilters(),people=messageFilterPeople();
  button.title=messageFilterFields.map(([key,label])=>`${label}：${filters[key]===null ? '全部' : filters[key].map(id=>id==='user' ? '我' : agentName(id)).join('、') || '未选择'}`).join('；');
  const signature=JSON.stringify([state.session.id,people.map(person=>[person.id,person.name,person.id==='user' ? '' : agentIcon(person.id)])]);
  if(panel.dataset.signature!==signature){
    panel.replaceChildren(text('strong','筛选消息'),text('p','发送者与接收者条件同时满足；你的消息始终显示。候选仅列存活的 Agent 和你。','filter-picker-help'));
    const fields=text('div','','filter-picker-fields');
    for(const [key,label] of messageFilterFields){
      const field=document.createElement('fieldset');field.dataset.filterField=key;
      field.append(text('legend',label));
      const actions=text('div','','filter-picker-actions'),all=text('label','','filter-person');
      const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.dataset.action='all';
      checkbox.setAttribute('aria-label',`${label}全部`);
      checkbox.addEventListener('change',()=>setAllMessageFilter(key,checkbox.checked ? null : []));
      all.append(checkbox,text('span','全部'));actions.append(all);
      const clear=text('button',key==='senders' ? '只看我' : '清空','text-button');clear.type='button';clear.dataset.action='clear';
      clear.addEventListener('click',()=>setAllMessageFilter(key,[]));actions.append(clear);field.append(actions);
      const list=text('div','','filter-person-list');
      for(const person of people){
        const row=text('label','','filter-person'),input=document.createElement('input');input.type='checkbox';
        input.dataset.personId=person.id;input.setAttribute('aria-label',person.name);
        const identity=text('span',person.id==='user' ? '你' : '','filter-person-icon');
        if(person.id!=='user')identity.append(agentIconNode(person.id));
        row.append(input,identity,text('span',person.name,'filter-person-name'));
        if(key==='senders' && person.id==='user'){
          input.disabled=true;row.title='你发出的消息始终显示';row.append(text('small','始终显示'));
        }else input.addEventListener('change',()=>{
          const candidates=messageFilterPeople().filter(person=>key!=='senders' || person.id!=='user').map(person=>person.id);
          const selected=new Set(allMessageFilters()[key] ?? candidates);
          if(input.checked)selected.add(person.id);else selected.delete(person.id);
          setAllMessageFilter(key,candidates.every(id=>selected.has(id)) ? null : [...selected]);
        });
        list.append(row);
      }
      field.append(list);fields.append(field);
    }
    const reset=text('button','重置为全部','text-button');reset.type='button';reset.id='reset-message-filters';
    reset.addEventListener('click',()=>setAllMessageFilter(null,null));
    panel.append(fields,reset);panel.dataset.signature=signature;
  }
  for(const [key] of messageFilterFields){
    const field=panel.querySelector(`[data-filter-field=${key}]`),selection=filters[key];
    const inputs=[...field.querySelectorAll('input[data-person-id]')];
    for(const input of inputs)input.checked=input.disabled || selection===null || selection.includes(input.dataset.personId);
    const all=field.querySelector('[data-action=all]');all.checked=selection===null || inputs.every(input=>input.checked);
    all.indeterminate=!all.checked && inputs.some(input=>!input.disabled && input.checked);
  }
}
function positionMessageFilter(){
  const panel=$('all-message-filter-panel');if(!panel.matches(':popover-open'))return;
  const anchor=$('all-message-filter').getBoundingClientRect(),margin=12;
  panel.style.left=`${Math.max(margin,Math.min(anchor.left,innerWidth-panel.offsetWidth-margin))}px`;
  const above=anchor.top-panel.offsetHeight-8;
  panel.style.top=`${above>=margin ? above : Math.max(margin,Math.min(anchor.bottom+6,innerHeight-panel.offsetHeight-margin))}px`;
}
function initAllMessageFilters(){
  $('all-message-filter').addEventListener('click',event=>{
    event.preventDefault();
    const panel=$('all-message-filter-panel');
    if(panel.matches(':popover-open'))panel.hidePopover();
    else {panel.showPopover();positionMessageFilter();}
  });
  $('all-message-filter-panel').addEventListener('toggle',()=>{
    $('all-message-filter').setAttribute('aria-expanded',String($('all-message-filter-panel').matches(':popover-open')));
    positionMessageFilter();
  });
  window.addEventListener('resize',positionMessageFilter);
  window.addEventListener('panelschange',positionMessageFilter);
}
async function replyToAllMessage(message){
  if(!state.session || !isAllAgentsView())return;
  const sessionId=state.session.id;
  const own=message.source==='user',target=own ? message.targets[0] : message.source;
  const recipient=own && message.targets.length>1 ? message.broadcast_scope || 'all' : target;
  if(allRecipientAgents(recipient).length){
    saveComposerPreference({recipient});controls();
  }else{
    // Stopped senders remain replyable through their own page, without waking them.
    await selectAgent(target);
    if(state.session?.id!==sessionId || state.agentId!==target)return;
  }
  const input=$('prompt');input.focus({preventScroll:true});input.setSelectionRange(input.value.length,input.value.length);
}

function allMessageGroups(view){
  const groups=new Map();
  for(const message of [...view.messages.values()].sort((a,b)=>a.timestamp-b.timestamp || a.id.localeCompare(b.id))){
    const key=message.broadcast_id ? `${message.kind}/${message.source}/${message.broadcast_id}/${message.delivery || "to"}` : message.id;
    if(!groups.has(key))groups.set(key,{...message,key,targets:[],ids:[]});
    const group=groups.get(key);group.ids.push(...[message.id,...(message.original_message_ids || [])].filter(id=>!group.ids.includes(id)));
    if(!group.targets.includes(message.target))group.targets.push(message.target);
  }
  return [...groups.values()];
}
function renderAllMessages({earlier=false,tail=false}={}){
  if(!state.session || !isAllAgentsView())return;
  renderAllMessageFilters();
  const filters=allMessageFilters(),view=allView(),container=$("messages"),scroller=$("conversation-scroll"),top=scroller.scrollTop;
  const anchor=earlier ? [...container.querySelectorAll(".all-agent-message")].find(node=>node.getBoundingClientRect().bottom>scroller.getBoundingClientRect().top) : null;
  const offset=anchor?.getBoundingClientRect().top;
  for(const node of container.querySelectorAll(".all-messages-empty,.earlier-history"))node.remove();
  const groups=allMessageGroups(view),keys=new Set(groups.map(group=>group.key));
  for(const [key,card] of view.cards)if(!keys.has(key)){card.remove();view.cards.delete(key);}
  let previous=null;
  let visible=0;
  for(const group of groups){
    const matches=detailMode!=='filter' || allMessageMatches(group,filters);
    if(matches && (detailMode!=='human' || group.kind!=='peer'))visible++;
    let card=view.cards.get(group.key);
    if(!card){
      card=text("article","",`${group.kind==="human_input" ? "user-followup" : group.kind==="human_output" ? "human-message" : "peer-message"} conversation-message all-agent-message`);
      card.tabIndex=-1;card.dataset.messageId=group.id;card.dataset.kind=group.kind;
      card.heading=text("div","","conversation-heading");card.body=text("div","","final-response");
      card.append(card.heading,card.body);view.cards.set(group.key,card);
    }
    const signature=JSON.stringify([group.content,group.images,group.image_owner,group.delivery,group.to,group.cc,group.targets.map(id=>[id,agentName(id),agentIcon(id)]),agentName(group.source),agentIcon(group.source)]);
    if(card.signature!==signature){
      const stamp=text("time","","message-time"),senderIsAgent=group.source!=="user";
      const revealSource=()=>revealAgentMessage({sessionId:state.session.id,agentId:group.source,messageIds:card.dataset.messageIds.split(",")});
      const avatar=text(senderIsAgent ? "button" : "span",senderIsAgent ? "" : "你","conversation-avatar");
      if(senderIsAgent){
        avatar.type="button";avatar.classList.add("message-sender-link");avatar.append(agentIconNode(group.source));
        avatar.title=`查看 ${agentName(group.source)} 的这条消息`;avatar.setAttribute("aria-label",avatar.title);
        avatar.addEventListener("click",()=>revealSource().catch(error=>toast(error.message)));
      }
      card.heading.replaceChildren(avatar,messageRoute(group.source,group.delivery==='cc' ? group.to : group.targets,{revealSource:senderIsAgent ? revealSource : null}));
      if(group.cc?.length)card.heading.append(ccBadge(group.cc,{copy:group.delivery==='cc',targets:group.targets}));
      card.classList.toggle('cc-message',group.delivery==='cc');
      if(group.broadcast_id && group.delivery!=='cc' && group.targets.length>1)card.heading.append(text("span",group.source==="user" ? `@${allRecipientGroups.get(group.broadcast_scope) || "all"}` : "群发","peer-role"));
      if(group.kind==="peer")card.heading.append(text("span",group.delivery==='cc' ? 'Agent 抄送' : "Agent 对话","peer-role"));
      const reply=text('button',group.source==='user' ? '继续对话' : '↩ 回复','text-button message-reply');reply.type='button';
      const target=group.source==='user' ? group.targets[0] : group.source;
      reply.title=group.source==='user' ? '继续给原接收者发消息' : `回复 ${agentName(target)}`;
      reply.setAttribute('aria-label',reply.title);
      reply.addEventListener('click',()=>replyToAllMessage(group).catch(error=>toast(error.message)));
      card.heading.append(stamp,reply);setMessageTime(stamp,group.timestamp);
      renderMessageBody(card.body,group.content,group.images,group.image_owner);card.signature=signature;
    }
    card.hidden=!matches;
    card.dataset.source=group.source;card.dataset.target=group.targets.join(",");card.dataset.messageIds=group.ids.join(",");
    if(previous ? previous.nextElementSibling!==card : container.firstElementChild!==card){
      if(previous)previous.after(card);else container.prepend(card);
    }
    previous=card;
  }
  if(view.more){
    const button=text("button","↑ 加载更早的消息","earlier-history");button.type="button";button.disabled=Boolean(view.loadingEarlier);
    button.addEventListener("click",()=>loadEarlierAllMessages().catch(error=>toast(error.message)));container.prepend(button);
  }
  if(groups.length && !visible)container.append(text('p','当前筛选下没有匹配的已加载消息。'+(view.more ? '可加载更早消息继续查找。' : ''),'all-messages-empty'));
  if(!groups.length)container.append(text("p",view.loaded ? "还没有消息。默认发送给 Master，也可以用 @ 选择接收者。" : "正在加载所有 Agent 的消息…","all-messages-empty"));
  if(tail)scrollToEnd();
  else if(anchor?.isConnected)scroller.scrollTop=top+anchor.getBoundingClientRect().top-offset;
  else scroller.scrollTop=top;
}
async function syncAllMessages(){
  if(!state.session || !isAllAgentsView() || state.serverChanged)return;
  const sessionId=state.session.id,view=allView();
  if(view.loading){view.dirty=true;return view.loading;}
  view.loading=(async()=>{
    do{
      view.dirty=false;
      const query=new URLSearchParams({limit:"100"});
      if(view.loaded)query.set("after",view.after || "start");
      const page=await api(`/api/sessions/${encodeURIComponent(sessionId)}/messages?${query}`);
      if(state.session?.id!==sessionId)return;
      for(const message of page.messages)view.messages.set(message.id,message);
      if(!view.loaded){view.before=page.before;view.more=page.has_more;}
      if(page.after)view.after=page.after;
      const moreForward=view.loaded&&page.has_more;view.loaded=true;
      if(isAllAgentsView())renderAllMessages();
      if(moreForward)view.dirty=true;
    }while(view.dirty && state.session?.id===sessionId && isAllAgentsView());
  })();
  try{await view.loading;}finally{view.loading=null;}
}
let allMessagesTimer;
function scheduleAllMessages(){
  if(!isAllAgentsView())return;
  if(allMessagesTimer)return;
  allMessagesTimer=setTimeout(()=>{allMessagesTimer=null;syncAllMessages().catch(error=>toast(error.message));},40);
}
async function loadEarlierAllMessages(){
  const sessionId=state.session.id,view=allView();if(!view.before || view.loadingEarlier)return false;
  view.loadingEarlier=true;
  try{
    const page=await api(`/api/sessions/${encodeURIComponent(sessionId)}/messages?before=${encodeURIComponent(view.before)}&limit=100`);
    if(state.session?.id!==sessionId)return false;
    for(const message of page.messages)view.messages.set(message.id,message);
    view.before=page.before || view.before;view.more=page.has_more;
    if(isAllAgentsView())renderAllMessages({earlier:true});return page.messages.length>0;
  }finally{view.loadingEarlier=false;if(isAllAgentsView())renderAllMessages();}
}
async function selectAllAgents(){
  const sessionId=state.session.id,version=++state.agentOpening;
  currentDraft().text=$("prompt").value;
  modelExcerptObserver.disconnect();state.agentId="all";state.agentDetail=null;
  state.views.clear();state.current=null;state.eventLog.clear();state.imageAssets.clear();state.userInputs.clear();state.systemInputs.clear();
  $("messages").replaceChildren();allView().cards.clear();$("welcome").hidden=true;
  const initiallyEmpty=!allView().messages.size;
  $("prompt").value=currentDraft().text;renderAttachments();loadComposerPreferences();renderAllMessages({tail:true});inspector();
  await syncAllMessages();
  if(isAllAgentsView() && state.session?.id===sessionId && state.agentOpening===version){readHumanMessages();inspector();if(initiallyEmpty)scrollToEnd();}
}

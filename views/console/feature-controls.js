/* A consistent feature header. Saves only its own switch/scope, never other drafts. */
(()=>{
 const defs={
  sightings:['网站 S 怪播报','限定接收群','同时控制实际出现与可触发提醒。留空沿用下方接收群；填写后只在已有接收群中筛选，不会新增发送目标。'],
  forwarding:['群消息转发','生效来源群','留空沿用全部转发规则来源群；填写后仅处理这些来源群。具体目标群与关键词仍在下方逐条设置。'],
  text:['文本与地图','生效来源群','控制地图规范化、S / SS 简称、词条修饰、信息补全与配图。留空应用于全部转发来源群；关闭后仍可按规则转发原消息。目标群沿用转发规则。'],
  collection:['查漏与采集','生效来源群','同时控制查漏补发与 S 怪记录采集。留空保持现有范围；填写后只采集、查漏这些来源群。扩散群与通知群仍在下方分别设置。'],
  mango:['芒果广播','生效来源群','留空沿用下方招募来源群；填写后进一步限定来源。广播目标群和选点行为保留原配置。'],
  hunt:['狩猎提醒','生效来源群','留空沿用下方 SS 前置来源群；填写后限定采集来源。定时超时提醒仍按下方目标群发送，受本页开关控制。'],
  trains:['车表与指令','允许使用的群','留空保持原有群聊和私聊查询；填写后仅回复这些群里的指令，私聊不回复。自动狩猎车提醒的目标群也须在此范围内；提醒接收群在下方单独设置。下方每条指令仍可单独开关。'],
  manual:['手动群发','允许发送的目标群','留空可从机器人已加入的群中选择；填写后只允许向这些群发送。此处不会自动发送，仍需在下方选择群并确认。关闭后仍可撤回已发送消息。'],
  preview:['效果预览','允许预览的来源群','仅生成本地效果，不发送到群。留空可预览所有来源群；填写后限制下方来源群号。预览同时遵守转发、文字地图开关和群范围。'],
 };
 const cards=new Map();let state,pending,advancedRoutes;
 const api=(path,body)=>body===undefined?window.AstrBotPluginView.apiGet('webui/'+path):window.AstrBotPluginView.apiPost('webui/'+path,body);
 const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
 const style=`.feature-scope{margin:20px 0;padding:22px;border:1px solid var(--border,var(--line));border-radius:13px;background:var(--panel);color:var(--text,var(--ink));}.feature-scope h2{font-size:16px;margin:0}.feature-scope .fs-head{display:flex;justify-content:space-between;align-items:center;gap:18px}.feature-scope .fs-check{display:flex;align-items:center;gap:9px;white-space:nowrap;margin:0;font-size:13px}.feature-scope input[type=checkbox]{width:17px;height:17px;margin:0;accent-color:#316cf5}.feature-scope p,.feature-scope .fs-state{font-size:12px;line-height:1.8;color:var(--muted);margin:12px 0}.feature-scope .fs-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.feature-scope .fs-field{font-size:12px;display:block;margin:0}.feature-scope .fs-field span{display:block;margin:0 0 8px}.feature-scope textarea{min-height:86px;max-height:220px;width:100%;font:12px/1.8 inherit;resize:vertical;border:1px solid var(--border,var(--line));border-radius:9px;padding:10px 12px;color:inherit;background:var(--input,var(--panel))}.feature-scope .fs-routes{font-size:12px;line-height:1.8;overflow-wrap:anywhere}.feature-scope .fs-routes b{display:block;font-size:12px;margin-bottom:8px}.feature-scope .fs-routes div{margin:6px 0;color:var(--muted)}.feature-scope .fs-actions{display:flex;align-items:center;gap:14px;margin-top:15px}.feature-scope .fs-state{margin:0}.feature-scope .fs-error{color:var(--danger)}.feature-scope .fs-advanced{display:flex;align-items:center;gap:10px;font-size:13px;margin-top:15px}@media(max-width:700px){.feature-scope .fs-grid{grid-template-columns:1fr}.feature-scope .fs-head{align-items:flex-start;flex-wrap:wrap}}`;
 function message(card,text,error=false){card.status.textContent=text;card.status.classList.toggle('fs-error',error)}
 function routes(card){
  const r=state.routes[card.key]||{};card.routes.replaceChildren(el('b','当前群路由'));
  for(const [field,label] of [['sources','已配置来源'],['targets','已配置目标']])if(r[field])card.routes.append(el('div',label+'：'+(window.BearGroupNames.list(r[field])||'未设置')));
  if(card.key==='trains')card.routes.append(el('div','查询和图片回复到发出指令的会话；自动文字提醒按下方提醒群发送。'));
  else if(!r.sources&&!r.targets)card.routes.append(el('div',card.key==='manual'?'从下方机器人群列表选择实际发送群。':'回复到发出指令的群，不额外广播。'));
  card.routes.append(el('div',(card.key==='manual'?'发送账号：按下方所选机器人':card.key==='trains'?'回复账号：收到指令的机器人':'自动发送账号：'+(state.platform_id||'未设置'))+' · '+(state.within_broadcast_window?'当前允许发送':'当前为统一静默时段')));
 }
 function render(card,force=false){
  if(!state)return;
  if(force||!card.dirty){const v=state.controls[card.key];card.check.checked=v.enabled;card.groups.value=v.groups.join('\n');card.dirty=false;message(card,'开关与群范围已加载')}
  routes(card);card.button.disabled=false;window.BearGroupNames.update();
  if(!card.dirty){const sub=state.sub_enabled[card.key];message(card,!card.check.checked?'本功能已停用；下方配置保留':sub===false?'范围开关已开启；下方原功能开关仍为关闭':(['forwarding','text','collection','mango','hunt','sightings'].includes(card.key)&&!state.master_enabled)?'全局自动播报与转发已关闭；本页配置保留':'本功能范围已启用；具体路由与子功能按下方配置执行')}
 }
 async function refresh(){
  if(!pending)pending=(async()=>{await window.AstrBotPluginView.ready();state=await api('feature-status');for(const c of cards.values())render(c);renderAdvanced();return state})().finally(()=>pending=null);
  return pending;
 }
 function mount(key,container){
  if(cards.has(key)||!container)return;
  const section=el('section',undefined,'feature-scope');section.dataset.feature=key;section.setAttribute('aria-label',defs[key][0]+'开关与群设置');
  const sheet=el('style',style);section.append(sheet);
  const head=el('div',undefined,'fs-head');head.append(el('h2',defs[key][0]+' · 开关与群'));
  const checkLabel=el('label',undefined,'fs-check'),check=el('input');check.type='checkbox';check.setAttribute('aria-label','启用'+defs[key][0]);checkLabel.append(check,el('span','启用本功能'));head.append(checkLabel);section.append(head,el('p',defs[key][2]));
  const grid=el('div',undefined,'fs-grid'),field=el('label',undefined,'fs-field'),groups=el('textarea');groups.rows=3;groups.spellcheck=false;groups.placeholder='留空沿用现有范围；每行一个群号';groups.setAttribute('aria-label',defs[key][0]+' · '+defs[key][1]);field.append(el('span',defs[key][1]+' · 每行一个群号'),groups);const routing=el('div',undefined,'fs-routes');grid.append(field,routing);section.append(grid);
  const actions=el('div',undefined,'fs-actions'),button=el('button','保存本功能开关与群','primary'),status=el('span','正在读取…','fs-state');button.type='button';button.disabled=true;status.setAttribute('role','status');actions.append(button,status);section.append(actions);
  container.prepend(section);
  window.BearGroupNames.bind(groups,{platform:()=>key==='manual'?(container.getRootNode().querySelector('#sendPlatform')?.value||window.BearGroupNames.selected()):window.BearGroupNames.selected()});
  const card={key,section,check,groups,button,status,routes:routing,dirty:false,revision:0};cards.set(key,card);
  const changed=()=>{card.dirty=true;card.revision++;message(card,'有未保存的开关或群范围修改')};check.onchange=changed;groups.oninput=changed;
  button.onclick=async()=>{button.disabled=true;const revision=card.revision;try{
   const value={enabled:check.checked,groups:groups.value.split('\n').map(g=>g.trim()).filter(Boolean)};
   state=await api('feature-settings',{feature:key,control:value});
   window.dispatchEvent(new CustomEvent('bear-feature-control-saved',{detail:{feature:key,control:value}}));
   if(card.revision===revision){render(card,true);message(card,'已保存并生效；下方其他配置请分别保存')}else message(card,'本次已保存，还有后续修改未保存');
   for(const c of cards.values())if(c!==card)render(c);
  }catch(error){message(card,error.message,true)}finally{button.disabled=false}};
  refresh().catch(error=>message(card,error.message,true));
 }
 function advanced(container){
  if(!container||container.querySelector('.feature-scope'))return;
  const section=el('section',undefined,'feature-scope');section.append(el('style',style),el('h2','高级配置 · 调试编辑'),el('p','高级配置用于编辑各功能的完整参数，群范围与启用状态沿用对应功能页，不建立额外的发送通道。默认隐藏，由查漏与采集里的调试模式控制。'));
  const label=el('label',undefined,'fs-advanced'),input=el('input');input.type='checkbox';input.setAttribute('aria-label','启用调试模式（关闭后隐藏本页）');input.checked=window.BearConsolePreferences.isDebugEnabled();input.onchange=()=>window.BearConsolePreferences.setDebug(input.checked);window.addEventListener('bear-console-debug-change',()=>{input.checked=window.BearConsolePreferences.isDebugEnabled()});label.append(input,el('span','启用调试模式（关闭后隐藏本页）'));section.append(label);
  advancedRoutes=el('div',undefined,'fs-routes');section.append(advancedRoutes);container.prepend(section);renderAdvanced();
 }
 function renderAdvanced(){if(!state||!advancedRoutes)return;advancedRoutes.replaceChildren(el('b','各功能的群范围（在对应功能页修改）'));for(const [key,def] of Object.entries(defs)){const v=state.controls[key],r=state.routes[key]||{};advancedRoutes.append(el('div',def[0]+' · '+(v.enabled?'开启':'关闭')+' · '+(window.BearGroupNames.list(v.groups.length?v.groups:(r.targets||r.sources||[]))||'沿用原配置')))}}
 window.BearFeatureControls={mount,advanced,refresh};
 mount('sightings',document.querySelector('#settings'));
 const trainHolder=el('div');document.querySelector('#trainPanel>header').after(trainHolder);mount('trains',trainHolder);
 window.addEventListener('hashchange',()=>refresh().catch(()=>{}));
 window.addEventListener('bear-magic-status',()=>{for(const card of cards.values())if(state)routes(card)});
 window.addEventListener('bear-group-names-updated',()=>{if(state){for(const card of cards.values())routes(card);renderAdvanced()}});
 document.getElementById('platform').addEventListener('change',()=>{if(state){for(const card of cards.values())routes(card);renderAdvanced()}});
})();

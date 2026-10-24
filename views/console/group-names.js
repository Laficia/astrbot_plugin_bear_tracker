/* Names are display metadata only. Never replace IDs in editable routing fields. */
(()=>{
 const cache=new Map(),pending=new Map(),fields=new Map();
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
 const selected=()=>document.getElementById('platform')?.value||'';
 const ids=value=>[...new Set(String(value||'').split(/[\n,，、]/).map(s=>s.trim()).filter(s=>s&&s!=='0'))];
 const style=`.group-name-display{margin:9px 0 0;font:12px/1.8 "Microsoft YaHei",system-ui,sans-serif;overflow-wrap:anywhere;color:var(--text,var(--ink));}.group-name-display .gn-list{display:flex;flex-wrap:wrap;gap:6px}.group-name-display .gn-item{padding:4px 8px;border:1px solid var(--border,var(--line));border-radius:7px;background:var(--panel-soft,var(--soft));}.group-name-display .gn-tools{display:flex;align-items:center;gap:10px;margin-top:7px}.group-name-display .gn-state{font-size:11px;color:var(--muted)}.group-name-display button{padding:4px 8px;min-height:0;font-size:11px;cursor:pointer}.group-name-display details{margin:9px 0 0;border:1px solid var(--border,var(--line));border-radius:9px;padding:8px 10px}.group-name-display summary{color:var(--accent);cursor:pointer;font-size:12px}.group-name-display .gn-search{margin:10px 0 7px;width:100%;font:inherit;box-sizing:border-box;padding:8px 10px;border:1px solid var(--border,var(--line));border-radius:7px;background:var(--panel);color:inherit}.group-name-display .gn-choices{display:grid;grid-template-columns:1fr;gap:5px;max-height:220px;overflow:auto;margin-top:8px}.group-name-display .gn-option{display:flex;gap:8px;align-items:center;text-align:left;white-space:normal;width:100%;color:inherit;border:1px solid var(--border,var(--line));border-radius:7px;background:var(--panel);line-height:1.65}.group-name-display .gn-option[aria-checked=true]{border-color:var(--accent);background:var(--accent-soft,var(--tint));}.group-name-display .gn-mark{width:16px;height:16px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid currentColor;border-radius:4px;color:var(--accent);font-size:12px}.group-name-display .gn-help{margin:6px 0;font-size:11px;color:var(--muted)}.group-name-display[hidden]{display:none!important}`;
 function sheet(root){if(root.querySelector('[data-group-name-style]'))return;const s=el('style',style);s.dataset.groupNameStyle='';(root.head||root).append(s)}
 function label(id,pid=selected()){
  const gid=String(id),name=cache.get(pid)?.groups?.find(g=>g.id===gid)?.name;
  return name?name+'（'+gid+'）':gid+'（群名未获取）';
 }
 function list(values,pid=selected()){return [...new Set((values||[]).filter(v=>v&&String(v)!=='0').map(String))].map(id=>label(id,pid)).join('、')}
 function paint(input,field){
  const values=ids(input.value),pid=field.platform(),data=cache.get(pid);
  field.wrap.hidden=false;
  field.list.replaceChildren(...values.map(id=>{const item=el('span');item.className='gn-item';const remove=el('button','×');remove.type='button';remove.setAttribute('aria-label','移除'+label(id,pid));remove.onclick=()=>write(input,ids(input.value).filter(v=>v!==id));item.append(el('span',label(id,pid)),remove);return item}));
  field.button.disabled=pending.has(pid)||!pid;
  field.state.textContent=!pid?'请先选择机器人':pending.has(pid)?'正在读取群名称…':data?.message|| (data?.synced_at?'群名称已同步'+(data.stale?' · 缓存':''):'群名称尚未获取');
  field.summary.textContent='搜索并选择群聊 · 已选 '+values.length+(field.multiple?' 个群':' 个');
  choices(input,field);
 }
 function choices(input,field){
  const pid=field.platform(),groups=cache.get(pid)?.groups||[],query=field.search.value.trim().toLocaleLowerCase(),values=ids(input.value);
  const visible=groups.filter(g=>(g.name+' '+g.id).toLocaleLowerCase().includes(query));
  field.visible=visible;field.choices.replaceChildren();
  field.selectAll.disabled=!visible.length;field.removeAll.disabled=!visible.some(g=>values.includes(g.id));
  field.count.textContent=groups.length?'找到 '+visible.length+' / '+groups.length+' 个群；勾选仅修改草稿，保存对应配置后生效。':'暂时没有可选择的群，可继续手填群号，或连接机器人后刷新。';
  for(const group of visible){
   const button=el('button');button.type='button';button.className='gn-option';button.dataset.groupId=group.id;button.setAttribute('role','checkbox');button.setAttribute('aria-label',group.name+'（'+group.id+'）');button.setAttribute('aria-checked',String(values.includes(group.id)));
   const mark=el('span',values.includes(group.id)?'✓':'');mark.className='gn-mark';mark.setAttribute('aria-hidden','true');button.append(mark,el('span',group.name+'（'+group.id+'）'));
   button.onclick=event=>{event.preventDefault();const old=ids(input.value),on=old.includes(group.id);const next=field.multiple?(on?old.filter(id=>id!==group.id):[...old,group.id]):on?[]:[group.id];
    if(next.length>100){field.state.textContent='最多选择 100 个群';return}write(input,next);[...field.choices.querySelectorAll('[data-group-id]')].find(b=>b.dataset.groupId===group.id)?.focus()};
   field.choices.append(button);
  }
 }
 function write(input,values){input.value=values.join('\n');input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))}
 function repaint(){for(const [input,field] of fields){if(input.isConnected){field.connected=true;sheet(input.getRootNode());paint(input,field)}else if(field.connected)fields.delete(input)}}
 async function load(pid=selected(),force=false){
  if(!pid)return;
  if(pending.has(pid))return pending.get(pid);
  const old=cache.get(pid);
  if(!force&&old&&Date.now()-(old.checked||0)<(old.stale?30000:300000))return old;
  const task=(async()=>{
   try{await window.AstrBotPluginView.ready();const data=await window.AstrBotPluginView.apiPost('webui/group-names',{platform_id:pid,refresh:force});cache.set(pid,{...data,checked:Date.now()});return data}
   catch(error){cache.set(pid,{...(old||{groups:[]}),stale:true,message:error.message||'群名称读取失败',checked:Date.now()})}
   finally{pending.delete(pid);repaint();window.dispatchEvent(new CustomEvent('bear-group-names-updated',{detail:{platform_id:pid}}))}
  })();pending.set(pid,task);repaint();return task;
 }
 function bind(input,options={}){
  if(!input||fields.has(input))return;
  const root=input.getRootNode();if(root.nodeType===9||root.host)sheet(root);
  const wrap=el('div');wrap.className='group-name-display';wrap.setAttribute('aria-label','群名称与群号');
  const names=el('div');names.className='gn-list';const tools=el('div');tools.className='gn-tools';const state=el('span');state.className='gn-state';
  const button=el('button','刷新群名称');button.type='button';tools.append(state,button);
  const picker=el('details'),summary=el('summary'),search=el('input');search.type='search';search.className='gn-search';search.placeholder='输入群名或群号搜索';search.setAttribute('aria-label','搜索群名或群号 · '+(input.getAttribute('aria-label')||input.labels?.[0]?.textContent?.trim()||input.id||'群选择'));
  const count=el('p');count.className='gn-help';const results=el('div');results.className='gn-choices';const help=el('p','也可直接在上方手填群号；已填写但不在当前群列表中的群号会保留。');help.className='gn-help';
  const bulk=el('div');bulk.className='gn-tools';const selectAll=el('button','全选搜索结果'),removeAll=el('button','取消搜索结果');selectAll.type=removeAll.type='button';bulk.append(selectAll,removeAll);bulk.hidden=!(options.multiple??input.tagName==='TEXTAREA');
  picker.append(summary,search,bulk,count,results,help);wrap.append(names,picker,tools);input.after(wrap);
  const field={selectAll,removeAll,wrap,list:names,state,button,summary,search,count,choices:results,multiple:options.multiple??input.tagName==='TEXTAREA',platform:options.platform||selected};fields.set(input,field);
  selectAll.onclick=()=>{const next=[...new Set([...ids(input.value),...field.visible.map(g=>g.id)])];if(next.length>100){state.textContent='最多选择 100 个群，请缩小搜索范围';return}write(input,next)};
  removeAll.onclick=()=>{const visible=new Set(field.visible.map(g=>g.id));write(input,ids(input.value).filter(id=>!visible.has(id)))};
  search.addEventListener('input',event=>{event.stopPropagation();choices(input,field)});picker.addEventListener('toggle',()=>{if(picker.open){load(field.platform());choices(input,field)}});
  button.onclick=event=>{event.preventDefault();load(field.platform(),true)};
  input.addEventListener('input',()=>{paint(input,field);load(field.platform())});paint(input,field);load(field.platform());queueMicrotask(repaint);
 }
 function update(){repaint();const platforms=new Set([...fields.values()].map(f=>f.platform()).filter(Boolean));for(const pid of platforms)load(pid)}
 window.BearGroupNames={bind,update,load,label,list,selected};
 window.addEventListener('bear-feature-control-saved',update);
 window.addEventListener('hashchange',update);
 document.getElementById('platform')?.addEventListener('change',()=>queueMicrotask(update));
 bind(document.getElementById('groups'));
})();

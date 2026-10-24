/* Train schedules and editable command replies, within the shared console. */
(()=>{
 let initialized=false,config,state,revision=0;
 const root=document.getElementById('trainPanel');
 const $=id=>root.querySelector('#'+id);
 const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
 const api=(path,body)=>body===undefined?window.AstrBotPluginView.apiGet('webui/train/'+path):window.AstrBotPluginView.apiPost('webui/train/'+path,body);
 const status=(text,error=false)=>{$('trainStatus').textContent=text;$('trainStatus').classList.toggle('train-error',error)};
 const changed=()=>{revision++;status('有未保存的车表指令修改')};
 function options(select,selected,all=true){
  if(all){const o=node('option','全部大区');o.value='all';select.append(o)}
  Object.entries(state.centers).forEach(([key,name])=>{const o=node('option',name);o.value=key;select.append(o)});select.value=selected;
 }
 function field(host,label,value,kind,update){
  const wrap=node('label',undefined,'train-field');wrap.append(node('span',label));
  const input=node(kind==='text'?'textarea':'input');
  if(kind==='number')input.type='number';
  if(kind==='bool'){input.type='checkbox';input.checked=value}else input.value=value;
  if(kind==='text'){input.rows=3;input.spellcheck=false}
  input.setAttribute('aria-label',label);
  input.addEventListener('input',()=>{update(kind==='bool'?input.checked:kind==='number'?Number(input.value):input.value);changed()});
  wrap.append(input);host.append(wrap);return input;
 }
 function variables(host,names,input,update){
  const buttons=node('div',undefined,'variable-list');
  names.forEach(name=>{const b=node('button','{'+name+'}');b.type='button';b.onclick=()=>{input.setRangeText(b.textContent,input.selectionStart,input.selectionEnd,'end');update(input.value);input.focus();changed()};buttons.append(b)});host.append(buttons);
 }
 function renderRules(){
  const list=$('trainRules');list.replaceChildren();
  if(!config.rules.length)list.append(node('p','尚未设置指令，点击“新增指令规则”。','note'));
  config.rules.forEach((rule,index)=>{
   const card=node('section',undefined,'card train-rule');
   const head=node('div',undefined,'train-rule-head');head.append(node('h2','指令规则 '+(index+1)));
   const remove=node('button','删除规则','remove');remove.type='button';remove.onclick=()=>{config.rules=config.rules.filter(r=>r.id!==rule.id);renderRules();changed()};head.append(remove);card.append(head);
   field(card,'启用这条指令规则',rule.enabled,'bool',v=>rule.enabled=v);
   const two=node('div',undefined,'two');
   field(two,'规则名称',rule.name,'input',v=>rule.name=v);
   field(two,'触发指令 · 每行一个',rule.commands.join('\n'),'text',v=>rule.commands=v.split('\n').map(x=>x.trim()).filter(Boolean));card.append(two);
   const modeLabel=node('label',undefined,'train-field');modeLabel.append(node('span','回复方式'));
   const mode=node('select');mode.setAttribute('aria-label','回复方式');
   [['schedule','动态车表'],['text','固定文字']].forEach(([key,name])=>{const o=node('option',name);o.value=key;mode.append(o)});mode.value=rule.mode;modeLabel.append(mode);card.append(modeLabel);
   const settings=node('div',undefined,'train-schedule-options');
   const centerLabel=node('label',undefined,'train-field');centerLabel.append(node('span','不填大区时默认查询'));
   const center=node('select');center.setAttribute('aria-label','不填大区时默认查询');options(center,rule.default_center);center.onchange=()=>{rule.default_center=center.value;changed()};centerLabel.append(center);settings.append(centerLabel);
   const numbers=node('div',undefined,'two');const hours=field(numbers,'未来多少小时',rule.hours,'number',v=>rule.hours=v);hours.min=1;hours.max=24;
   const limit=field(numbers,'最多显示前几班',rule.limit,'number',v=>rule.limit=v);limit.min=1;limit.max=100;settings.append(numbers);card.append(settings);
   const reply=field(card,'回复文字模板',rule.reply_template,'text',v=>rule.reply_template=v);reply.maxLength=2000;
   variables(card,state.summary_variables,reply,v=>rule.reply_template=v);
   const detail=node('div');const item=field(detail,'每班车的文字模板',rule.item_template,'text',v=>rule.item_template=v);item.maxLength=2000;
   variables(detail,state.item_variables,item,v=>rule.item_template=v);card.append(detail);
   const help=node('p',undefined,'note');card.append(help);
   const showMode=()=>{settings.hidden=detail.hidden=rule.mode==='text';help.textContent=rule.mode==='text'?'收到指令后回复上方文字，可直接输入固定内容。':'单个大区按时间排序；全部大区按时间混排，不分区；暂停班次跳过，已过时刻排到明天。{车表} 插入全部班次；显示条数先按全局时间截取。'};
   mode.onchange=()=>{rule.mode=mode.value;showMode();changed()};showMode();
   const preview=node('button','用这条规则预览');preview.type='button';preview.onclick=()=>{$('trainTestCommand').value='/'+(rule.commands[0]||'车表');previewCommand()};card.append(preview);
   list.append(card);
  });
 }
 async function loadTable(force=false){
  $('trainRefresh').disabled=true;$('trainTableStatus').textContent='正在读取网站车表…';
  try{
   const result=await api('table',{center:$('trainCenter').value,force});
   $('trainRows').replaceChildren();
   result.rows.forEach(row=>{const tr=node('tr');['#'+row['编号'],row['日期']+' '+row['时间'],row['大区'],row['服务器'],row['版本显示']||row['版本']+'A',row['班次'],row['车头'],[row['始发站'],row['水晶'],row['班次备注']].filter(Boolean).join(' · ')].forEach(value=>tr.append(node('td',value)));$('trainRows').append(tr)});
   if(!result.rows.length){const tr=node('tr'),cell=node('td','未来 24 小时暂无排班');cell.colSpan=8;tr.append(cell);$('trainRows').append(tr)}
   $('trainTableStatus').textContent='未来 24 小时共 '+result.rows.length+' 班 · 数据读取于 '+new Date(result.fetched_at*1000).toLocaleString('zh-CN',{hour12:false});
  }catch(error){$('trainTableStatus').textContent=error.message}finally{$('trainRefresh').disabled=false}
 }
 async function previewCommand(){
  $('trainTest').disabled=true;
  try{const result=await api('query',{command:$('trainTestCommand').value,config:structuredClone(config)});$('trainReply').textContent=result.text;$('trainPreviewStatus').textContent='命中“'+result.matched+'” · '+result.parts.length+' 条纯文字消息 · 仅预览，未发送';}
  catch(error){$('trainReply').textContent=error.message;$('trainPreviewStatus').textContent='请检查指令与模板'}finally{$('trainTest').disabled=false}
 }
 async function initialize(){
  try{
   await window.AstrBotPluginView.ready();state=await api('state');config=structuredClone(state.config);
   window.addEventListener('bear-feature-control-saved',event=>{if(event.detail.feature==='trains')config.enabled=event.detail.control.enabled});
   options($('trainCenter'),'bean');$('trainCenter').onchange=()=>loadTable();$('trainRefresh').onclick=()=>loadTable(true);
   $('trainTest').onclick=previewCommand;
   $('trainAddRule').onclick=()=>{const r=structuredClone(state.default_rule);r.id=crypto.randomUUID();r.name='新指令规则';let n=2;const existing=new Set(config.rules.flatMap(x=>x.commands));while(existing.has('车表'+n))n++;r.commands=['车表'+n];config.rules.push(r);renderRules();changed()};
   $('trainSave').onclick=async()=>{const button=$('trainSave'),savedRevision=revision;button.disabled=true;try{await api('settings',{config:structuredClone(config)});status(savedRevision===revision?'车表指令已保存并立即生效':'本次已保存；还有后续修改未保存')}catch(error){status(error.message,true)}finally{button.disabled=false}};
   renderRules();$('trainSave').disabled=false;$('trainAddRule').disabled=false;$('trainTest').disabled=false;status('车表指令配置已加载');loadTable();
  }catch(error){status(error.message,true)}
 }
 window.BearTrainPanel={open(){window.BearTrainHuntPanel.open();if(!initialized){initialized=true;initialize()}}};
})();

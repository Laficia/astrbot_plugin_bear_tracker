/* Train reminders remain text; images are only returned to explicit commands. */
(()=>{
 let initialized=false,config,state,revision=0;
 const root=document.getElementById('trainPanel');
 const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e};
 const api=(path,body)=>body===undefined?window.AstrBotPluginView.apiGet('webui/train/'+path):window.AstrBotPluginView.apiPost('webui/train/'+path,body);
 const lines=text=>text.split('\n').map(v=>v.trim()).filter(Boolean);
 let status,save,output;
 const changed=()=>{revision++;status.textContent='有未保存的狩猎车提醒或图片指令修改'};
 function field(host,label,value,type,update,hint=''){
  const wrap=el('label',undefined,'train-field');wrap.append(el('span',label));
  const input=el(type==='list'||type==='template'?'textarea':'input');input.setAttribute('aria-label',label);
  if(type==='bool'){input.type='checkbox';input.checked=value}else if(type==='number'){input.type='number';input.min=1;input.max=120;input.value=value}else{input.value=type==='list'?value.join('\n'):value;if(type==='template'){input.rows=8;input.maxLength=2000}}
  input.oninput=()=>{update(type==='bool'?input.checked:type==='list'?lines(input.value):type==='number'?Number(input.value):input.value);changed()};
  wrap.append(input);if(hint)wrap.append(el('p',hint,'note'));host.append(wrap);return input;
 }
 function centerSelect(host,label,selected){
  const wrap=el('label',undefined,'train-field');wrap.append(el('span',label));const select=el('select');select.setAttribute('aria-label',label);
  for(const [id,name] of Object.entries({all:'全部大区',...state.centers})){const o=el('option',name);o.value=id;select.append(o)}select.value=selected;wrap.append(select);host.append(wrap);return select;
 }
 async function preview(kind,argument){
  output.replaceChildren(el('p','正在生成预览…','note'));
  try{const result=await api('hunt-preview',{kind,argument,config:structuredClone(config)});output.replaceChildren();if(result.image){const image=el('img');image.src=result.image;image.alt=kind==='recent'?'近期发车卡片':'预计线路卡片';image.style.cssText='max-width:100%;height:auto;display:block;border-radius:12px';output.append(image)}else output.append(el('pre',result.text,'template-preview'));output.append(el('p','仅预览，未发送到群。','note'))}
  catch(error){output.replaceChildren(el('p',error.message,'train-error'))}
 }
 async function initialize(){
  const holder=el('div');root.querySelector('#trainAddRule').closest('section').before(holder);
  try{
   await window.AstrBotPluginView.ready();state=await api('state');config=structuredClone(state.hunt_config);
   const reminders=el('section',undefined,'card train-card');reminders.append(el('h2','狩猎车 · 发车前文字提醒'),el('p','默认提前 10 分钟只发送文字。统一静默时段和本页功能开关同样生效；不主动发送图片。','note'));
   field(reminders,'启用发车前文字提醒',config.enabled,'bool',v=>config.enabled=v);
   const grid=el('div',undefined,'two');field(grid,'狩猎车提醒群 · 每行一个群号',config.groups,'list',v=>config.groups=v,'独立设置提醒接收群；留空不自动发送。发送机器人沿用接收与监听中的选择。');field(grid,'仅提醒这些服务器 · 每行一个名称',config.servers,'list',v=>config.servers=v,'留空表示所选大区的全部服务器。');reminders.append(grid);
   const areas=el('div',undefined,'chips');for(const [id,name] of Object.entries(state.centers)){const label=el('label',undefined,'chip'),input=el('input');input.type='checkbox';input.checked=config.centers.includes(id);input.style.width='16px';input.setAttribute('aria-label','提醒大区 '+name);input.onchange=()=>{config.centers=input.checked?[...config.centers,id]:config.centers.filter(c=>c!==id);changed()};label.append(input,el('span',name));areas.append(label)}reminders.append(el('p','提醒大区','note'),areas);
   field(reminders,'提前多少分钟提醒',config.minutes,'number',v=>config.minutes=v);
   const template=field(reminders,'狩猎车文字提醒模板',config.template,'template',v=>config.template=v,'车头只读取来源，来源缺失时显示“待公布”。');
   const variables=el('div',undefined,'variable-list');for(const name of state.hunt_variables){const button=el('button','{'+name+'}');button.type='button';button.onclick=()=>{template.setRangeText(button.textContent,template.selectionStart,template.selectionEnd,'end');config.template=template.value;template.focus();changed()};variables.append(button)}reminders.append(variables);
   const actions=el('div',undefined,'train-test-row');const textPreview=el('button','预览文字提醒'),reset=el('button','恢复提醒模板');textPreview.type=reset.type='button';textPreview.onclick=()=>preview('text','');reset.onclick=()=>{template.value=state.hunt_default_template;config.template=template.value;changed()};actions.append(textPreview,reset);reminders.append(actions);holder.append(reminders);
   const media=el('section',undefined,'card train-card');media.append(el('h2','狩猎车 · 图片指令'),el('p','收到指令后发图片到发出指令的会话。近期发车按时间排序显示最近四班；预计线路按地图和分线排列。无共享标记时显示“未上报 / 待确认”，不填候选坐标。','note'));
   const commandGrid=el('div',undefined,'two');field(commandGrid,'近期发车图指令 · 每行一个',config.image_commands,'list',v=>config.image_commands=v,'例如 /车图 猪、/近期发车 全部。');field(commandGrid,'预计线路图指令 · 每行一个',config.route_commands,'list',v=>config.route_commands=v,'例如 /车线 8；同编号多班次时可追加时间：/车线 8 22:40。');media.append(commandGrid);
   const defaultCenter=centerSelect(media,'图片指令默认大区',config.default_center);defaultCenter.onchange=()=>{config.default_center=defaultCenter.value;changed()};
   const selectCenter=centerSelect(media,'预览近期发车图的大区',config.default_center),recentButton=el('button','预览近期发车图');recentButton.type='button';recentButton.onclick=()=>preview('recent',selectCenter.value);media.append(recentButton);
   const routeWrap=el('div',undefined,'train-test-row'),routeInput=el('input');routeInput.placeholder='填写上方车表中的班次编号';routeInput.setAttribute('aria-label','预览线路的班次编号');const routeButton=el('button','预览预计线路图');routeButton.type='button';routeButton.onclick=()=>preview('route',routeInput.value);routeWrap.append(routeInput,routeButton);media.append(el('label','预计线路预览 · 班次编号'),routeWrap);holder.append(media);
   output=el('section',undefined,'card train-card');output.setAttribute('aria-label','狩猎车提醒与图片预览');output.append(el('p','点击上方预览按钮检查文字或图片，预览不会发群消息。','note'));holder.append(output);
   const footer=el('div',undefined,'actions');save=el('button','保存狩猎车提醒与图片指令','primary');save.type='button';status=el('span',undefined,'note');status.setAttribute('role','status');status.textContent=config.groups.length?'提醒配置已加载':'尚未设置提醒群，不会自动发送；图片指令可使用。';footer.append(save,status);holder.append(footer);
   if(state.hunt_last_error)reminders.append(el('p',state.hunt_last_error,'train-error'));
   save.onclick=async()=>{save.disabled=true;const current=revision;try{await api('hunt-settings',{config:structuredClone(config)});status.textContent=current===revision?'狩猎车提醒与图片指令已保存并生效':'本次已保存，还有后续修改未保存'}catch(error){status.textContent=error.message}finally{save.disabled=false}};
  }catch(error){holder.append(el('p',error.message,'train-error'))}
 }
 window.BearTrainHuntPanel={open(){if(!initialized){initialized=true;initialize()}}};
})();

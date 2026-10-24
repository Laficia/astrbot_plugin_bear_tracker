/* Personal console navigation preferences; no broadcast settings are changed. */
(()=>{
 const nav=document.querySelector('.nav');
 const orderKey='bear-tracker-sidebar-order',debugKey='bear-tracker-debug-mode';
 const links=[...nav.querySelectorAll('a')];
 const byId=new Map(links.map(link=>[link.hash.slice(1),link]));
 const debugLinks=['magic-advanced','magic-text'].map(id=>byId.get(id));
 const read=key=>{try{return localStorage.getItem(key)}catch{return null}};
 const write=(key,value)=>{try{localStorage.setItem(key,value)}catch{}};
 let debug=read(debugKey)==='true';
 let order;
 try{order=JSON.parse(read(orderKey))}catch{}
 if(Array.isArray(order)){
  const restored=new Set();
  for(const id of order)if(byId.has(id)&&!restored.has(id)){nav.append(byId.get(id));restored.add(id)}
  for(const [id,link] of byId)if(!restored.has(id))nav.append(link);
 }
 const saveOrder=()=>write(orderKey,JSON.stringify([...nav.querySelectorAll('a')].map(link=>link.hash.slice(1))));
 document.getElementById('resetNavOrder').onclick=()=>{links.forEach(link=>nav.append(link));saveOrder()};
 const syncDebugLinks=()=>debugLinks.forEach(link=>{link.hidden=!debug});
 syncDebugLinks();
 window.BearConsolePreferences={
  isDebugEnabled:()=>debug,
  setDebug(enabled){
   debug=enabled===true;syncDebugLinks();write(debugKey,String(debug));
   window.dispatchEvent(new CustomEvent('bear-console-debug-change',{detail:{enabled:debug}}));
  }
 };
 for(const link of links){
  link.title='按住拖动调整顺序；也可用 Alt + 方向键移动';
  const grip=document.createElement('span');grip.className='nav-drag-handle';
  grip.textContent='⠿';grip.setAttribute('aria-hidden','true');link.append(grip);
  link.addEventListener('keydown',event=>{
   if(!event.altKey||!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key))return;
   event.preventDefault();
   const visible=[...nav.querySelectorAll('a')].filter(a=>!a.hidden),index=visible.indexOf(link);
   const backwards=event.key==='ArrowUp'||event.key==='ArrowLeft';
   const neighbor=visible[index+(backwards?-1:1)];
   if(neighbor){backwards?neighbor.before(link):neighbor.after(link);saveOrder();link.focus()}
  });
 }
 // Keep normal label clicks and touch scrolling. A handle also supports touch
 // dragging; desktop users can grab anywhere on a row.
 let suppressClick=false;
 nav.addEventListener('dragstart',event=>event.preventDefault());
 nav.addEventListener('click',event=>{if(suppressClick){event.preventDefault();event.stopImmediatePropagation()}},true);
 nav.addEventListener('pointerdown',event=>{
  const link=event.target.closest('a');
  if(!link||link.hidden||event.button!==0||!event.isPrimary)return;
  if(event.pointerType==='touch'&&!event.target.closest('.nav-drag-handle'))return;
  const id=event.pointerId,startX=event.clientX,startY=event.clientY;
  const original=[...nav.children];let dragging=false;
  if(event.target.closest('.nav-drag-handle'))event.preventDefault();
  function move(e){
   if(e.pointerId!==id)return;
   if(!dragging&&Math.hypot(e.clientX-startX,e.clientY-startY)<6)return;
   if(!dragging){dragging=true;nav.setPointerCapture(id)}
   suppressClick=true;link.classList.add('nav-dragging');
   document.documentElement.classList.add('nav-sorting');e.preventDefault();
   const box=nav.getBoundingClientRect(),horizontal=getComputedStyle(nav).display==='flex';
   if(horizontal){if(e.clientX<box.left+24)nav.scrollLeft-=16;else if(e.clientX>box.right-24)nav.scrollLeft+=16}
   else{if(e.clientY<box.top+24)nav.scrollTop-=16;else if(e.clientY>box.bottom-24)nav.scrollTop+=16}
   const over=document.elementFromPoint(e.clientX,e.clientY)?.closest('.nav a');
   if(!over||over===link||over.hidden)return;
   const rect=over.getBoundingClientRect();
   const before=horizontal?e.clientX<rect.left+rect.width/2:e.clientY<rect.top+rect.height/2;
   before?over.before(link):over.after(link);
  }
  function finish(e){
   if(e.pointerId!==id)return;
   if(dragging){if(e.type==='pointercancel')original.forEach(node=>nav.append(node));else saveOrder()}
   link.classList.remove('nav-dragging');document.documentElement.classList.remove('nav-sorting');
   window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',finish);window.removeEventListener('pointercancel',finish);
   if(nav.hasPointerCapture(id))nav.releasePointerCapture(id);
   setTimeout(()=>{suppressClick=false},0);
  }
  window.addEventListener('pointermove',move,{passive:false});window.addEventListener('pointerup',finish);window.addEventListener('pointercancel',finish);
 });
})();

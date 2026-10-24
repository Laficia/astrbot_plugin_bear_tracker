"""Deterministic Pillow cards; positions come exclusively from shared lit points."""
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
from functools import lru_cache
from PIL import Image,ImageDraw,ImageFont

CATALOG=json.loads(Path(__file__).with_name('train_route_catalog.json').read_text('utf-8'))['maps']
BG='#0b1016';PANEL='#161d25';LINE='#303943';WHITE='#eef3fa';MUTED='#99a5b3';GOLD='#dca01a';BLUE='#59b2ff';GREEN='#63df91'


def route_sections(row,snapshot):
    shared=snapshot.get('shared',{});points=shared.get('points',{});lines=shared.get('lines',{})
    maps=[m for m in CATALOG if m['version']==row['版本']]
    route=row.get('路线','');explicit=row.get('route_map_ids',[])
    order=[mid for mid in explicit if any(m['id']==mid for m in maps)]
    if not order:order=[m['id'] for m in sorted(maps,key=lambda m:route.find(m['name']) if m['name'] in route else 10000) if m['name'] in route]
    if not order:
        order=list(dict.fromkeys(r.get('mapId') for r in shared.get('route',[]) if r.get('server')==row['服务器'] and any(m['id']==r.get('mapId') for m in maps)))
        if order:order.extend(m['id'] for m in maps if m['id'] not in order)
    if not order:
        preferred={'4.0':[4,5,6,1,2,3],'6.0':[2,1,3,4,5,6]}.get(row['版本'],[1,2,3,4,5,6])
        maps.sort(key=lambda m:preferred.index(int(m['id'].split('-')[-1])))
        start=next((m for m in maps if m['name'] in row['始发站']),None)
        if start:maps.remove(start);maps.insert(0,start)
        order=[m['id'] for m in maps]
    # A stated route may omit maps intentionally; do not silently add them.
    sections=[]
    for mid in order:
        m=next(m for m in maps if m['id']==mid)
        key=lambda line:json.dumps([row['服务器'],mid,line],ensure_ascii=False,separators=(',',':'))
        count=lines.get(key(0),1)
        if type(count) is not int or not 1<=count<=8:count=1
        for line in range(1,count+1):
            rows=points.get(key(line),[]);rows=rows if isinstance(rows,list) else []
            monsters=[]
            for monster in m['monsters']:
                candidates=[p for p in rows if isinstance(p,dict) and p.get('monsterId')==monster['id'] and p.get('lit') is True
                            and all(type(p.get(a)) in (int,float) and math.isfinite(p[a]) and 1<=p[a]<=41.96 for a in ('x','y'))]
                chosen=max(candidates,key=lambda p:str(p.get('updatedAt',''))) if candidates else None
                monsters.append({'name':monster['name'],'position':(chosen['x'],chosen['y']) if chosen else None,
                                 'status':'已标记' if chosen else '待确认','updated_at':chosen.get('updatedAt','') if chosen else ''})
            sections.append({'name':m['name']+(f' · {line}线' if count>1 else ''),'monsters':monsters})
    return sections


@lru_cache(maxsize=64)
def font(size,bold=False):
    candidates=([Path('C:/Windows/Fonts/msyhbd.ttc')] if bold else [])+[Path('C:/Windows/Fonts/msyh.ttc'),Path('C:/Windows/Fonts/simhei.ttf'),Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
    for path in candidates:
        if path.is_file():return ImageFont.truetype(str(path),size)
    return ImageFont.truetype('DejaVuSans.ttf',size)


def fit(draw,text,size,width,bold=False,minimum=18):
    while size>minimum and draw.textlength(text,font=font(size,bold))>width:size-=1
    f=font(size,bold)
    while text and draw.textlength(text,font=f)>width:text=text[:-2]+'…'
    return text,f


def write(draw,xy,text,size=28,color=WHITE,bold=False,width=None):
    text=str(text)
    if width:text,f=fit(draw,text,size,width,bold)
    else:f=font(size,bold)
    draw.text(xy,text,font=f,fill=color)


def save(image,directory,kind):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(image.tobytes()).hexdigest()[:20];path=directory/(kind+'-'+digest+'.png')
    if not path.exists():image.save(path)
    # Keep a bounded local preview cache. Recent in-flight images are untouched.
    for old in directory.glob('*.png'):
        if time.time()-old.stat().st_mtime>7*86400:old.unlink(missing_ok=True)
    return path


def render_recent(rows,directory,center,config):
    width=860;height=170+len(rows)*220+150
    image=Image.new('RGB',(width,height),BG);d=ImageDraw.Draw(image)
    d.rounded_rectangle((28,28,width-28,height-28),18,fill=PANEL,outline=LINE,width=2)
    write(d,(65,66),'近期发车',38,bold=True);write(d,(65,124),center+' · 未来24小时',20,MUTED)
    for index,row in enumerate(rows):
        y=170+index*220;d.rounded_rectangle((65,y,width-65,y+194),13,fill=BG,outline=LINE,width=2)
        write(d,(92,y+25),row['日期']+' '+row['时间'],43,GOLD,True,width=380)
        write(d,(480,y+27),'车头: '+row['车头'],26,BLUE,True,width=280)
        write(d,(92,y+94),row['服务器']+' - '+row['始发地'],27,width=width-180)
        x=92
        for label,color in [('#'+row['编号'],'#59a8ff'),('排班','#a879ef'),(row['版本显示'],GOLD),(row['班次'],'#8f9ca9')]:
            w=int(d.textlength(label,font=font(22,True)))+26
            d.rounded_rectangle((x,y+139,x+w,y+178),7,fill=color);write(d,(x+13,y+142),label,22,BG,True);x+=w+10
    y=170+len(rows)*220+10
    write(d,(65,y),'图片指令：/'+config['image_commands'][0]+' 大区',22,BLUE,width=width-130)
    write(d,(65,y+38),'预计线路：/'+config['route_commands'][0]+' 班次编号',22,BLUE,width=width-130)
    write(d,(65,y+76),'排班及临时调整以招募为准；来源未提供车头时显示待公布。',18,MUTED,width=width-130)
    return save(image,directory,'recent')


def render_route(row,sections,directory,error='',updated_at=''):
    width=1200;height=245+sum(90+len(s['monsters'])*62 for s in sections)+120
    if not sections:height+=120
    image=Image.new('RGB',(width,height),BG);d=ImageDraw.Draw(image)
    d.rounded_rectangle((28,28,width-28,height-28),16,fill=PANEL,outline=LINE,width=2)
    title=f"{row['服务器']} {row['版本显示']} {row['时间']}车次预计线路"
    write(d,(66,66),title,38,bold=True,width=width-132)
    d.rectangle((30,139,width-30,212),fill='#202833');write(d,(66,154),'始发站点【'+row['始发地']+'】',30,GOLD,True,width=width-132)
    y=245
    if not sections:write(d,(66,y),'该版本暂无地图资料；路线：'+row.get('路线','待公布'),24,MUTED,width=width-132);y+=120
    for section in sections:
        write(d,(66,y),section['name'],30,BLUE,True);y+=62
        for monster in section['monsters']:
            write(d,(70,y),'#',24,MUTED);write(d,(128,y),monster['name'],27,width=530)
            coords=',  '.join(f'{v:.1f}' for v in monster['position']) if monster['position'] else '未上报'
            write(d,(720,y),coords,27,color=WHITE if monster['position'] else MUTED,width=230)
            write(d,(1020,y),monster['status'],27,GREEN if monster['position'] else MUTED);y+=62
        d.line((30,y+10,width-30,y+10),fill=LINE,width=2);y+=28
    write(d,(66,y+12),error or '坐标来自共享点位的人工标记；已标记不代表怪物当前一定存活。',20,MUTED,width=width-132)
    write(d,(66,y+48),'读取时间：'+datetime.now().strftime('%Y-%m-%d %H:%M')+' · 无标记时不填候选坐标',18,MUTED,width=width-132)
    return save(image,directory,'route')

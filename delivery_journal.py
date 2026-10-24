"""Durable delivery outcomes and conservative cross-feature S-report dedup."""
import asyncio
import base64
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import unquote, urlparse

FEATURE_LABELS = {'sightings':'S怪出现', 'trigger':'可触发提醒', 'forwarding':'群转发',
                  'collection':'查漏补发', 'mango':'芒果广播', 'hunt':'狩猎提醒',
                  'trains':'狩猎车', 'manual':'手动群发', 'commands':'指令回复'}
SCOPE = ContextVar('bear_delivery_scope', default=None)


class DeliveryUncertain(RuntimeError):
    pass


class SafeDeliveryFailure(RuntimeError):
    pass


@contextmanager
def delivery_scope(feature, event_key=None, **values):
    old = SCOPE.get() or {}
    data = {**old, **values, 'feature': feature}
    if event_key is not None:
        data['event_key'] = str(event_key)
    data.setdefault('stats', {'sent':0, 'merged':0})
    token = SCOPE.set(data)
    try:
        yield data
    finally:
        SCOPE.reset(token)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class DeliveryJournal:
    def __init__(self, owner, folder, context):
        self.owner, self.context = owner, context
        self.folder = Path(folder)
        self.assets = self.folder / 'delivery_assets'
        self.assets.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.folder / 'deliveries.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.mutex = threading.RLock()
        self.locks = {}
        self.pruned_at=time.time()
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY,request_key TEXT,at REAL,updated REAL,platform TEXT,target TEXT,feature TEXT,state TEXT,detail TEXT,payload TEXT,metadata TEXT,semantic TEXT,has_image INTEGER,attempts INTEGER,message_id TEXT,parent TEXT)')
            self.db.execute('CREATE INDEX IF NOT EXISTS delivery_request ON deliveries(request_key,updated)')
            self.db.execute('CREATE INDEX IF NOT EXISTS delivery_semantic ON deliveries(platform,target,semantic,updated)')
            self.db.execute("UPDATE deliveries SET state='unknown',detail='发送过程中插件退出，结果待确认' WHERE state IN ('sending','retrying')")
        self.prune()

    def query(self, sql, args=()):
        with self.mutex:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.mutex, self.db:
            return self.db.execute(sql, args).rowcount

    def prune(self):
        self.execute('DELETE FROM deliveries WHERE updated < ? OR id NOT IN (SELECT id FROM deliveries ORDER BY updated DESC LIMIT 1000)', (time.time()-30*86400,))
        referenced = set()
        for row in self.query('SELECT payload FROM deliveries'):
            try:
                for s in json.loads(row['payload'] or '[]') or []:
                    file = s.get('data', {}).get('file', '')
                    if isinstance(file, str) and file.startswith(str(self.assets)):
                        referenced.add(file)
            except (ValueError, AttributeError):
                pass
        for path in self.assets.glob('*.image'):
            if str(path) not in referenced:
                path.unlink(missing_ok=True)

    def stash(self, segments):
        result = copy.deepcopy(segments)
        if not isinstance(result, list):
            return None
        total = 0
        for segment in result:
            if segment.get('type') not in ('text', 'image', 'at', 'reply', 'face'):
                return None
            if segment.get('type') != 'image':
                continue
            data = segment.setdefault('data', {})
            file = str(data.get('file') or data.get('url') or '')
            raw = None
            if file.startswith('base64://'):
                if len(file) > 18 * 1024 * 1024:
                    return None
                raw = base64.b64decode(file[9:], validate=True)
            elif file and not file.startswith(('http://', 'https://')):
                path = Path(unquote(urlparse(file).path).lstrip('/') if re.match(r'^file:///\w:', file) else unquote(urlparse(file).path) if file.startswith('file://') else file)
                if path.is_file() and path.stat().st_size <= 12*1024*1024:
                    raw = path.read_bytes()
            if raw is not None:
                total += len(raw)
                if total > 12*1024*1024:
                    return None
                target = self.assets / (hashlib.sha256(raw).hexdigest() + '.image')
                if not target.is_file():
                    if sum(p.stat().st_size for p in self.assets.glob('*.image')) + len(raw) > 256*1024*1024:
                        return None
                    target.write_bytes(raw)
                data['file'] = str(target)
                data.pop('url', None)
        if len(json.dumps(result, ensure_ascii=False)) > 128*1024:
            return None
        return result

    @staticmethod
    def chain_segments(chain):
        from astrbot.api.message_components import Plain, Image
        result = []
        for part in chain.chain:
            if isinstance(part, Plain):
                result.append({'type':'text','data':{'text':part.text}})
            elif isinstance(part, Image):
                result.append({'type':'image','data':{'file':part.file or part.url or part.path}})
            else:
                return None
        return result

    def semantic(self, text, report=None):
        if report and report.get('position') and report.get('hunt_key') and report.get('world'):
            return digest([report['hunt_key'], report['world'], report.get('instance') or 1,
                           [round(float(n),1) for n in report['position'][:2]]])
        if re.search(r'(?i)SS|ＳＳ', text):
            return ''
        pairs = re.findall(r'[（(]\s*(?:[Xx]\s*[:：]?\s*)?(\d{1,2}(?:\.\d+)?)\s*[,，、]\s*(?:[Yy]\s*[:：]?\s*)?(\d{1,2}(?:\.\d+)?)\s*[)）]', text)
        pairs += re.findall(r'[Xx]\s*[:：]\s*(\d{1,2}(?:\.\d+)?)\s*[,， ]*\s*[Yy]\s*[:：]\s*(\d{1,2}(?:\.\d+)?)', text)
        if len(set(pairs)) != 1:
            return ''
        pos = [round(float(n),1) for n in pairs[0]]
        if any(not 1 <= n <= 42 for n in pos):
            return ''
        from .listening_scope import center_worlds
        resources = getattr(self.owner.client, 'resources', {}) or {}
        worlds = [w for names in center_worlds(resources).values() for w in names if w in text]
        if len(set(worlds)) != 1:
            return ''
        hunts = []
        for key, definition in resources.get('DatabaseHunt', {}).items():
            if definition.get('Rank')!=3:continue
            try:
                name = self.owner.client.info(key, definition)['名称']
            except (ValueError, KeyError, TypeError):
                continue
            if name and name in text:
                hunts.append(key)
        if len(set(hunts)) != 1:
            return ''
        line = re.search(r'(?:第)?([1-9]|1[0-9]|20)\s*(?:线|線)', text)
        return digest([hunts[0], worlds[0], int(line[1]) if line else 1, pos])

    def connected(self, platform):
        manager = getattr(self.context, 'platform_manager', None)
        for p in getattr(manager, 'platform_insts', []):
            if p.meta().id == platform:
                if p.meta().name == 'aiocqhttp':
                    try:
                        if not getattr(p.get_client(), '_wsr_api_clients', {}):
                            raise SafeDeliveryFailure('机器人协议端未连接，消息尚未发送')
                    except SafeDeliveryFailure:
                        raise
                    except Exception as exc:
                        raise SafeDeliveryFailure('机器人协议端未连接，消息尚未发送') from exc
                return p
        raise SafeDeliveryFailure('发送机器人不存在，消息尚未发送')

    async def send(self, platform, target, segments, action, feature, event_key=None, report=None, api=False, parent=''):
        if time.time()-self.pruned_at>600:
            self.prune();self.pruned_at=time.time()
        scope = dict(SCOPE.get() or {})
        feature = scope.get('feature', feature)
        event_key = event_key or scope.get('event_key') or uuid.uuid4().hex
        text = ''.join(s.get('data',{}).get('text','') for s in segments or [] if s.get('type')=='text')
        try:saved = self.stash(segments) if segments is not None else None
        except (ValueError,OSError,TypeError):saved=None
        media = any(s.get('type')=='image' for s in segments or [])
        semantic = self.semantic(text, report) if scope.get('dedup', True) and feature in ('sightings','forwarding','collection') else ''
        request_key = digest([platform,str(target),feature,event_key,segments if segments is not None else text])
        lock_key = digest([platform,str(target),semantic or request_key])
        if len(self.locks)>2000:
            self.locks={k:v for k,v in self.locks.items() if v.locked()}
        async with self.locks.setdefault(lock_key, asyncio.Lock()):
            previous = self.query('SELECT * FROM deliveries WHERE request_key=? ORDER BY updated DESC LIMIT 1',(request_key,))
            if previous and previous[0]['state'] in ('sent','accepted','merged','retried'):
                if api:
                    return {'_bear_merged':True}
                return True
            if previous and previous[0]['state'] in ('unknown','held','sending'):
                if api:
                    return {'_bear_unknown':True}
                raise DeliveryUncertain('此前发送结果待确认，未重复发送')
            settings=self.owner.config.get('operations',{}).get('dedup',{})
            recent=[]
            if semantic and settings.get('enabled',True):
                recent=self.query("SELECT * FROM deliveries WHERE platform=? AND target=? AND semantic=? AND updated>? AND state IN ('sent','accepted','unknown','held') ORDER BY updated DESC LIMIT 20",
                    (platform,str(target),semantic,time.time()-settings.get('window_seconds',180)))
                recent=[r for r in recent if r['feature']!=feature and (not media or r['has_image'])]
            identity = previous[0]['id'] if previous else uuid.uuid4().hex
            attempts = previous[0]['attempts'] if previous else 0
            metadata={k:v for k,v in scope.items() if k!='stats'}
            if report:metadata['report']=report
            metadata.setdefault('expires_at',time.time()+300)
            if not previous:
                self.execute('INSERT INTO deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identity,request_key,time.time(),time.time(),platform,str(target),feature,'sending','',json.dumps(saved,ensure_ascii=False),json.dumps(metadata,ensure_ascii=False),semantic,int(media),0,'',parent))
            self.execute('DELETE FROM deliveries WHERE id NOT IN (SELECT id FROM deliveries ORDER BY updated DESC LIMIT 1000)')
            if recent:
                state='merged' if recent[0]['state'] in ('sent','accepted') else 'held'
                self.execute('UPDATE deliveries SET state=?,detail=?,updated=? WHERE id=?',(state,'同群同怪同服同分线同坐标的报告已合并' if state=='merged' else '相同报告此前发送结果待确认，暂停重复发送',time.time(),identity))
                scope.get('stats',{}).__setitem__('merged',scope.get('stats',{}).get('merged',0)+1)
                if api:return {'_bear_merged':state=='merged','_bear_unknown':state=='held'}
                if state=='merged':return True
                raise DeliveryUncertain('相同报告发送结果待确认')
            if attempts>=3:
                raise SafeDeliveryFailure('已达到三次自动尝试上限，可在发送记录核对后重试')
            self.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,updated=? WHERE id=?",(time.time(),identity))
            try:
                self.connected(platform)
                result=await action()
                if result is False:
                    raise SafeDeliveryFailure('未找到可发送的平台')
                if isinstance(result,dict) and (result.get('status')=='failed' or result.get('retcode',0) not in (0,None)):
                    raise SafeDeliveryFailure('OneBot 明确返回发送失败')
                data=result.get('data',result) if isinstance(result,dict) else {}
                mid=str(data.get('message_id') or '') if isinstance(data,dict) else ''
                state='sent' if mid else 'unknown' if api else 'accepted'
                self.execute('UPDATE deliveries SET state=?,message_id=?,detail=?,updated=? WHERE id=?',(state,mid,'接口未返回消息ID，结果待确认' if state=='unknown' else '',time.time(),identity))
                if state=='unknown':return {'_bear_unknown':True}
                if scope.get('stats') is not None:scope['stats']['sent']+=1
                return result
            except asyncio.CancelledError:
                self.execute("UPDATE deliveries SET state='unknown',detail='发送被中断，结果待确认',updated=? WHERE id=?",(time.time(),identity))
                raise
            except Exception as exc:
                safe=isinstance(exc,SafeDeliveryFailure) or type(exc).__name__ in ('ApiNotAvailable','ActionFailed')
                self.execute('UPDATE deliveries SET state=?,detail=?,updated=? WHERE id=?',('failed' if safe else 'unknown',str(exc) or type(exc).__name__,time.time(),identity))
                if safe:raise SafeDeliveryFailure(str(exc) or type(exc).__name__) from exc
                if api:return {'_bear_unknown':True}
                raise DeliveryUncertain('发送结果待确认：'+(str(exc) or type(exc).__name__)) from exc

    def records(self, state='', feature='', target='', limit=100):
        where=[];args=[]
        for key,value in [('state',state),('feature',feature),('target',target)]:
            if value:where.append(key+'=?');args.append(value)
        rows=self.query('SELECT * FROM deliveries'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY updated DESC LIMIT ?',(*args,min(200,max(1,limit))))
        for row in rows:
            payload=json.loads(row.pop('payload') or 'null')
            meta=json.loads(row.pop('metadata') or '{}')
            row['text']=''.join(s.get('data',{}).get('text','') for s in payload or [] if s.get('type')=='text')[:2000]
            row['can_retry']=row['state']=='failed' and row['feature'] not in ('trigger','commands') and bool(payload) and meta.get('expires_at',0)>time.time()
            row['feature_name']=FEATURE_LABELS.get(row['feature'],row['feature'])
            row.pop('request_key',None);row.pop('semantic',None)
        return rows

    def close(self):
        try:self.prune()
        finally:self.db.close()


class DeliveryContext:
    def __init__(self, original, journal):
        self.original, self.bear_delivery = original, journal
    def __getattr__(self,name):return getattr(self.original,name)
    async def send_message(self,origin,chain):
        return await recorded_chain(self,origin,chain)


async def recorded_chain(context,origin,chain,feature='sightings',event_key=None,report=None,expires_at=None):
    journal=getattr(context,'bear_delivery',None)
    if not isinstance(journal,DeliveryJournal):return await context.send_message(origin,chain)
    platform,_,target=origin.split(':',2)
    with delivery_scope(feature,event_key,**({'expires_at':expires_at} if expires_at else {})):
        return await journal.send(platform,target,journal.chain_segments(chain),lambda:journal.context.send_message(origin,chain),feature,event_key,report)


async def recorded_event(owner,event,chain,feature='commands'):
    operations=getattr(owner,'operations',None)
    if operations is None:return await event.send(chain)
    origin=event.unified_msg_origin
    platform,_,target=origin.split(':',2)
    target=str(getattr(event,'get_group_id',lambda:'')() or target)
    key=str(getattr(event.message_obj,'message_id',''))+':'+digest(operations.delivery.chain_segments(chain))
    with delivery_scope(feature,key,expires_at=time.time()+120):
        return await operations.delivery.send(platform,target,operations.delivery.chain_segments(chain),lambda:event.send(chain),feature,key)

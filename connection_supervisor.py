"""Opt-in local NapCat recovery. Never terminates QQ or changes credentials."""
import asyncio
import copy
import os
import json
from pathlib import Path
import re
import subprocess
import time

DEFAULT = {'enabled': False, 'launcher': '', 'account': '', 'webui_port': 6099,
           'cooldown_seconds': 300, 'max_restarts_per_hour': 3}


def validate(value):
    if not isinstance(value,dict):raise ValueError('连接守护配置无效')
    result = {**DEFAULT, **copy.deepcopy(value)}
    if set(result) != set(DEFAULT) or type(result['enabled']) is not bool:
        raise ValueError('连接守护配置无效')
    for key, low, high in [('webui_port', 1, 65535), ('cooldown_seconds', 60, 3600), ('max_restarts_per_hour', 1, 6)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('连接守护端口或重启限制无效')
    if not isinstance(result['launcher'], str) or not isinstance(result['account'], str):
        raise ValueError('请填写启动器路径与快速登录账号')
    if result['enabled']:
        if os.name != 'nt':
            raise ValueError('自动启动目前适用于 Windows 本机 NapCat；其他平台仍可使用状态监测')
        path = Path(result['launcher']).resolve()
        if path.name.lower() != 'napcatwinbootmain.exe' or not path.is_file() or not (path.parent / 'NapCatWinBootHook.dll').is_file():
            raise ValueError('请选择现有 NapCat 安装目录中的 NapCatWinBootMain.exe')
        if not re.fullmatch(r'[1-9][0-9]{4,15}', result['account']):
            raise ValueError('快速登录账号应为已登录过的 QQ 号')
        result['launcher'] = str(path)
    return result


class ConnectionSupervisor:
    def __init__(self, owner):
        self.owner = owner
        self.task = None
        self.attempts = []
        self.misses = 0
        self.last_error = ''
        self.last_start = 0
        self.note = '等待连接检查'
        self.process = None
        self.state_path=Path(owner.alerts.data_dir)/'connection_restarts.json'
        try:
            saved=json.loads(self.state_path.read_text('utf-8'))
            self.attempts=[float(t) for t in saved['attempts'] if float(t)>0 and time.time()-float(t)<3600]
            self.last_start=float(saved.get('last_start',0))
        except (OSError,ValueError,KeyError,TypeError):pass

    def config(self):
        return {**DEFAULT, **self.owner.config.get('operations', {}).get('watchdog', {})}

    async def port_open(self, port):
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 2)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def tick(self, now=None):
        now = time.time() if now is None else now
        cfg = self.config()
        connection = self.owner.webui.bot_connection()
        if connection['connected'] is True:
            self.misses = 0
            self.last_error = ''
            self.note = '机器人连接正常'
            return
        if not cfg['enabled']:
            self.note = '自动恢复未启用'
            return
        if connection.get('connected') is None or not connection.get('platform_id'):
            self.note='请在接收与监听中选择 OneBot 发送机器人后使用自动恢复'
            return
        self.misses += 1
        if self.misses < 3:
            self.note = '连接中断，等待连续三次检查确认'
            return
        if await self.port_open(cfg['webui_port']):
            self.note = 'NapCat 正在运行；等待登录或反向 WebSocket 恢复，不重复启动'
            return
        self.attempts = [at for at in self.attempts if now - at < 3600]
        if now - self.last_start < cfg['cooldown_seconds'] or len(self.attempts) >= cfg['max_restarts_per_hour']:
            self.note = '已进入重启冷却或达到每小时上限'
            return
        if self.process and self.process.poll() is None:
            self.note = '启动器仍在运行，等待恢复'
            return
        cfg = validate(cfg)
        self.last_start = now
        self.attempts.append(now)
        temporary=self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'attempts':self.attempts,'last_start':now}),'utf-8')
        temporary.replace(self.state_path)
        self.process = subprocess.Popen([cfg['launcher'], cfg['account']], cwd=str(Path(cfg['launcher']).parent),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.note = '已启动现有 NapCat，等待快速登录与协议连接'

    def status(self):
        return {'note': self.note, 'last_error': self.last_error, 'last_start': self.last_start,
                'restarts_last_hour': sum(time.time() - t < 3600 for t in self.attempts)}

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self.run(), name='bear-connection-supervisor')

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                self.note = '连接守护未能启动，请检查启动器配置'
            await asyncio.sleep(15)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

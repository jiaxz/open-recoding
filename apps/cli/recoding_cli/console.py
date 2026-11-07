"""Interactive commands and human routing, independent of terminal layout."""

import json
from pathlib import Path
import re
import shlex
import threading

from .host import resolve_agent
from .client import NoSessionClient


COMMANDS = [
    ('/agents', 'Agent 列表，选择聊天对象'), ('/to ', '切换默认收件人'),
    ('/messages ', '全部消息 / 只看人机消息'), ('/view ', '查看全部或指定 Agent 的消息'),
    ('/settings', '模型、并发和休眠设置'), ('/model ', '切换整个 Session 的模型'),
    ('/config ', '一次修改模型、并发与休眠设置'),
    ('/max-agents ', '设置 sub 并发上限 0–99'), ('/sleep ', '设置兜底休眠 0.1–3600 秒'),
    ('/speed', 'Session 输出速度；← 扩大、→ 缩短最近时间窗口'),
    ('/processes', '全部 Node 的 Bash/Python 进程、调用模式和耗时'),
    ('/python', '当前运行 / ready 的 Python 进程'),
    ('/executions', '当前查看 Agent 的后台任务；/executions ID 查看日志'),
    ('/status', 'Session 状态、工作目录和设置'), ('/stop', '停止全部 Agent'),
    ('/resume', '选择本目录 Session，进入子目录或返回上级'), ('/wake', '唤醒当前 Session 的 Master'), ('/history ', '加载最近 N 条消息，最多 200'),
    ('/attach ', '给下一条输入添加 PNG/JPEG'), ('/clear-attachments', '清除待发送图片'),
    ('/rename ', '修改 Session 名称'), ('/pin', '置顶 Session'), ('/unpin', '取消置顶'),
    ('/compact', '查看后台压缩；/compact cancel [ID] 取消'),
    ('/draft', '取回操作设置前暂存的聊天草稿'),
    ('/export ', '将全部通信消息导出为 JSON'), ('/retry', '恢复上次发送失败的草稿'),
    ('/help', '输入操作与 / 命令'), ('/quit', '离开界面（也可 Ctrl+D）'), ('/exit', '离开界面，同 /quit'),
]


class Console:
    def __init__(self, client, args, *, sessions=None):
        self.client = client
        self.sessions = sessions
        self.lock = threading.RLock()
        self.target = getattr(args, 'to', 'master')
        self.scope = getattr(args, 'messages', 'all')
        self.view = None if getattr(args,'agent',None)=='all' else getattr(args,'agent',None)
        self.attachments = []
        self.failed = []

    def select(self, target):
        with self.lock: self.target = target

    def recipient(self,value):
        if isinstance(self.client,NoSessionClient):
            if value=='master':return value
            raise ValueError('新 Session 尚未创建，第一条消息请发给 master')
        return resolve_agent(self.client.call('agents'),value)

    def send(self, text, target):
        original=text
        pictures=[]
        try:
            matched=re.match(r'^@(?:"([^"]+)"|\'([^\']+)\'|(\S+))(?:\s+([\s\S]*))?$',text)
            if matched:
                target=self.recipient(next(value for value in matched.groups()[:3] if value is not None))
                text=matched.group(4) or ''
                if not text.strip():
                    self.select(target)
                    return {'panel':None,'notice':'已切换收件人','target':target}
            with self.lock:
                pictures=list(self.attachments)
                self.attachments.clear()
            if isinstance(self.client,NoSessionClient):
                if self.sessions is None:raise ValueError('请先配置 Session 导航')
                client=self.sessions.create_with_message(text,target,pictures)
                return {'sent':True,'switch':client}
            self.client.send(text,target,pictures)
        except Exception:
            with self.lock:self.failed.append({'text':original,'target':target,'images':pictures})
            raise
        return {'sent':True}

    def execute(self, text, *, target=None):
        with self.lock:
            target = target or self.target
            has_images = bool(self.attachments)
        if not text.strip():
            return self.send('', target) if has_images else {}
        if not text.startswith('/'):
            return self.send(text, target)
        command, *values = shlex.split(text)
        if command in {'/quit','/exit'}: return {'quit':True}
        if command == '/agents': return {'panel':'agents', 'data':self.client.call('agents')}
        if command in {'/help','/settings','/status'}:
            return {'panel':command[1:], 'data':self.client.call('status') if command != '/help' else None}
        if command == '/to':
            if not values: return {'panel':'agents', 'data':self.client.call('agents')}
            if len(values) != 1: raise ValueError('/to master|ID|"名字"|all|subs')
            self.select(self.recipient(values[0]))
            return {'panel':None, 'notice':'已切换收件人', 'target':self.target}
        if command == '/view':
            view = None if not values or values[0]=='all' else resolve_agent(self.client.call('agents'), values[0])
            if view in {'all','all_except_master'}: raise ValueError('/view all|master|ID')
            with self.lock: self.view = view
            return {'view':view, 'notice':'已切换查看范围', 'panel':None}
        if command == '/messages':
            if len(values)!=1 or values[0] not in {'human','all'}: raise ValueError('/messages human|all')
            with self.lock: self.scope = values[0]
            return {'scope':self.scope, 'notice':'显示全部消息' if self.scope=='all' else '只显示人机消息', 'panel':None}
        if command == '/history':
            count=int(values[0]) if values else 100
            if not 1 <= count <= 200: raise ValueError('History: 1–200')
            return {'history':self.client.call('messages',limit=count)['messages'], 'notice':f'已加载最近 {count} 条记录'}
        if command == '/speed':
            allowed={'10m','1h','1d','all','wider','narrower','in','out','left','right','reset','off'}
            if len(values)>1 or values and values[0] not in allowed:
                raise ValueError('/speed [10m|1h|1d|all|wider|narrower|in|out|left|right|reset|off]')
            if values==['off']:return {'panel':None}
            return {'panel':'speed','data':self.client.call('speed'),'speed_action':values[0] if values else None}
        if command == '/processes':
            if values:raise ValueError('/processes 不接受参数；执行日志使用 /executions')
            return {'panel':'processes','data':self.client.call('status')}
        if command == '/python':
            if values:raise ValueError('/python 不接受参数；执行日志使用 /executions')
            return {'panel':'python_pool','data':self.client.call('python_pool')}
        if command == '/executions':
            agent_id=self.view or (target if target not in {'all','all_except_master'} else 'master')
            if not values:return {'panel':'executions','data':self.client.call('executions',agent_id=agent_id)}
            if len(values) not in {1,3}:raise ValueError('/executions [ID [stdout字节游标 stderr字节游标]]')
            offsets=[int(value) for value in values[1:]] if len(values)==3 else [0,0]
            result=self.client.call('execution_output',agent_id=agent_id,execution_id=values[0],
                stdout_offset=offsets[0],stderr_offset=offsets[1],max_bytes=8000)
            return {'panel':'execution_output','data':{**result,'agent_id':agent_id}}
        if command == '/resume':
            if self.sessions is None:raise ValueError('此界面未配置 Session 导航；可用 -s 指定要恢复的存档')
            if values and values[0]=='--directory':
                if len(values)!=2:raise ValueError('/resume --directory 路径')
                return {'panel':'sessions','data':self.sessions.change_directory(values[1])}
            if values and values[0].startswith('--'):raise ValueError('/resume 不递归搜索；请选择子目录或返回上一级')
            if not values:
                return {'panel':'sessions','data':self.sessions.listing()}
            if len(values)!=1:raise ValueError('/resume ID、名字或存档路径；含空格时请加引号')
            return {'switch':self.sessions.restore(values[0])}
        if command in {'/stop','/wake'}:
            if values:raise ValueError(command+' 不接受参数')
            if command=='/stop' and self.sessions:return {'switch':self.sessions.stop()}
            result=self.sessions.wake(self.client) if command=='/wake' and self.sessions else self.client.call('stop' if command=='/stop' else 'wake')
            return {'notice':'全部 Agent 已停止' if command=='/stop' else
                    '已发送唤醒消息' if result.get('action')=='signalled' else
                    'Master 已在运行' if result.get('action')=='active' else '正在唤醒 Master'}
        if command in {'/model','/max-agents','/sleep','/config'}:
            if not values: return {'panel':'settings','data':self.client.call('status')}
            if command == '/config':
                settings={}
                for value in values:
                    key,sep,raw=value.partition('=')
                    if not sep or key not in {'model','max_subagents','idle_sleep_seconds'}:
                        raise ValueError('/config model=ID max_subagents=N idle_sleep_seconds=N')
                    settings[key]=raw if key=='model' else int(raw) if key=='max_subagents' else float(raw)
            else:
                if len(values)!=1: raise ValueError(f'{command} 需要一个值')
                key={'/model':'model','/max-agents':'max_subagents','/sleep':'idle_sleep_seconds'}[command]
                settings={key:values[0] if key=='model' else int(values[0]) if key=='max_subagents' else float(values[0])}
            result=self.client.call('config',values=settings)
            return {'panel':'settings','data':self.client.call('status'), 'notice':'设置仅暂存于当前窗口，Resume 时应用' if result.get('pending') else '设置已保存'}
        if command == '/attach':
            if len(values)!=1: raise ValueError('/attach "图片路径"')
            path=Path(values[0]).expanduser()
            if not path.is_file() or path.stat().st_size>5*1024*1024: raise ValueError('图片文件需存在且不超过 5 MiB')
            with self.lock:
                if len(self.attachments)>=8: raise ValueError('每条消息最多 8 张图片')
                self.attachments.append(str(path))
            return {'notice':'图片将在下一条消息中发送'}
        if command == '/clear-attachments':
            with self.lock: self.attachments.clear()
            return {'notice':'待发送图片已清除'}
        if command in {'/rename','/pin','/unpin'}:
            if command=='/rename' and not values: raise ValueError('/rename "Session 名字"')
            self.client.call('rename',**({'name':' '.join(values)} if command=='/rename' else {'pinned':command=='/pin'}))
            return {'notice':'Session 信息已保存'}
        if command == '/compact':
            if not values: return {'panel':'compact','data':self.client.call('status').get('compaction',[])}
            if values[0]!='cancel' or len(values)>2: raise ValueError('/compact cancel [REQUEST_ID]')
            jobs=self.client.call('status').get('compaction',[])
            active=[operation for operation in jobs if operation['status'] in {'queued','running','cancelling'}]
            if len(values)==2:active=[operation for operation in active if operation['request_id']==values[1]]
            for operation in active:self.client.call('compact',request_id=operation['request_id'])
            return {'notice':'已取消后台压缩；Agent 继续运行' if active else '没有对应的后台压缩'}
        if command == '/export':
            if len(values)!=1: raise ValueError('/export "输出 JSON 文件"')
            records,cursor=[],'start'
            while True:
                page=self.client.call('messages',after=cursor,limit=200)
                records.extend(page['messages'])
                if not page['has_more']: break
                cursor=page['after']
            with Path(values[0]).expanduser().open('x',encoding='utf-8') as stream:
                json.dump(records,stream,ensure_ascii=False,indent=2)
            return {'notice':f'已导出 {len(records)} 条消息'}
        if command == '/retry':
            with self.lock:
                if not self.failed: raise ValueError('没有待恢复的失败草稿')
                failed=self.failed.pop()
                self.target=failed['target']
                self.attachments.extend(failed['images'])
            return {'draft':failed['text'],'notice':'失败草稿已恢复，请确认后发送'}
        raise ValueError('未知命令，输入 / 查看命令列表')

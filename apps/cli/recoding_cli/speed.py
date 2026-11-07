"""Terminal speed plot: call throughput and additive 10-second Session estimate."""

from bisect import bisect_right
from datetime import datetime
import math
import time

RANGES={'10m':600,'1h':3600,'1d':86400,'all':None}
ACTIONS={'10m':'最近10分钟','1h':'最近1小时','1d':'最近一天','all':'全部',
         'wider':'扩大最近时间窗口（←）','narrower':'缩短最近时间窗口（→）',
         'in':'放大','out':'缩小','left':'向历史平移','right':'向现在平移','reset':'恢复时间范围','off':'关闭曲线'}
from multiagent.model_config import profile_for
TOTAL='fg:#b6a275'


def number(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


class SessionRate:
    """Integral of concurrent call intervals, matching the Web's denominator."""
    def __init__(self,samples):
        deltas={}
        for sample in samples:
            start,end=sample['timestamp']-sample['elapsed'],sample['timestamp']
            rate=sample['output_tokens']/sample['elapsed']
            deltas[start]=deltas.get(start,0)+rate
            deltas[end]=deltas.get(end,0)-rate
        self.times=sorted(deltas)
        self.areas,self.rates=[],[]
        area=rate=0
        previous=self.times[0] if self.times else 0
        for stamp in self.times:
            area+=(stamp-previous)*rate
            rate+=deltas[stamp]
            self.areas.append(area);self.rates.append(rate);previous=stamp

    def integral(self,stamp):
        index=bisect_right(self.times,stamp)-1
        return 0 if index<0 else self.areas[index]+(stamp-self.times[index])*self.rates[index]

    def __call__(self,stamp):
        return max(0,(self.integral(stamp)-self.integral(stamp-10))/10)


def recent_label(seconds):
    for unit,label in ((86400,'天'),(3600,'小时'),(60,'分钟')):
        if seconds>=unit:return f'最近{seconds/unit:g}{label}'
    return f'最近{seconds:g}秒'


class SpeedPlot:
    def __init__(self):
        self.samples=[]
        self.total=SessionRate([])
        self.breaks=[]
        self.view={'preset':'1h','last':'1h','custom':None,'seconds':None}
        self.as_of=time.time();self.received=time.monotonic();self.started=self.as_of
        self.data=None
        self.y_range=(0,1)

    def update(self,data):
        self.data=data
        self.as_of=data['as_of'] if number(data.get('as_of')) else time.time()
        self.received=time.monotonic()
        started=data.get('started_at',self.as_of)
        try:self.started=float(started) if type(started) in (int,float) else datetime.fromisoformat(started.replace('Z','+00:00')).timestamp()
        except (TypeError,ValueError,OverflowError):self.started=self.as_of
        samples=[]
        for item in data.get('samples',[]):
            if not isinstance(item,dict) or not all(number(item.get(key)) for key in ('timestamp','elapsed','output_tokens')) or item['elapsed']<=0:continue
            samples.append({**item,'rate':item['output_tokens']/item['elapsed']})
        samples.sort(key=lambda point:point['timestamp'])
        if samples!=self.samples:
            self.samples=samples
            self.total=SessionRate(samples)
            self.breaks=sorted({stamp for point in samples for stamp in
                (point['timestamp']-point['elapsed'],point['timestamp'],point['timestamp']-point['elapsed']+10,point['timestamp']+10)})

    def now(self):return self.as_of+max(0,time.monotonic()-self.received)

    def domain(self,now):
        start=min([self.started]+[p['timestamp']-p['elapsed'] for p in self.samples])
        end=max(now,self.samples[-1]['timestamp'] if self.samples else now,start+1)
        return max(0,start),end

    def bounds(self,now):
        first,last=self.domain(now)
        if self.view['preset']:
            seconds=self.view['seconds'] if self.view['preset']=='recent' else RANGES[self.view['preset']]
            return max(first,last-seconds) if seconds else first,last
        start,end=self.view['custom']
        span=min(end-start,last-first)
        start=max(first,min(last-span,start))
        return start,start+span

    def select(self,action):
        if action is None:return
        if action in RANGES:
            self.view.update(preset=action,last=action,custom=None,seconds=None);return
        if action=='reset':
            self.select(self.view['last']);return
        now=self.now();first,last=self.domain(now);start,end=self.bounds(now)
        span=end-start
        if action in {'wider','narrower'}:
            seconds=self.view.get('seconds') if self.view['preset']=='recent' else RANGES.get(self.view['preset'])
            seconds=max(1,min(315360000,(seconds or span)*(2 if action=='wider' else .5)))
            self.view.update(preset='recent',seconds=seconds,custom=None)
            return
        if action in {'in','out'}:
            changed=max(min(1,last-first),min(last-first,span*(.5 if action=='in' else 2)))
            start+=(span-changed)/2;span=changed
        elif action in {'left','right'}:start+=span*(-.25 if action=='left' else .25)
        else:return
        start=max(first,min(last-span,start))
        self.view.update(preset=None,custom=(start,start+span))

    def render(self,width,height,*,now=None):
        """Return prompt_toolkit fragments; generated styles never contain API text."""
        width,height=max(18,width),max(1,height)
        now=self.now() if now is None else now
        start,end=self.bounds(now)
        if height<6:
            latest=self.samples[-1] if self.samples else None
            recent=f"{latest['rate']:.1f}" if latest else '—'
            combined=f'{self.total(now):.1f}' if latest else '—'
            return [('class:panel.title',f' 最近 {recent} · 合计10s {combined} tok/s\n'),
                    ('class:muted',' 增大终端查看曲线；/speed off 关闭')]
        calls=[point for point in self.samples if start<=point['timestamp']<=end]
        stamps=sorted({start,end,*[stamp for stamp in self.breaks if start<stamp<end]})
        aggregate=[{'timestamp':stamp,'rate':self.total(stamp)} for stamp in stamps] if self.samples else []
        values=[p['rate'] for p in [*calls,*aggregate]]
        low,high=(min(values),max(values)) if values else (0,1)
        padding=max((high-low)*.06,high*.03,.05)
        self.y_range=(max(0,low-padding),high+padding)
        low,high=self.y_range
        left=7;columns=max(6,width-left-1);rows=max(2,height-4)
        pixel_width,pixel_height=columns*2,rows*4
        cells={}
        dots={}
        def pixel(x,y,style,layer):
            x=max(0,min(pixel_width-1,x));y=max(0,min(pixel_height-1,y))
            key=(x//2,y//4)
            bit=((0,3),(1,4),(2,5),(6,7))[y%4][x%2]
            mask,old_style,old_layer=cells.get(key,(0,style,layer))
            cells[key]=(mask|1<<bit,style if layer>=old_layer else old_style,max(old_layer,layer))
        def position(p):
            return round((p['timestamp']-start)/max(1e-9,end-start)*(pixel_width-1)),round((high-p['rate'])/(high-low)*(pixel_height-1))
        def line(a,b,style,layer):
            x0,y0=position(a);x1,y1=position(b)
            steps=max(abs(x1-x0),abs(y1-y0),1)
            for i in range(steps+1):pixel(round(x0+(x1-x0)*i/steps),round(y0+(y1-y0)*i/steps),style,layer)
        for a,b in zip(aggregate,aggregate[1:]):line(a,b,TOTAL,0)
        for point in calls:
            x,y=position(point);style='fg:'+profile_for(point.get('model') or '').color;pixel(x,y,style,2)
            dots[(x//2,y//4)]=style
        fragments=[]
        latest=self.samples[-1] if self.samples else None
        recent=f"{latest['rate']:.1f}" if latest else '—'
        combined=f'{self.total(now):.1f}' if latest else '—'
        fragments += [('class:panel.title',f' 最近 {recent}  · 合计10s {combined} tok/s\n')]
        for row in range(rows):
            tick=f'{high-(high-low)*row/max(1,rows-1):6.1f}│'
            fragments.append(('class:muted',tick))
            for column in range(columns):
                value=cells.get((column,row))
                dot=dots.get((column,row))
                fragments.append((dot or (value[1] if value else 'class:muted'),
                    '•' if dot else chr(0x2800+value[0]) if value else ('·' if row==rows-1 else ' ')))
            fragments.append(('','\n'))
        clock_format='%m-%d %H:%M' if end-start>=86400 else '%H:%M:%S'
        clock=lambda stamp:datetime.fromtimestamp(stamp).astimezone().strftime(clock_format)
        axis=f"{clock(start)} → {clock(end)}"+(' NOW' if abs(end-now)<1 else '')
        fragments.append(('class:muted',(' '*left+axis)[:width]+'\n'))
        if not latest:
            fragments.append(('class:muted',' 等待 API 回报 Output；Demo 不虚构速度\n'))
        else:
            fragments.append((TOTAL,' ─ 合计10s '))
            for model in dict.fromkeys(p.get('model') or '' for p in self.samples):
                fragments.append(('fg:'+profile_for(model or '').color,'● '+(profile_for(model or '').label or model[:12] or 'Other')+' '))
            fragments.append(('','\n'))
        label=recent_label(self.view['seconds']) if self.view['preset']=='recent' else ACTIONS.get(self.view['preset'],'自定义窗口')
        fragments.append(('class:muted',f' {label} · Output/总耗时；合计为10s平滑估计'))
        return fragments

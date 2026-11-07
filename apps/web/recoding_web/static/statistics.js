"use strict";

// Presentation only: all source measurements arrive from server observations.
window.AgentStatistics = (() => {
  const node = (tag, value, cls) => {
    const el = document.createElement(tag);el.textContent = value ?? "";
    if (cls) el.className = cls;
    return el;
  };
  const number = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const duration = value => number(value) ? `${value.toFixed(2)}s` : "—";
  const count = value => number(value) ? value.toLocaleString() : "—";
  let profiles = {}, modelStyle = {};
  const colorValue = (value, fallback) => /^#[0-9a-f]{6}$/i.test(value || "") ? value : fallback;
  function modelPresentation(id) {
    if(Object.prototype.hasOwnProperty.call(profiles,id))return profiles[id];
    const colors=(modelStyle.unknown_colors || []).filter(value=>/^#[0-9a-f]{6}$/i.test(value));
    let hash=2166136261;
    for(const byte of new TextEncoder().encode(String(id || "")))hash=Math.imul(hash^byte,16777619)>>>0;
    return {label:id || "",short_label:id || "",family:"other",color:colors.length ? colors[hash%colors.length] : "#a0aaa8"};
  }
  const modelFamily = id => modelPresentation(id).family || "other";
  const identifyModel = (element, id) => {
    const profile=modelPresentation(id);
    element.dataset.model = id || "";element.dataset.family = profile.family || "other";
    element.style.setProperty("--model-accent",colorValue(profile.color,"#a0aaa8"));
    element.style.setProperty("--model-tint",colorValue(profile.tint,"#252c2c"));
    element.style.setProperty("--model-border",colorValue(profile.border,"#485352"));
    return element;
  };
  function configureModels(values, style) {
    profiles=values || {};modelStyle=style || {};
    for(const element of document.querySelectorAll("[data-model]"))identifyModel(element,element.dataset.model);
  }
  const outputRate = (tokens, elapsed) => number(tokens) && number(elapsed) && elapsed > 0 ? tokens / elapsed : null;
  const clock = value => new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false}).format(new Date(value * 1000));

  function modelRate(event) {
    const tokens = event.usage?.completion_tokens;
    const compression=["context_compaction_end","context_compaction_error"].includes(event.type);
    if (event.type !== "model_end" && !compression || compression && typeof event.id!=="string" || typeof event.run_id !== "string" || !event.run_id || !Number.isInteger(event.step) || event.step < 1 || !Number.isInteger(tokens) || tokens < 0 ||
        !number(event.elapsed) || event.elapsed === 0 || !number(event.timestamp)) return null;
    return {run_id: compression ? `${event.run_id}/context/${event.id}` : event.run_id, ...(compression ? {purpose:"context_compaction"} : {}),step: event.step, timestamp: event.timestamp, elapsed: event.elapsed,
      output_tokens: tokens, rate: outputRate(tokens, event.elapsed), model: event.model || null,
      agent_id: event.agent_id || "master", agent_name: event.agent_name || "Master"};
  }

  // Each call contributes its API Output uniformly over its measured interval.
  // Integrate the sum of concurrent rates, then use a trailing time window;
  // agents are added, never averaged, and Thinking is not counted twice.
  function sessionIntegrator(samples) {
    const deltas=new Map();
    for(const p of samples)if(number(p.timestamp) && number(p.elapsed) && p.elapsed>0 && number(p.output_tokens)) {
      const start=p.timestamp-p.elapsed,rate=p.output_tokens/p.elapsed;
      deltas.set(start,(deltas.get(start)||0)+rate);deltas.set(p.timestamp,(deltas.get(p.timestamp)||0)-rate);
    }
    const times=[...deltas.keys()].sort((a,b)=>a-b),areas=[],rates=[];
    let area=0,rate=0,previous=times[0] || 0;
    for(const time of times){area+=(time-previous)*rate;rate+=deltas.get(time);areas.push(area);rates.push(rate);previous=time;}
    const integral=time=>{
      let lo=0,hi=times.length;
      while(lo<hi){const mid=(lo+hi)>>>1;if(times[mid]<=time)lo=mid+1;else hi=mid;}
      const index=lo-1;return index<0 ? 0 : areas[index]+(time-times[index])*rates[index];
    };
    return (time,windowSeconds=10)=>Math.max(0,(integral(time)-integral(time-windowSeconds))/windowSeconds);
  }
  function sessionOutputRate(samples,time,windowSeconds=10) {
    if(!Number.isFinite(time) || !Number.isFinite(windowSeconds) || windowSeconds<=0)return null;
    return sessionIntegrator(samples)(time,windowSeconds);
  }

  const speedViews = new Map(), speedContainers = new WeakMap();
  const speedRanges = new Map([["10m",{label:"最近10分钟",seconds:600}], ["1h",{label:"最近1小时",seconds:3600}],
    ["1d",{label:"最近一天",seconds:86400}], ["all",{label:"全部",seconds:Infinity}]]);
  let speedClip = 0;

  function renderSpeed(container, samples, scope, {startedAt} = {}) {
    const points = samples.filter(p => number(p.rate) && number(p.timestamp) && number(p.elapsed) && p.elapsed > 0)
      .sort((a, b) => a.timestamp - b.timestamp);
    const view = (scope == null ? speedContainers.get(container)?.view : speedViews.get(scope)) ||
      {preset:"1h",lastPreset:"1h",full:true,start:0,end:1,origin:number(startedAt) ? startedAt : Date.now()/1000};
    if(scope != null)speedViews.set(scope,view);
    container.replaceChildren();
    container.dataset.samples = points.length;
    container.dataset.timeUnit = "seconds";
    const heading = node("div", "", "speed-heading");
    heading.append(node("span", "SESSION · OUTPUT SPEED", "section-label"));
    const latest = points.at(-1);
    const value = identifyModel(node("div", "", "speed-value"), latest?.model);
    value.append(node("strong", latest ? latest.rate.toFixed(1) : "—"), node("span", "tok/s · 最近调用"));
    const totalValue=node("div","","speed-value speed-total-value"),totalNumber=node("strong","—");
    totalValue.append(totalNumber,node("span","tok/s · 合计 10s"));
    const values=node("div","","speed-values");values.append(value,totalValue);container.append(heading,values);
    const totalSeconds = points.reduce((sum, p) => sum + p.elapsed, 0);
    const average = totalSeconds ? points.reduce((sum, p) => sum + p.output_tokens, 0) / totalSeconds : null;
    const svgNode = (tag, attrs) => {
      const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, val] of Object.entries(attrs)) el.setAttribute(key, val);
      return el;
    };
    const width = 260, height = 122, left = 33, right = 8, top = 12, bottom = 20;
    const sessionRate=sessionIntegrator(points);
    // The moving-window sum is piecewise linear. Include every change in its
    // slope before reducing to screen pixels, so short bursts aren't missed.
    const aggregateTimes=[...new Set(points.flatMap(p=>[p.timestamp-p.elapsed,p.timestamp,p.timestamp-p.elapsed+10,p.timestamp+10]))].sort((a,b)=>a-b);
    const aggregatePoints=aggregateTimes.map(timestamp=>({timestamp,rate:sessionRate(timestamp)}));
    const first=points.reduce((start,p)=>Math.min(start,p.timestamp-p.elapsed),number(startedAt) ? startedAt : points[0]?.timestamp ?? view.origin);
    const domainStart=Math.max(0,first);
    let now,domainEnd,fullSpan,minimumSpan;
    function updateDomain(){
      now=Date.now()/1000;domainEnd=Math.max(now,latest?.timestamp ?? 0,domainStart+1);
      fullSpan=domainEnd-domainStart;minimumSpan=Math.min(fullSpan,1);
    }
    updateDomain();
    const plotWidth = width - left - right;
    const x = p => left + (p.timestamp - view.start) / (view.end - view.start) * plotWidth;
    let yMin=0,yMax=1;
    const y = p => height - bottom - (p.rate-yMin)/(yMax-yMin) * (height - bottom - top);
    const svg = svgNode("svg", {viewBox: `0 0 ${width} ${height}`, class: "speed-chart", role: "img", tabindex: "0",
      "aria-label": `Session 输出速度：逐次调用与所有 Agent 合计 10 秒平滑，共用时间轴和 tok/s 刻度。方向键平移，加减键缩放，Home 恢复全时段。`});
    const ranges=node("div","","speed-ranges"),rangeButtons=new Map();
    ranges.setAttribute("role","group");ranges.setAttribute("aria-label","速度图时间窗口");
    for(const [key,{label}] of speedRanges) {
      const button=node("button",label);button.type="button";button.dataset.speedRange=key;
      button.addEventListener("click",()=>selectRange(key));ranges.append(button);rangeButtons.set(key,button);
    }
    const restoreRange=node("button","恢复时间窗口","text-button speed-restore-range");restoreRange.type="button";
    restoreRange.addEventListener("click",()=>selectRange(view.lastPreset));
    const windowLabel=node("div","","speed-window");
    const curveLegend=node("div","","speed-curve-legend");
    curveLegend.append(node("span","逐次调用 · 模型色","speed-call-key"),node("span","Session 合计 · 10s","speed-total-key"));
    // Totals and per-model averages always describe the entire Session,
    // independently of the displayed time window.
    const groups = new Map();
    for (const point of points) {
      const id = point.model || "", group = groups.get(id) || {tokens: 0, elapsed: 0, calls: 0};
      group.tokens += point.output_tokens;group.elapsed += point.elapsed;group.calls++;groups.set(id, group);
    }
    const detail = identifyModel(node("p", "", "speed-detail"), latest?.model);
    const describe = p => `${clock(p.timestamp)} · ${p.agent_name || "Master"} · ${p.purpose==="context_compaction" ? "Context compression" : `Model call #${p.step}`} · ${p.model || "模型未记录"}\n${p.rate.toFixed(1)} tok/s · ${count(p.output_tokens)} Output / ${duration(p.elapsed)}`;
    detail.textContent = latest ? describe(latest) : "等待 API 回报 Output tokens；Demo 或缺少用量时不估算。";
    function boundWindow(start, span) {
      span = Math.max(minimumSpan, Math.min(fullSpan, span));
      view.full = span >= fullSpan * (1 - 1e-9);
      view.start = view.full ? domainStart : Math.max(domainStart, Math.min(domainEnd - span, start));
      view.end = view.full ? domainEnd : view.start + span;
    }
    function followRange() {
      const span=Math.min(fullSpan,speedRanges.get(view.preset).seconds);
      boundWindow(domainEnd-span,span);
    }
    function selectRange(key) {
      view.preset=view.lastPreset=key;updateDomain();followRange();draw();
    }
    function setWindow(start,span) {
      view.preset=null;boundWindow(start,span);
      draw();
    }
    const lowerBound = (time,series=points) => {
      let lo = 0, hi = series.length;
      while (lo < hi) {const mid = (lo + hi) >>> 1;if (series[mid].timestamp < time) lo = mid + 1;else hi = mid;}
      return lo;
    };
    function windowPoints(startIndex,endIndex) {
      const visible=points.slice(startIndex,endIndex);
      const boundary=(time,index)=>{
        if(index<=0 || index>=points.length || points[index].timestamp===time)return null;
        const before=points[index-1],after=points[index];
        return {...after,timestamp:time,
          rate:before.rate+(after.rate-before.rate)*(time-before.timestamp)/(after.timestamp-before.timestamp),
          interpolated:true};
      };
      const start=boundary(view.start,startIndex),end=boundary(view.end,endIndex);
      if(start)visible.unshift(start);
      if(end && points[endIndex-1]?.timestamp!==view.end)visible.push(end);
      return visible;
    }
    function reducePixels(series) {
      const plotted=[];
      for(let i=0;i<series.length;) {
        const bucket=Math.floor(x(series[i]));let end=i+1;
        while(end<series.length && Math.floor(x(series[end]))===bucket)end++;
        const batch=series.slice(i,end),byModel=new Map(),selected=new Set();
        for(const point of batch){const id=point.model || "",group=byModel.get(id) || [];group.push(point);byModel.set(id,group);}
        for(const group of byModel.values())for(const point of [group[0],group.at(-1),
          group.reduce((a,b)=>a.rate<b.rate ? a : b),group.reduce((a,b)=>a.rate>b.rate ? a : b)])selected.add(point);
        plotted.push(...batch.filter(point=>selected.has(point)));i=end;
      }
      return plotted;
    }
    function draw() {
      const startIndex = lowerBound(view.start);
      let endIndex = lowerBound(view.end);
      while (endIndex < points.length && points[endIndex].timestamp === view.end) endIndex++;
      // Fit both actual visible curves, including interpolated edge crossings.
      // Off-screen peaks must not flatten the currently inspected interval.
      const visible=windowPoints(startIndex,endIndex);
      const aggregateVisible=latest ? [{timestamp:view.start,rate:sessionRate(view.start)},
        ...aggregatePoints.slice(lowerBound(view.start,aggregatePoints),lowerBound(view.end,aggregatePoints)),
        {timestamp:view.end,rate:sessionRate(view.end)}] : [];
      let low=Infinity,high=0;
      for(const p of [...visible,...aggregateVisible]){low=Math.min(low,p.rate);high=Math.max(high,p.rate);}
      yMin=0;yMax=1;
      if(high>0) {
        const padding=Math.max((high-low)*.08,high*.02);
        yMin=Math.max(0,low-padding);yMax=high+padding;
      }
      Object.assign(container.dataset, {viewStart: String(view.start), viewEnd: String(view.end), now: String(now),
        domainStart: String(domainStart), domainEnd: String(domainEnd),
        yMin:String(yMin),yMax:String(yMax),
        visibleSamples: String(endIndex - startIndex), fullRange: String(view.full),rangePreset:view.preset || "custom"});
      ranges.hidden=!view.preset;restoreRange.hidden=Boolean(view.preset);
      for(const [key,button] of rangeButtons)button.setAttribute("aria-pressed",String(key===view.preset));
      restoreRange.title=`返回${speedRanges.get(view.lastPreset).label}，跟随当前时间`;
      windowLabel.textContent = `${view.preset ? speedRanges.get(view.preset).label : "自定义窗口"} · ${endIndex - startIndex}/${points.length} calls · 当前 ${clock(now).slice(-8)}`;
      const currentRate=latest ? sessionRate(now) : null;
      totalNumber.textContent=currentRate===null ? "—" : currentRate.toFixed(1);
      svg.dataset.latestRate=currentRate===null ? "" : currentRate.toFixed(6);svg.dataset.windowSeconds="10";
      svg.replaceChildren();
      const decimals=Math.max(0,Math.min(6,1-Math.floor(Math.log10((yMax-yMin)/2))));
      for (const rate of [yMin, (yMin+yMax)/2, yMax]) {
        const axisY = y({rate});
        svg.append(svgNode("line", {x1: left, y1: axisY, x2: width - right, y2: axisY, class: "speed-grid"}));
        const label = svgNode("text", {x: left - 5, y: axisY + 3, "text-anchor": "end"});
        label.textContent = rate.toFixed(decimals);svg.append(label);
      }
      const clipId = `speed-clip-${++speedClip}`, defs = svgNode("defs", {}), clip = svgNode("clipPath", {id: clipId});
      clip.append(svgNode("rect", {x: left, y: 0, width: plotWidth, height: height - bottom + 5}));defs.append(clip);svg.append(defs);
      const plot = svgNode("g", {"clip-path": `url(#${clipId})`, class: "speed-plot"});svg.append(plot);
      // Draw the same boundary-clipped data used by the vertical scale.
      if(latest) {
        const summed=reducePixels(aggregateVisible);
        plot.append(svgNode("path",{d:summed.map((p,i)=>`${i ? "L" : "M"}${x(p).toFixed(2)},${y(p).toFixed(2)}`).join(" "),class:"speed-session-line"}));
      }
      const plotted = reducePixels(visible);
      for (let i = 1; i < plotted.length; i++) {
        const previous = plotted[i - 1], current = plotted[i];
        const path = `M${x(previous).toFixed(2)},${y(previous).toFixed(2)} L${x(current).toFixed(2)},${y(current).toFixed(2)}`;
        const line = identifyModel(svgNode("path", {d: path, class: "speed-line"}), current.model);
        line.dataset.fromModel = previous.model || "";plot.append(line);
      }
      for (const p of plotted) {
        if (p.interpolated) continue;
        const circle = identifyModel(svgNode("circle", {cx: x(p), cy: y(p), r: p === latest ? 3.5 : 2.5,
          tabindex: "0", "aria-label": describe(p), class: "speed-point"}), p.model);
        const title = svgNode("title", {});title.textContent = describe(p);circle.append(title);
        const showPoint = () => {detail.textContent = describe(p);identifyModel(detail, p.model);};
        circle.addEventListener("pointerenter", showPoint);circle.addEventListener("focus", showPoint);plot.append(circle);
      }
      if(now>=view.start && now<=view.end) {
        const currentX=x({timestamp:now});
        plot.append(svgNode("line",{x1:currentX,y1:top,x2:currentX,y2:height-bottom,class:"speed-now"}));
        const current=svgNode("text",{x:currentX-3,y:top-3,"text-anchor":"end",class:"speed-now-label"});current.textContent="NOW";svg.append(current);
      }
      for (const [time, anchor] of [[view.start, "start"], [view.end, "end"]]) {
        const label = svgNode("text", {x: anchor === "start" ? left : width - right, y: height - 3, "text-anchor": anchor});
        label.textContent = clock(time).slice(-8);svg.append(label);
      }
    }
    function zoom(factor, anchor = .5) {
      const span = view.end - view.start, next = Math.max(minimumSpan, Math.min(fullSpan, span * factor));
      setWindow(view.start + (span - next) * anchor, next);
    }
    const shift = fraction => setWindow(view.start + (view.end - view.start) * fraction, view.end - view.start);
    svg.addEventListener("keydown", event => {
      if (event.key === "+" || event.key === "=") zoom(.5);
      else if (event.key === "-" || event.key === "_") zoom(2);
      else if (event.key === "ArrowLeft") shift(-.2);
      else if (event.key === "ArrowRight") shift(.2);
      else if (event.key === "Home") selectRange("all");
      else return;
      event.preventDefault();
    });
    let drag = null;
    svg.addEventListener("pointerdown", event => {
      if (event.button !== 0 || !event.isPrimary) return;
      const pixels = svg.getBoundingClientRect().width * plotWidth / width;
      if (!pixels) return;
      drag = {id: event.pointerId, x: event.clientX, start: view.start, span: view.end - view.start, pixels, moved:false};
      svg.setPointerCapture(event.pointerId);svg.focus({preventScroll: true});svg.classList.add("is-panning");event.preventDefault();
    });
    svg.addEventListener("pointermove", event => {
      if (drag?.id === event.pointerId && (drag.moved || event.clientX!==drag.x)) {
        drag.moved=true;setWindow(drag.start - (event.clientX - drag.x) / drag.pixels * drag.span, drag.span);
      }
    });
    const endDrag = event => {
      if (drag?.id !== event.pointerId) return;
      drag = null;svg.classList.remove("is-panning");if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId);
    };
    svg.addEventListener("pointerup", endDrag);svg.addEventListener("pointercancel", endDrag);
    svg.addEventListener("wheel", event => {
      if (event.ctrlKey || event.metaKey) {
        const box = svg.getBoundingClientRect();if (!box.width) return;
        const anchor = Math.max(0, Math.min(1, ((event.clientX - box.left) / box.width * width - left) / plotWidth));
        zoom(Math.exp(Math.max(-2, Math.min(2, event.deltaY * .003))), anchor);
      } else if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        shift((event.deltaX || event.deltaY) / 300);
      } else return;
      event.preventDefault();
    }, {passive: false});
    const meta = node("div", `${average===null ? "整体均值 —" : `逐次均值 ${average.toFixed(1)} tok/s`} · ${points.length} calls`, "speed-meta");
    const legend = node("div", "", "speed-legend");
    for (const [id, group] of groups) {
      const item = identifyModel(node("div", "", "speed-model"), id);
      item.append(node("span", id || "模型未记录"), node("strong", `${(group.tokens / group.elapsed).toFixed(1)} tok/s`));
      item.title = `${id || "模型未记录"} · ${group.calls} calls · ${count(group.tokens)} Output / ${duration(group.elapsed)}`;legend.append(item);
    }
    container.append(curveLegend,svg,ranges,restoreRange,windowLabel,detail,meta,legend,
      node("p", "拖拽平移 · Ctrl/⌘+滚轮缩放 · Home 全部\n手动查看时隐藏时间选项，可恢复窗口\n合计按完成调用时段回填、并发相加 · UTC+8", "speed-note"));
    container.title = "同图共用时间和 tok/s 刻度：模型色细线为已完成调用的 Output / 总耗时；金色细实线为全 Session 所有 Agent 相加、过去 10 秒平滑的输出。Output 使用 API completion_tokens，不重复加 Thinking。合计按完成调用时段均匀回填，空闲回落为零；进行中的调用未回报用量前不估算。默认最近 1 小时，预设窗口每秒跟随当前时间；手动缩放/平移进入固定的自定义窗口，时间选项隐藏。纵轴随当前窗口内两条曲线自动适配，窗口外峰值不参与；纵轴不一定从零开始，请以刻度为准。";
    if(view.preset)followRange();else boundWindow(view.start,view.end-view.start);
    draw();
    speedContainers.set(container,{view,advance(){
      if(drag)return;
      updateDomain();
      if(view.preset)followRange();
      draw();
    }});
  }

  const advanceSpeed=container=>speedContainers.get(container)?.advance();
  return {outputRate, modelFamily, identifyModel, modelPresentation, configureModels, modelRate, renderSpeed, advanceSpeed, sessionOutputRate};
})();

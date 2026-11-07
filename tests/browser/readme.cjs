// Reproducible documentation screenshots: actual UI renderers, synthetic mail only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function terminalSVG(screen){
  const w=screen.columns*9+32,h=screen.rows*18+32;
  const colors={ansiblack:'#10171b',ansiwhite:'#dedede',ansibrightwhite:'#ffffff',ansired:'#e99797',ansigreen:'#a6dec5',ansiyellow:'#e8c476',ansiblue:'#91bbdd',ansimagenta:'#c7ade1',ansicyan:'#98d9e2',ansigray:'#bbbbbb'};
  const color=(value,fallback)=>!value ? fallback : colors[value] || (/^[0-9a-f]{6}$/i.test(value) ? '#'+value : fallback);
  let svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><rect width="100%" height="100%" fill="#10171b"/>`;
  const backgrounds=[],glyphs=[];
  for(const cell of screen.cells){
    const a=cell.attrs,x=16+cell.x*9,y=16+cell.y*18,width=Math.max(cell.width,1)*9;
    let fg=color(a.color,'#d8e2e5'),bg=color(a.bgcolor,'#10171b');if(a.reverse)[fg,bg]=[bg,fg];
    backgrounds.push(`<rect x="${x}" y="${y}" width="${width}" height="18" fill="${bg}"/>`);
    if(cell.char.trim() && !/[\u0000-\u0008\u000b-\u001f]/.test(cell.char))
      glyphs.push(`<text x="${x}" y="${y+14}" fill="${fg}" font-family="DejaVu Sans Mono,Noto Sans CJK SC,monospace" font-size="14" font-weight="${a.bold?700:400}" font-style="${a.italic?'italic':'normal'}" textLength="${width}" lengthAdjust="spacingAndGlyphs">${escape(cell.char)}</text>`);
  }
  return {svg:svg+backgrounds.join('')+glyphs.join('')+'</svg>',width:w,height:h};
}
(async()=>{
  const temp=testDirectory('recoding-readme-'),images=path.join(project,'docs/images');fs.mkdirSync(images,{recursive:true});
  let server,browser,result;const errors=[];
  try{
    const seed=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-B','-c',String.raw`
import asyncio,json,sys
from pathlib import Path
from types import SimpleNamespace
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from recoding_cli.tui import TerminalUI
from recoding_cli.render import Renderer
from session.store import SessionStore
from session import SavedSession
from multiagent import MultiAgentSystem
from test_teams import Runtime,CONFIG
from test_tui import ModelClient
root=Path(sys.argv[1]);workspace=root/'workspace';workspace.mkdir()
config={**CONFIG,'model':'grok-4.7','max_subagents':16}
store=SessionStore.create(workspace,'并行协作示例-离线展示',config)
master=Runtime(store,config)
team=MultiAgentSystem(store,config,lambda s,c,*_:Runtime(s,c),master.publish,master)
try:
    analyst=team.request('master','create_agent',{'name':'接口检查','icon':'⚙'})['agent_id']
    writer=team.request('master','create_agent',{'name':'文档整理','icon':'✎'})['agent_id']
    team.queue_user('master','请并行检查模型接口与项目文档，告诉我如何开始使用。')
    team.request('master','send_agent_message',{'agent_id':analyst,'message':'你核对模型接口与切换机制，文档 Agent 同步整理启动步骤。请直接沟通，必要时向我报告。'})
    team.request(analyst,'send_agent_message',{'agent_id':writer,'message':'已核对接口约定：Master 与 sub 共用 Session 模型选择，切换在下一次 Model call 生效。请把这个行为写入说明。'})
    fence=chr(96)*3
    team.request('master','send_user_message',{'message':'## 从这里开始\n\n**Web** 适合图文阅读和多 Agent 协作，**CLI / TUI** 适合终端工作流。\n\n'+fence+'bash\n.venv/bin/python3 recoding-web.py --env-file .env -C ./sessions --port 8000\n'+fence+'\n\n后续用消息继续协调；你可以随时 Stop，保留已提交的工作。\n\n*离线示例消息，用于展示界面。*'})
finally:team.pause()
saved=SavedSession(store.folder)
async def capture():
    class Output(DummyOutput):
        def get_size(self):return Size(rows=48,columns=116)
    client=ModelClient(Path('/workspace/example-project'));client.agents=saved.agents();client.items=[{**row,'timestamp':946728000} for row in saved.messages()['messages']];client.settings.update(max_subagents=16)
    with create_pipe_input() as keyboard:
        ui=TerminalUI(client,Renderer(color='always'),SimpleNamespace(),input=keyboard,output=Output())
        ui.status={'name':store.name,'status':'stop','settings':client.settings};ui.agents=client.agents
        ui.renderer.agents={row['id']:row for row in client.agents}
        for row in client.items:ui.conversation.add(row)
        ui.buffer.text='继续核对启动步骤';ui.buffer.cursor_position=len(ui.buffer.text);ui.open_panel('agents')
        task=asyncio.create_task(ui.app.run_async())
        try:
            await asyncio.sleep(.15)
            screen=ui.app.renderer._last_screen;cells=[]
            for y in range(48):
                for x in range(116):
                    cell=screen.data_buffer[y][x]
                    attrs=ui.app._merged_style.get_attrs_for_style_str(cell.style)
                    cells.append({'x':x,'y':y,'char':cell.char,'width':cell.width,'attrs':attrs._asdict()})
            (root/'terminal.json').write_text(json.dumps({'rows':48,'columns':116,'cells':cells},ensure_ascii=False))
        finally:
            ui.app.exit();await task;ui.close()
asyncio.run(capture())
print(json.dumps({'id':store.id,'workspace':str(workspace),'session_directory':str(store.folder)}))
`,temp],{cwd:project,env:environment({PYTHONPATH:path.join(project,'tests'),TZ:'UTC'}),encoding:'utf8'}));
    browser=await launchBrowser();
    const terminal=terminalSVG(JSON.parse(fs.readFileSync(path.join(temp,'terminal.json'),'utf8')));
    fs.writeFileSync(path.join(temp,'terminal.svg'),terminal.svg);
    const terminalPage=await browser.newPage({viewport:{width:terminal.width,height:terminal.height}});
    await terminalPage.setContent('<style>body{margin:0}svg{display:block}</style>'+terminal.svg);
    await terminalPage.screenshot({path:path.join(images,'cli.png')});await terminalPage.close();
    server=startServer(seed.workspace);const base=await server.ready;
    const page=await browser.newPage({viewport:{width:1440,height:1050},timezoneId:'UTC'});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    await page.evaluate(id=>openSession({action:'browse',id}),seed.id);
    await page.waitForFunction(()=>state.agentId==='all'&&allView().loaded&&!state.switching);
    await page.locator('button[data-detail-mode=chat]').click();
    assert.equal(await page.locator('.all-agent-message').count(),4);
    assert.equal(await page.locator('.human-message').isVisible(),true);
    assert.equal(await page.evaluate(()=>state.session.read_only),true,'Screenshots must not Resume the fixture');
    await page.evaluate(()=>{document.querySelector('#conversation-scroll').scrollTop=0;});
    const presentation=await page.evaluate(({paths,home})=>{
      const replacements=paths.sort((a,b)=>b[0].length-a[0].length);
      const portable=value=>replacements.reduce((out,[from,to])=>out.replaceAll(from,to),value)
        .replace(/\b\d{4}\/\d{1,2}\/\d{1,2}\s+\d{2}:\d{2}:\d{2}\b/g,'2000/01/01 12:00:00')
        .replace(/\b\d{2}:\d{2}:\d{2}\b/g,'12:00:00');
      let observer;
      const scrub=()=>{
        observer?.disconnect();
        document.querySelector('#workspace-sync').hidden=true;
        const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
        while(walker.nextNode()){
          const node=walker.currentNode,value=portable(node.nodeValue);
          if(value!==node.nodeValue)node.nodeValue=value;
        }
        for(const node of document.querySelectorAll('[title],[aria-label]')){
          for(const attr of ['title','aria-label'])if(node.hasAttribute(attr))node.setAttribute(attr,portable(node.getAttribute(attr)));
        }
        observer?.observe(document.body,{subtree:true,childList:true,characterData:true});
      };
      observer=new MutationObserver(scrub);scrub();
      return {text:document.body.innerText,privatePaths:paths.map(([value])=>value).concat(home)};
    },{paths:[[seed.session_directory,'/workspace/example-project/.recoding/example-session'],
      [seed.workspace,'/workspace/example-project'],[temp,'/workspace/example-fixture'],[project,'/workspace/recoding-source']],home:os.homedir()});
    for(const privatePath of presentation.privatePaths)assert(!presentation.text.includes(privatePath),'Documentation screenshot contains a deployment path');
    assert(presentation.text.includes('/workspace/example-project'));
    await page.screenshot({path:path.join(images,'web.png')});
    assert.deepEqual(errors,[]);
    result={passed:true,screenshot_source:'Actual UI renderers with synthetic correspondence, fictional paths and fixed example times; not deployment data',
      images:['docs/images/cli.png','docs/images/web.png'],session_directory:seed.session_directory,
      external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});

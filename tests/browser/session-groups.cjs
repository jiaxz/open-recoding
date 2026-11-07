// A directory's own Sessions stay above, and independent of, nested directories.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-session-groups-'),root=path.join(temp,'workspace');
  const parent=path.join(root,'A'),child=path.join(parent,'B');
  const create=(directory,name,pinned=false)=>JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-c',
    'import json,sys; from session import SessionStore; s=SessionStore.create(sys.argv[1],sys.argv[2],{}); s.update_metadata(pinned=sys.argv[3]=="yes"); print(json.dumps(s.navigation_info()))',
    directory,name,pinned?'yes':'no'],{cwd:project,env:environment(),encoding:'utf8'}));
  const here=create(root,'root-session'),a=create(parent,'parent-session',true),b=create(child,'child-session');
  fs.mkdirSync(path.join(parent,'ordinary'),{recursive:true});
  let server,browser,result;const errors=[];
  try{
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1450,height:1000}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded&&!state.switching);
    const folder=directory=>page.locator(`.workspace-group[data-directory="${directory}"]`);
    const group=directory=>folder(directory).locator(':scope > .directory-sessions');
    const own=(directory,id)=>group(directory).locator(`:scope > .session-row[data-id="${id}"]`);
    assert.equal(await own(root,here.id).isVisible(),true);
    assert.equal(await own(parent,a.id).isVisible(),true,'Pinning must not remove the Session from its workspace');
    assert.equal(await own(child,b.id).isVisible(),true,'Nested Sessions and parent Sessions coexist');
    assert.equal(await page.locator(`.pinned-sessions .session-row[data-id="${a.id}"]`).isVisible(),true);
    assert(await group(parent).evaluate(node=>{
      const siblings=[...node.parentElement.children];return siblings.indexOf(node)<siblings.findIndex(item=>item.classList.contains('directory-children'));
    }),'Own Sessions appear before subdirectories');
    assert((await own(parent,a.id).boundingBox()).y<(await folder(child).boundingBox()).y);

    await group(parent).locator(':scope > summary').click();
    assert.equal(await own(parent,a.id).isVisible(),false);
    assert.equal(await own(child,b.id).isVisible(),true,'Collapsing parent Sessions does not collapse child directories');
    await page.evaluate(()=>listSessions());
    assert.equal(await group(parent).getAttribute('open'),null,'Unchanged sync respects a deliberate group collapse');
    const filter=folder(parent).locator(':scope > .non-session-toggle');
    await filter.click();
    assert.equal(await folder(path.join(parent,'ordinary')).isVisible(),true);
    assert.equal(await own(child,b.id).isVisible(),true);
    assert.equal(await group(parent).getAttribute('open'),null,'Non-Session folders and own Sessions have independent toggles');

    const extra=create(parent,'new-parent-session');
    await page.evaluate(()=>listSessions());
    assert.equal(await own(parent,extra.id).isVisible(),true,'A newly discovered own Session reopens that group');
    assert.equal(await own(child,b.id).isVisible(),true);
    await group(parent).locator(':scope > summary').click();
    await group(child).locator(':scope > summary').click();
    await page.locator('#discover-sessions').click();
    await page.waitForFunction(()=>!state.discoveringSessions);
    assert.equal(await own(parent,a.id).isVisible(),true);
    assert.equal(await own(child,b.id).isVisible(),true);
    assert.deepEqual(errors,[]);
    result={passed:true,checks:[
      'Root, parent and child own Sessions appear above nested directories; pinned items remain at their own location',
      'Own Session groups fold independently of child directories and non-Session folder filters',
      'Ordinary sync preserves deliberate folding; new own Sessions reopen only the affected group',
      'Discover all reopens own Session groups as well as their paths'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});

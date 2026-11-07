// Focused SVG delivery, branding layout and document references; no model calls.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {project,testDirectory,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-branding-');
  let server,browser,result;const errors=[];
  try{
    for(const name of ['logo.svg','logomark.svg'])
      assert.deepEqual(fs.readFileSync(path.join(project,name)),fs.readFileSync(path.join(project,'apps/web/recoding_web/static',name)));
    const readme=fs.readFileSync(path.join(project,'README.md'),'utf8');
    const guide=fs.readFileSync(path.join(project,'docs/guide.md'),'utf8');
    const brand=fs.readFileSync(path.join(project,'docs/branding.md'),'utf8');
    for(const name of ['logo.svg','logomark.svg']){
      assert(brand.includes(`src="../${name}"`));
    }
    assert(readme.includes('src="logo.svg"'));assert(guide.includes('src="../logo.svg"'));
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:950}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    for(const name of ['logo.svg','logomark.svg']){
      const response=await page.request.get(`${base}/${name}`);
      assert.equal(response.status(),200);assert.match(response.headers()['content-type'],/^image\/svg\+xml/);
      assert.deepEqual(await response.body(),fs.readFileSync(path.join(project,name)));
    }
    assert.equal(await page.locator('link[rel=icon]').getAttribute('href'),'/logomark.svg');
    assert.equal(await page.locator('#recoding-loop image').getAttribute('href'),'/logomark.svg');
    assert.equal(await page.locator('.brand-mark use').getAttribute('href'),'#recoding-loop');
    assert.equal(await page.locator('#exit-host').count(),0);
    assert.equal(await page.locator('#tool-inventory').isVisible(),true);
    await page.locator('#composer-help-button').click();
    const logo=page.locator('.help-brand-logo');
    await page.waitForFunction(()=>document.querySelector('.help-brand-logo').complete);
    const dimensions=await logo.evaluate(img=>({width:img.naturalWidth,height:img.naturalHeight,rect:img.getBoundingClientRect().toJSON()}));
    assert.equal(dimensions.width,960);assert.equal(dimensions.height,280);
    assert(Math.abs(dimensions.rect.width/dimensions.rect.height-960/280)<.02);
    assert.equal(await page.locator('#composer-help').isVisible(),true);
    await page.screenshot({path:path.join(temp,'branding-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(100);
    assert.equal(await logo.isVisible(),true);
    const rect=await logo.boundingBox();assert(rect.x>=0&&rect.x+rect.width<=390);
    await page.screenshot({path:path.join(temp,'branding-narrow.png')});
    assert.deepEqual(errors,[]);
    result={passed:true,checks:[
      'Root SVG originals and packaged copies are byte-identical; README/guide show the logo and the brand reference shows both graphics',
      'Both images are delivered unchanged over HTTP with SVG MIME types',
      'Sidebar/loop marks and browser favicon use the supplied logomark',
      'Help panel displays the complete logo at its original ratio on desktop and narrow screens',
      'Tool inventory remains visible and the removed Exit application button stays absent'
    ],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});

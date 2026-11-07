// Focused local check: autonomous method selection, retired controls and code rendering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const {testDirectory, startServer, launchBrowser, cleanup, report: writeReport, localTLS, listen} = require('./support.cjs');

(async () => {
  const toolOnly = process.argv.includes('--tool-source-smoke');
  const temp = testDirectory('recoding-rendering-');
  const workspace = path.join(temp, 'workspace');fs.mkdirSync(workspace);
  const requests = [], checks = [], errors = [];
  const python = 'def answer():\n    return 6 * 7\nprint(answer())\n';
  const toolPython = `def solve_velocity_profile(ds_vec, kappa_vec, mu=1.4, g=9.81):
    N = len(ds_vec)
    a_lat_max = mu * g
    v_corner = np.sqrt(a_lat_max / (np.abs(kappa_vec) + 1e-5))
    constant = np.pi
    # np.sqrt(fake) + 999 must stay a comment
    literal = """np.sqrt(fake) + 999
return False # this is text"""
    html = '<img src=x onerror="window.toolSourceUnsafe=true">'
    for iteration in range(5):
        a_lat_max += v_corner[iteration] ** 2
    return f"speed={a_lat_max + 1}"
` + '# padding preserves a complete, large Python source file\n'.repeat(2200)
    + 'def tail_check(value=7):\n    return value + 35\nprint(tail_check())\n';
  const toolBash = 'value=42\nif [ "$value" -ge 1 ]; then\n    printf "%s\\n" "$value" | cat\nfi\n'
    + '# padding preserves complete Bash source and quoted values\n'.repeat(1900)
    + 'printf "%s\\n" "$value"\n';
  const codeText = '## 结果\n\n42。公式 $6 \\times 7=42$。\n\n'
    + '```python\n' + python + '```\n\n'
    + '```javascript\nconst html = \'<img src=x onerror="window.highlightUnsafe = true">\';\nconsole.log(html);\n```\n\n'
    + '```bash\nprintf "%s\\n" "42"\n```\n\n'
    + '```json\n{"answer": 42, "verified": true}\n```\n\n'
    + '```svg\n<svg xmlns="http://www.w3.org/2000/svg"><circle r="10"/></svg>\n```\n\n'
    + '```latex\n\\begin{equation} x^2 + y^2 = 1 \\end{equation}\n```\n\n'
    + '```unknown-fixture-language\nkeep <source> unchanged\n```\n';
  let model, server, browser, releaseFirst, report;
  let firstSeen;const firstRequest = new Promise(resolve => {firstSeen = resolve;});
  try {
    const {cert, options} = localTLS(temp);
    model = https.createServer(options, (req, res) => {
      let body = '';req.on('data', data => {body += data;});
      req.on('end', () => {
        requests.push(JSON.parse(body));const index = requests.length;
        const reply = () => {
          const delta = index === 1 ? {role: 'assistant', content: '先执行两项验证。', tool_calls: [
            {index: 0, id: 'render-python', type: 'function', function: {name: 'python', arguments: JSON.stringify({code: toolOnly ? toolPython : 'print(6 * 7)'})},
              extra_content: {google: {thought_signature: 'retained-signature'}}},
            {index: 1, id: 'render-bash', type: 'function', function: {name: 'bash', arguments: JSON.stringify({command: toolOnly ? toolBash : 'printf verified'})}},
          ]} : {role: 'assistant', content: codeText, reasoning_content: '本地固定摘要：检查结果后作答。'};
          const output = 'data: ' + JSON.stringify({choices: [{index: 0, delta, finish_reason: index === 1 ? 'tool_calls' : 'stop'}]})
            + '\n\ndata: ' + JSON.stringify({choices: [], usage: {prompt_tokens: 100, completion_tokens: 50, total_tokens: 150}})
            + '\n\ndata: [DONE]\n\n';
          res.writeHead(200, {'Content-Type': 'text/event-stream', 'Content-Length': Buffer.byteLength(output)});res.end(output);
        };
        if (index === 1) {releaseFirst = reply;firstSeen();} else reply();
      });
    });
    await listen(model);
    const env = {};
    Object.assign(env, {GEMINI_API_KEY: 'local-rendering-fixture', GEMINI_BASE_URL: `https://localhost:${model.address().port}/v1`,
      SSL_CERT_FILE: cert, NO_PROXY: 'localhost,127.0.0.1', no_proxy: 'localhost,127.0.0.1'});
    server = startServer(workspace, {demo: false, env});
    const base = await server.ready;
    browser = await launchBrowser();
    const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    const ready = () => page.waitForFunction(() => state.session?.status === 'ready' && !state.switching);
    const turn = async prompt => {
      const run_id = await page.evaluate(prompt => api(`/api/sessions/${state.session.id}/turn`, {prompt}), prompt);
      await page.waitForFunction(run_id => state.session?.status === 'ready' && [...state.eventLog.values()].some(event =>
        event.run_id === run_id && ['answer', 'task_error'].includes(event.type)), run_id.run_id);
    };
    const preferenceNotes = request => request.messages.filter(message => message.role === 'user' && typeof message.content === 'string'
      && message.content.startsWith('[Session偏好更新]\n'));
    await page.goto(base, {waitUntil: 'networkidle'});
    await page.locator('#new-session').click();await page.locator('#session-name').fill('自主方法与渲染测试');
    await page.locator('#session-submit').click();await ready();
    assert.equal(await page.locator('[data-preference],.enhancement-bar').count(), 0);
    assert.equal(await page.evaluate(() => "behavior_preferences" in state.config.capabilities), false);
    await page.locator('#prompt').fill('先验证两次，再解释结果');await page.locator('#prompt').press('Enter');
    await Promise.race([firstRequest, new Promise((_, reject) => setTimeout(() => reject(new Error('First model request not received')), 10000))]);
    const system = requests[0].messages[0].content;
    assert.match(system, /Network research can help/);
    assert.match(system, /Drawing, inspecting and refining images can also support visual reasoning/);
    assert.match(system, /Decide independently whether research, illustrated output or a visual reasoning cycle is useful/);
    assert.match(system, /none is required for every task/);
    assert.doesNotMatch(system, /Session偏好更新|Enhancement ON|Enhancement OFF/);
    releaseFirst();
    await page.waitForFunction(() => [...state.eventLog.values()].some(event => event.type === 'answer'));await ready();
    assert.equal(requests.length, 2);
    assert.deepEqual(requests[1].messages.slice(0, requests[0].messages.length), requests[0].messages);
    assert.deepEqual(requests[1].tools, requests[0].tools);
    assert.deepEqual(requests[1].messages.slice(-3).map(message => message.role), ['assistant', 'tool', 'tool']);
    assert.deepEqual(requests[1].messages.slice(-2).map(message => message.tool_call_id), ['render-python', 'render-bash']);
    assert.equal(requests[1].messages.at(-3).tool_calls[0].extra_content.google.thought_signature, 'retained-signature');
    assert.equal(preferenceNotes(requests[1]).length, 0);
    assert(!JSON.stringify(requests).includes('"_preferences"'));
    assert.equal(await page.evaluate(() => state.session.network), false);
    checks.push('No enhancement switches or injected settings; neutral System leaves research, illustrations and visual reasoning to the model; tool pairing/signatures and network permission stay intact');

    const answer = page.locator('.turn').first().locator(':scope > .final-response');
    for (const language of ['python', 'javascript', 'bash', 'json', 'svg', 'latex'])
      assert(await answer.locator(`code.language-${language} span[class*=hljs]`).count() > 0, language);
    assert.equal(await answer.locator('code.language-python').textContent(), python);
    assert.equal(await answer.locator('code.language-unknown-fixture-language').textContent(), 'keep <source> unchanged\n');
    assert.equal(await page.evaluate(() => window.highlightUnsafe), undefined);
    assert.equal(await answer.locator('.katex').count(), 1);
    assert(await page.locator('.tool-code .hljs-built_in').count() > 0);
    await page.evaluate(() => {navigator.clipboard.writeText = async value => {window.testCopiedSource = value;};});
    await answer.locator('.code-block').first().locator('.copy-code').click();
    assert.equal(await page.evaluate(() => window.testCopiedSource), python);
    checks.push('Python/JS/Bash/JSON/SVG/TikZ code colors, original-source copying, safe HTML text, unknown-language fallback and math coexist');

    if (toolOnly) {
      const cards = page.locator('.tool-call'), source = cards.first().locator('.tool-code code');
      assert.equal((await source.textContent()).length, 16000);
      await cards.first().locator(':scope > summary').click();
      await page.waitForFunction(() => document.querySelector('.tool-call .tool-source-note')?.hidden === true);
      assert.equal(await source.textContent(), toolPython);
      const functions = await source.locator('.hljs-title.function_').allTextContents();
      assert(functions.includes('sqrt'));assert(functions.includes('tail_check'));
      assert((await source.locator('.hljs-operator').allTextContents()).includes('**'));
      assert((await source.locator('.hljs-punctuation').allTextContents()).includes('.'));
      assert((await source.locator('.hljs-property').allTextContents()).includes('pi'));
      assert.equal(await source.locator('.hljs-comment .hljs-title,.hljs-comment .hljs-operator').count(), 0);
      assert.equal(await source.locator('.hljs-string:not(:has(.hljs-subst)) .hljs-variable,.hljs-string:not(:has(.hljs-subst)) .hljs-operator').count(), 0);
      assert(await source.locator('.hljs-subst .hljs-operator').count() > 0);
      const visual = await source.evaluate(code => ({font: getComputedStyle(code).fontFamily,
        size: getComputedStyle(code).fontSize, parentSize: getComputedStyle(code.parentElement).fontSize,
        foreground: getComputedStyle(code).color, operator: getComputedStyle(code.querySelector('.hljs-operator')).color}));
      assert.match(visual.font, /monospace/);assert.equal(visual.size, visual.parentSize);assert.notEqual(visual.foreground, visual.operator);
      assert.equal(await page.evaluate(() => window.toolSourceUnsafe), undefined);
      await cards.nth(1).locator(':scope > summary').click();
      await page.waitForFunction(() => document.querySelectorAll('.tool-source-note')[1]?.hidden === true);
      assert.equal(await cards.nth(1).locator('.tool-code code').textContent(), toolBash);
      assert((await cards.nth(1).locator('.hljs-operator').allTextContents()).includes('|'));
      await page.reload({waitUntil: 'networkidle'});await ready();
      await page.locator('.tool-call').first().locator(':scope > summary').click();
      await page.waitForFunction(() => document.querySelector('.tool-source-note')?.hidden === true);
      assert.equal(await page.locator('.tool-call').first().locator('.tool-code code').textContent(), toolPython);
      assert.equal(requests.length, 2);
      checks.push('Full Python/Bash over 100k chars loads from archived source; tail/functions/operators/properties and f-strings are highlighted without coloring comments/strings or changing any source');
    } else {

    for (const viewport of [{width: 1450, height: 1000}, {width: 390, height: 850}]) {
      await page.setViewportSize(viewport);
      await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
      await page.locator('#composer-help-button').click();
      assert.match(await page.locator('#composer-help').innerText(), /Shift\+Enter/);
      assert.doesNotMatch(await page.locator('#composer-help').innerText(), /增强/);
      const help = await page.locator('#composer-help').boundingBox();
      assert(help.x >= 0 && help.y >= 0 && help.x + help.width <= viewport.width && help.y + help.height <= viewport.height);
      await page.keyboard.press('Escape');assert.equal(await page.locator('#composer-help').isVisible(), false);
    }
    checks.push('Composer retains input help and Send, with a fitting help popover on desktop and narrow screens');


    }
    assert.deepEqual(errors, []);
    if (toolOnly && process.env.AGENT_UI_SCREENSHOT) await page.screenshot({path: process.env.AGENT_UI_SCREENSHOT});
    report = {passed: true, checks, errors, external_model_api_calls: 0, full_suite_run: false};
  } finally {
    await cleanup({browser, server, model, temp});
  }
  writeReport(report);
})().catch(error => {console.error(error);process.exitCode = 1;});

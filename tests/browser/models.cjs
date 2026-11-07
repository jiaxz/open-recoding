// One targeted local check: model switching, honest speed labels and history.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const {testDirectory, startServer, launchBrowser, cleanup, report: writeReport, localTLS, listen} = require('./support.cjs');

(async () => {
  const temp = testDirectory('recoding-models-');
  const workspace = path.join(temp, 'workspace');fs.mkdirSync(workspace);
  const requests = [], errors = [], checks = [];
  let model, server, browser, report, releaseFirst, releaseSecond;
  try {
    const {cert, options} = localTLS(temp);
    model = https.createServer(options, (req, res) => {
      let body = '';req.on('data', data => {body += data;});req.on('end', () => {
        requests.push({body: JSON.parse(body), authorization: req.headers.authorization});const index = requests.length;
        const reply = () => {
          let delta = {role: 'assistant', content: '## 完成\n\n42'};
          if (index < 3) delta = {role: 'assistant', content: '运行检查。', reasoning_content: '检查状态后继续。', tool_calls: [
            {index: 0, id: `model-tool-${index}`, type: 'function', function: {name: 'python', arguments: JSON.stringify({code:
              index === 1 ? 'shared_value = 41\nprint(shared_value)' : 'shared_value += 1\nprint(shared_value)'})},
              ...(index === 1 ? {extra_content: {google: {thought_signature: 'original-gemini-signature'}}} : {})}]};
          const usage = index === 4 ? {} : {usage: {prompt_tokens: 100, completion_tokens: 60,
            completion_tokens_details: {reasoning_tokens: 20}, total_tokens: 160}};
          const frame = data => 'data: ' + JSON.stringify(data) + '\n\n';
          const output = frame({model: requests[index - 1].body.model, choices: [{index: 0, delta, finish_reason: index < 3 ? 'tool_calls' : 'stop'}]})
            + frame({choices: [], ...usage}) + 'data: [DONE]\n\n';
          res.writeHead(200, {'Content-Type': 'text/event-stream'});res.end(output);
        };
        if (index === 1) releaseFirst = reply;
        else if (index === 2) releaseSecond = reply;
        else setTimeout(reply, 100);
      });
    });
    await listen(model);
    const env = {};
    Object.assign(env, {GEMINI_API_KEY: 'local-model-fixture', GEMINI_BASE_URL: `https://localhost:${model.address().port}/v1`,
      SSL_CERT_FILE: cert, NO_PROXY: 'localhost,127.0.0.1', no_proxy: 'localhost,127.0.0.1'});
    server = startServer(workspace, {demo: false, env});
    const base = await server.ready;
    browser = await launchBrowser();
    const page = await browser.newPage({viewport: {width: 1450, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    const ready = () => page.waitForFunction(() => state.session?.status === 'ready' && !state.switching);
    const awaitRequest = async check => {
      const deadline = Date.now() + 10000;
      while (!check() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
      assert(check(), 'Expected local model request did not arrive');
    };
    const select = async id => {
      await page.locator('#model-name').selectOption(id);
      await page.waitForFunction(id => state.session.model === id && !modelSaves.has(state.session.id), id);
    };
    await page.goto(base, {waitUntil: 'networkidle'});await page.locator('#new-session').click();
    assert.deepEqual(await page.locator('#session-model option').allTextContents(), ['gemini-4-argon', 'gemini-3.8-flash', 'grok-4.7']);
    assert.equal(await page.locator('#session-model').inputValue(), 'gemini-4-argon');
    await page.locator('#session-model').selectOption('gemini-3.8-flash');
    await page.locator('#session-submit').click();await ready();
    const saved = await page.evaluate(() => ({id: state.session.id, path: state.session.session_directory}));
    await page.locator('#prompt').fill('检查并保留 Python 状态');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(() => document.querySelector('.model-call')?.dataset.status === 'running');
    await awaitRequest(() => releaseFirst);
    assert.equal(await page.locator('.model-tag').first().innerText(), 'gemini-3.8-flash');
    assert.equal(await page.locator('.model-metric').last().locator('strong').innerText(), '—');
    await select('grok-4.7');
    assert.match(await page.locator('#model-mode').innerText(), /当前 gemini.*下一次/);
    assert.equal(requests.length, 1);releaseFirst();
    await page.waitForFunction(() => document.querySelectorAll('.model-call').length === 2);
    await awaitRequest(() => releaseSecond);
    assert.equal(requests[1].body.model, 'grok-4.7');
    assert.equal(JSON.stringify(requests[1].body).includes('original-gemini-signature'), false);
    await select('gemini-3.8-flash');releaseSecond();await ready();
    assert.deepEqual(requests.map(r => r.body.model), ['gemini-3.8-flash', 'grok-4.7', 'gemini-3.8-flash']);
    assert(requests.every(r => r.authorization === 'Bearer local-model-fixture'));
    const history = requests[2].body.messages.filter(m => m.role === 'assistant');
    assert.equal(history[0].tool_calls[0].extra_content.google.thought_signature, 'original-gemini-signature');
    assert.equal(history[1].tool_calls[0].extra_content.google.thought_signature, 'skip_thought_signature_validator');
    assert.equal(JSON.stringify(requests).includes('"_model"'), false);
    assert(requests[2].body.messages.some(m => m.role === 'tool' && m.content.includes('42')));
    assert.deepEqual(await page.locator('.model-tag').allTextContents(), ['gemini-3.8-flash', 'grok-4.7', 'gemini-3.8-flash']);
    await page.locator('.turn-totals-details > summary').click();
    assert.match(await page.locator('.turn-totals-models').innerText(), /gemini-3.8-flash × 2.*grok-4.7 × 1/);
    await page.locator('.turn-totals-details > summary').click();
    const values = await page.evaluate(() => [...state.current.models.values()].map(m => ({elapsed: m.elapsed, speed: m.values.speed.textContent})));
    assert(values.every(value => value.speed === `${(60 / value.elapsed).toFixed(1)} tok/s`));
    assert.equal(await page.locator('.speed-model').count(), 2);
    assert.equal(await page.locator('.speed-point[data-family=grok]').count(), 1);
    assert.equal(await page.locator('.speed-line').first().evaluate(n => getComputedStyle(n).strokeWidth), '0.75px');
    await page.locator('.speed-point[data-family=grok]').hover();assert.match(await page.locator('.speed-detail').innerText(), /grok-4.7/);
    checks.push('Mid-Turn Gemini → Grok → Gemini changes only the next call; shared key, Python state, tool pairing and signatures are preserved; cards/rates identify each model');

    await select('grok-4.7');
    await page.evaluate(() => api(`/api/sessions/${state.session.id}/turn`, {prompt: '没有用量'}));
    await page.waitForFunction(() => state.session.status === 'ready' && document.querySelectorAll('.model-call').length === 4);
    assert.equal(await page.locator('.model-call').last().locator('.model-metric').last().locator('strong').innerText(), '—');
    assert.equal(await page.locator('#session-speed').getAttribute('data-samples'), '3');
    await page.reload({waitUntil: 'networkidle'});await ready();
    assert.equal(await page.locator('#model-name').inputValue(), 'grok-4.7');
    assert.equal(await page.locator('.model-tag').first().innerText(), 'gemini-3.8-flash');
    assert.equal(await page.locator('.model-tag').last().innerText(), 'grok-4.7');
    await page.evaluate(id => api(`/api/sessions/${id}/close`, {}), saved.id);
    await page.reload({waitUntil: 'networkidle'});await ready();
    assert.equal(await page.locator('#model-name').inputValue(), 'grok-4.7');
    assert.equal(await page.locator('.model-tag').nth(1).innerText(), 'grok-4.7');
    assert.equal(requests.length, 4, 'Resume must not repeat API calls');
    const checkpoint = JSON.parse(fs.readFileSync(path.join(saved.path, 'state.json'), 'utf8'));
    assert.equal(checkpoint.messages.find(m => m._model === 'grok-4.7').tool_calls[0].extra_content, undefined, 'Migration marker must not be written into the saved Grok response');
    assert.equal(checkpoint.messages.find(m => m._model === 'gemini-3.8-flash').tool_calls[0].extra_content.google.thought_signature, 'original-gemini-signature');
    checks.push('Missing usage stays unknown; Session choice, original per-call model IDs and immutable source history survive refresh/Resume without replay');
    await page.setViewportSize({width: 390, height: 800});await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    await page.setViewportSize({width: 1450, height: 1000});
    await page.locator('#new-session').click();await page.locator('#session-model').selectOption('grok-4.7');
    await page.locator('#session-submit').click();await ready();
    assert.equal(await page.locator('#model-name').inputValue(), 'grok-4.7');
    if (process.env.AGENT_UI_SCREENSHOT) {
      await page.evaluate(id => openSession({action: 'resume', id}), saved.id);await ready();
      await page.screenshot({path: process.env.AGENT_UI_SCREENSHOT});
    }
    assert.deepEqual(errors, []);
    checks.push('New Sessions can select Grok; compact cards stay within narrow viewports');
    report = {passed: true, checks, errors, full_suite_run: false, external_model_api_calls: 0};
  } finally {
    await cleanup({browser, server, model, temp});
  }
  writeReport(report);
})().catch(error => {console.error(error);process.exitCode = 1;});

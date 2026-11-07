// One local end-to-end check for Model call summaries, including old archives.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const {testDirectory, startServer, launchBrowser, cleanup, report: writeReport, localTLS, listen} = require('./support.cjs');

(async () => {
  const temp = testDirectory('recoding-reasoning-');
  const workspace = path.join(temp, 'workspace'); fs.mkdirSync(workspace);
  const summary = '**验证计划**：先计算 $6 \\times 7$，再核对结果。\n\n'
    + Array.from({length: 12}, (_, i) => `${i + 1}. 检查第 ${i + 1} 项数据，确保步骤可重复且结果正确。`).join('\n')
    + '\n\n摘要结束标记。<script>window.summaryUnsafe = true</script>';
  const finalSummary = '结果已经验证，可以向用户报告 **42**。';
  const requests = [], errors = [], checks = [];
  let model, server, browser, report;
  try {
    const {cert, options} = localTLS(temp);
    model = https.createServer(options, (req, res) => {
      let body = '';
      req.on('data', data => {body += data;});
      req.on('end', () => {
        requests.push(JSON.parse(body));
        const index = requests.length;
        const frame = delta => 'data: ' + JSON.stringify({choices: [{index: 0, delta}]}) + '\n\n';
        let output = frame({role: 'assistant'});
        if (index === 1) {
          output += frame({reasoning_content: summary.slice(0, 80)}) + frame({reasoning_content: summary.slice(80)});
          output += frame({content: '执行 Python 验证。', tool_calls: [{index: 0, id: 'summary-python', type: 'function',
            function: {name: 'python', arguments: JSON.stringify({code: 'print(6 * 7)'})},
            extra_content: {google: {thought_signature: 'opaque-signature-do-not-render'}}}]});
        } else output += frame({...index === 2 ? {reasoning_content: finalSummary} : {}, content: '## 结果\n\n42'});
        output += 'data: ' + JSON.stringify({choices: [{index: 0, delta: {}, finish_reason: index === 1 ? 'tool_calls' : 'stop'}],
          usage: {prompt_tokens: 100, completion_tokens: 30, total_tokens: 130,
            completion_tokens_details: {reasoning_tokens: 20}}}) + '\n\ndata: [DONE]\n\n';
        res.writeHead(200, {'Content-Type': 'text/event-stream', 'Content-Length': Buffer.byteLength(output)});res.end(output);
      });
    });
    await listen(model);
    const env = {};
    Object.assign(env, {GEMINI_API_KEY: 'local-summary-fixture', GEMINI_BASE_URL: `https://localhost:${model.address().port}/v1`,
      SSL_CERT_FILE: cert, NO_PROXY: 'localhost,127.0.0.1', no_proxy: 'localhost,127.0.0.1'});
    server = startServer(workspace, {demo: false, env});
    const base = await server.ready;
    browser = await launchBrowser();
    const page = await browser.newPage({viewport: {width: 1400, height: 950}});
    page.on('pageerror', error => errors.push(error.message));
    const ready = () => page.waitForFunction(() => state.session?.status === 'ready' && !state.switching);
    await page.goto(base, {waitUntil: 'networkidle'});
    await page.locator('#new-session').click();await page.locator('#session-name').fill('思考摘要测试');
    await page.locator('#session-submit').click();await ready();
    await page.locator('#prompt').fill('请计算 6 × 7');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('.model-call[data-status=done]').length === 2);await ready();
    assert.equal(requests.length, 2);
    await page.locator('.turn-toggle').first().click();
    assert.equal(requests[1].messages.find(message => message.role === 'assistant').reasoning_content, summary);
    assert.match(await page.locator('.tool-output').textContent(), /42/);
    const first = page.locator('.model-call').first(), summaryBox = first.locator('.model-summary');
    assert.equal(await summaryBox.isVisible(), true);
    assert.equal(await summaryBox.locator('.model-summary-heading').count(), 0);
    assert.doesNotMatch(await summaryBox.innerText(), /思考摘要/);
    assert.equal(await summaryBox.evaluate(node => node.firstElementChild.classList.contains('model-summary-content')), true);
    assert.equal(await summaryBox.locator('.model-summary-content strong').innerText(), '验证计划');
    assert.equal(await summaryBox.locator('.katex').count(), 1);
    assert.equal(await page.evaluate(() => window.summaryUnsafe), undefined);
    assert.doesNotMatch(await page.locator('#messages').innerText(), /opaque-signature-do-not-render/);
    assert.equal(await first.evaluate(node => [...node.children].findIndex(child => child.classList.contains('model-summary'))
      < [...node.children].findIndex(child => child.classList.contains('model-metrics'))), true);
    checks.push('Real SSE reasoning fields reach the primary summary area; Markdown/math render safely; Python tool succeeds');

    const toggle = summaryBox.locator('.model-summary-toggle');await toggle.waitFor({state: 'visible'});
    const height = await summaryBox.locator('.model-summary-content').evaluate(node => node.clientHeight);
    await toggle.click();assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    assert(await summaryBox.locator('.model-summary-content').evaluate(node => node.clientHeight) > height);
    await toggle.click();assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    assert.match(await page.locator('.model-call').nth(1).locator('.model-summary-content').innerText(), /结果已经验证/);
    await page.setViewportSize({width: 390, height: 800});
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    await page.setViewportSize({width: 1400, height: 950});
    checks.push('Long summaries expand/collapse; final-answer calls also show summaries; narrow layout stays within viewport');

    await page.locator('#prompt').fill('继续');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(() => document.querySelectorAll('.model-call[data-status=done]').length === 3);await ready();
    await page.locator('.turn-toggle').last().click();
    assert.equal(await page.locator('.model-call').nth(2).locator('.model-summary').isVisible(), false);
    const emptyStates = await page.evaluate(() => {
      const view = [...state.views.values()].at(-1), model = [...view.models.values()].at(-1);
      const original = {status: model.status, reasoningContent: model.reasoningContent}, checks = [];
      for (const status of ['running', 'done', 'error', 'cancelled']) {
        for (const reasoningContent of [undefined, null, '', ' \n\t ']) {
          Object.assign(model, {status, reasoningContent});renderModel(model);
          checks.push(model.summary.hidden && model.summary.getClientRects().length === 0 && model.summaryContent.textContent === '');
        }
      }
      model.reasoningContent = '实际摘要';renderModel(model);
      checks.push(!model.summary.hidden && model.summaryContent.textContent.includes('实际摘要'));
      Object.assign(model, original);renderModel(model);
      return checks;
    });
    assert(emptyStates.every(Boolean), 'Empty summaries must take no space in any call state; real summaries must remain visible');
    const saved = await page.evaluate(() => ({id: state.session.id, path: state.session.session_directory}));
    await page.reload({waitUntil: 'networkidle'});await ready();
    assert.match(await page.locator('.model-summary-content').first().innerText(), /验证计划/);
    checks.push('Empty summaries are hidden during running/done/error/cancelled states; real summaries remain visible and survive reload');

    if (process.env.AGENT_UI_SCREENSHOT) {
      await first.locator('.model-summary-toggle').click();
      await page.screenshot({path: process.env.AGENT_UI_SCREENSHOT});
    }
    assert.deepEqual(errors, []);
    report = {passed: true, checks, errors, full_suite_run: false, external_model_api_calls: 0};
  } finally {
    await cleanup({browser, server, model, temp});
  }
  writeReport(report);
})().catch(error => {console.error(error);process.exitCode = 1;});

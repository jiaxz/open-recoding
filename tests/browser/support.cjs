// Shared local test lifecycle. Fixtures never load the project's .env or use a fixed port.
const fs = require('node:fs');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');

const project = path.resolve(__dirname, '../..');
const testSessionRoot = path.join(project, 'sessions', 'tests');
let currentTestDirectory;
const testOwnerPid = Number(process.env.RECODING_TEST_OWNER_PID || process.pid);
process.env.RECODING_TEST_OWNER_PID = String(testOwnerPid);
process.on('exit', () => {
  if (process.pid !== testOwnerPid) return;
  try {
    execFileSync(path.join(project, '.venv', 'bin', 'python3'), [path.join(project, 'tests', 'workspace_cleanup.py')],
      {cwd: project, stdio: ['ignore', 'ignore', 'pipe'], timeout: 120000});
  } catch (error) {
    console.error('Test data cleanup failed:', error.stderr?.toString() || error.message);
    process.exitCode = 1;
  }
});

function testDirectory(prefix = 'recoding-browser-') {
  fs.mkdirSync(testSessionRoot, {recursive: true});
  currentTestDirectory = fs.mkdtempSync(path.join(testSessionRoot, prefix));
  return currentTestDirectory;
}

function environment(overrides = {}) {
  const env = {...process.env};
  for (const name of Object.keys(env)) if (name.startsWith('GEMINI_') || ['RECODING_API_KEY','RECODING_BASE_URL','RECODING_MODEL'].includes(name)) delete env[name];
  return {...env, PATH: path.join(project, '.venv', 'bin') + path.delimiter + (env.PATH || ''),
    PYTHONDONTWRITEBYTECODE: '1', RECODING_HOME: path.join(currentTestDirectory || testDirectory('recoding-browser-home-'), '.recoding'), ...overrides};
}

function startServer(workspace, {demo = true, env = {}, port = 0, network = false, gpu = false} = {}) {
  const child = spawn(path.join(project, '.venv', 'bin', 'python3'), ['recoding-web.py', ...(demo ? ['--demo'] : []),
    '--env-file', path.join(workspace, '.unused-test-env'), ...(network ? [] : ['--no-network']), ...(gpu ? [] : ['--no-gpu']),
    '-C', workspace, '--port', String(port)], {cwd: project, env: environment(env), stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = '';
  child.stderr.on('data', data => {stderr += data;});
  const ready = new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => finish(new Error('Server startup timed out: ' + stderr)), 10000);
    const fail = error => finish(error);
    const exited = (code, signal) => finish(new Error(`Server exited: ${code ?? signal} ${stderr}`));
    const read = data => {
      output += data;
      const match = output.match(/Web GUI: (http:\/\/127\.0\.0\.1:\d+)/);
      if (match) finish(null, match[1]);
    };
    function finish(error, url) {
      clearTimeout(timer);
      child.off('error', fail);child.off('exit', exited);child.stdout.off('data', read);
      if (error) reject(error);else resolve(url);
    }
    child.once('error', fail);child.once('exit', exited);child.stdout.on('data', read);
  });
  return {process: child, ready};
}

function launchBrowser() {
  const {chromium} = require('playwright');
  return chromium.launch({headless: true, args: ['--no-sandbox'],
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? {executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE} : {})});
}

function localTLS(temp) {
  const key = path.join(temp, 'key.pem'), cert = path.join(temp, 'cert.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
    '-out', cert, '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], {stdio: 'ignore'});
  return {cert, options: {key: fs.readFileSync(key), cert: fs.readFileSync(cert)}};
}

function listen(model) {
  return new Promise((resolve, reject) => {
    model.once('error', reject);
    model.listen(0, '127.0.0.1', () => {model.off('error', reject);resolve();});
  });
}

async function stopServer(server) {
  const child = server?.process;
  if (!child || child.exitCode !== null || child.signalCode !== null || !child.pid) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => child.kill('SIGKILL'), 12000);
    child.once('exit', () => {clearTimeout(timer);resolve();});
    child.kill('SIGINT');
  });
}

async function cleanup({browser, server, model}) {
  const errors = [];
  for (const close of [() => browser?.close(), () => stopServer(server), () => model && new Promise(resolve => {
    model.close(resolve);model.closeAllConnections?.();
  })]) {
    try {await close();} catch (error) {errors.push(error);}
  }
  if (errors.length) throw errors[0];
}

function report(result) {
  const data = JSON.stringify({...result, ...(currentTestDirectory ? {test_directory: currentTestDirectory} : {})}, null, 2);
  if(currentTestDirectory)fs.writeFileSync(path.join(currentTestDirectory, 'test-report.json'), data + '\n');
  if (process.env.AGENT_UI_TEST_REPORT) fs.writeFileSync(process.env.AGENT_UI_TEST_REPORT, data + '\n');
  console.log(data);
}

module.exports = {project, testSessionRoot, testDirectory, environment, startServer, stopServer, launchBrowser, localTLS, listen, cleanup, report};

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

// Test-generated fake executables never contact Docker, GHCR or a Box.
function environment(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'web-terminal-script-test-'));
  const log = path.join(dir, 'calls');
  fs.writeFileSync(path.join(dir, 'docker'), `#!/bin/bash
printf '%s\\n' "$*" >> "$MOCK_LOG"
case "$*" in
  *'--format {{.Os}}/{{.Architecture}}'*) echo linux/amd64 ;;
  'run -d '*) echo owned-test-container ;;
  'port '*) echo 127.0.0.1:32123 ;;
esac
`, { mode: 0o755 });
  fs.writeFileSync(path.join(dir, 'curl'), '#!/bin/bash\nif [[ "${MOCK_CURL_FAIL:-}" == 1 ]]; then exit 22; fi\necho "Box Terminal"\n', { mode: 0o755 });
  fs.writeFileSync(path.join(dir, 'sleep'), '#!/bin/bash\nexit 0\n', { mode: 0o755 });
  // Temp files only; cleanup is limited to this test-created directory.
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return { dir, log, env: { ...process.env, PATH: dir + ':' + process.env.PATH, MOCK_LOG: log,
    LOCAL_IMAGE: 'test-local:0.1.0', GHCR_IMAGE: 'ghcr.io/example/web-terminal', IMAGE_VERSION: '0.1.0', PLATFORM: 'linux/amd64' } };
}
function run(script, ctx, extra = {}) {
  return spawnSync('bash', [path.join(__dirname, script)], { cwd: ctx.dir, env: { ...ctx.env, ...extra }, encoding: 'utf8' });
}
test('build uses its own source directory, loads locally and never pushes', t => {
  const ctx = environment(t), result = run('build_web_terminal_image.sh', ctx);
  assert.equal(result.status, 0, result.stderr);
  const log = fs.readFileSync(ctx.log, 'utf8');
  assert.match(log, /--load/); assert.ok(log.includes(__dirname)); assert.doesNotMatch(log, /--push/);
});
test('unsupported architecture fails before Docker is called', t => {
  const ctx = environment(t), result = run('build_web_terminal_image.sh', ctx, { PLATFORM: 'linux/arm64' });
  assert.equal(result.status, 2); assert.equal(fs.existsSync(ctx.log), false);
});
test('successful smoke uses loopback, entrypoint override and cleans its own container', t => {
  const ctx = environment(t), result = run('test_web_terminal_image.sh', ctx);
  assert.equal(result.status, 0, result.stderr);
  const log = fs.readFileSync(ctx.log, 'utf8');
  assert.match(log, /--entrypoint \/bin\/bash/); assert.match(log, /127\.0\.0\.1::3001/);
  assert.match(log, /stop owned-test-container/);
});
test('failed smoke stops owned container and prevents pushing', t => {
  const ctx = environment(t), result = run('push_web_terminal_image.sh', ctx, { MOCK_CURL_FAIL: '1' });
  assert.notEqual(result.status, 0);
  const log = fs.readFileSync(ctx.log, 'utf8');
  assert.match(log, /stop owned-test-container/); assert.doesNotMatch(log, /^push /m);
});
test('push tags the tested local image and never rebuilds', t => {
  const ctx = environment(t), result = run('push_web_terminal_image.sh', ctx);
  assert.equal(result.status, 0, result.stderr);
  const log = fs.readFileSync(ctx.log, 'utf8');
  assert.match(log, /tag test-local:0.1.0 ghcr.io\/example\/web-terminal:0.1.0/);
  assert.match(log, /^push ghcr.io\/example\/web-terminal:0.1.0/m); assert.doesNotMatch(log, /buildx build/);
});

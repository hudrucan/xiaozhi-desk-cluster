import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

const { clusterLinuxCommand } = await import(pathToFileURL(join(process.env.BESZEL_UI_SOURCE, 'internal/site/src/lib/agent-install.ts')));
const keys = ['ssh-ed25519 AAAA', 'ssh-ed25519 BBBB', 'ssh-ed25519 CCCC'];

function fixture(run) {
  const root = mkdtempSync(join(tmpdir(), 'beszel-command-test-'));
  try {
    mkdirSync(join(root, 'bin'));
    const stub = join(root, 'stub.cjs');
    writeFileSync(stub, `#!${process.execPath}
const fs = require('node:fs'), path = require('node:path'), cp = require('node:child_process');
const cmd = path.basename(process.argv[1]), args = process.argv.slice(2), root = process.env.FIXTURE_ROOT;
fs.appendFileSync(path.join(root,'calls'),JSON.stringify([cmd,...args])+'\\n');
if (process.env.FAIL_CMD===cmd) process.exit(1);
if (cmd==='id') console.log(process.env.FIXTURE_UID);
if (cmd==='sudo') process.exit(cp.spawnSync(args[0],args.slice(1),{stdio:'inherit',env:{...process.env,FIXTURE_UID:'0'}}).status ?? 1);
// The downloaded file is not executable. Its interpreter must already run as root,
// otherwise the vendor script re-execs the unexecutable file through sudo.
if (cmd==='bash' && process.env.FIXTURE_UID!=='0') process.exit(99);
if (cmd==='install') for (const dir of args.filter(x=>x.startsWith('/'))) fs.mkdirSync(root+dir,{recursive:true});
if (cmd==='tee') fs.writeFileSync(root+args[0],fs.readFileSync(0));
`, { mode: 0o755 });
    for (const cmd of ['id', 'curl', 'bash', 'sudo', 'install', 'tee', 'chmod', 'systemctl']) symlinkSync(stub, join(root, 'bin', cmd));
    run(root);
  } finally { rmSync(root, { recursive: true, force: true }); }
}

for (const uid of ['0', '1000']) {
  test(`generated command configures all three keys for uid ${uid} and preserves quoted arguments`, () => fixture(root => {
    const token = `token' $(touch ${root}/injected)`;
    const url = "http://192.168.1.186/monitor?value='quoted'&next=$HOME";
    const command = clusterLinuxCommand('/tmp/agent socket', keys[0], token, url, keys, true);
    const result = spawnSync('/bin/bash', ['-c', command], {
      env: { ...process.env, PATH: join(root, 'bin') + ':/usr/bin:/bin', FIXTURE_ROOT: root, FIXTURE_UID: uid }, encoding: 'utf8'
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(readFileSync(root + '/etc/beszel-agent/hub-keys.pub', 'utf8'), keys.join('\n') + '\n');
    const override = readFileSync(root + '/etc/systemd/system/beszel-agent.service.d/cluster-keys.conf', 'utf8');
    assert.ok(override.includes('Environment="KEY="\n'));
    assert.ok(override.includes('Environment="BESZEL_AGENT_KEY="\n'));
    assert.ok(override.includes('Environment="KEY_FILE=/etc/beszel-agent/hub-keys.pub"\n'));
    const calls = readFileSync(join(root, 'calls'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.deepEqual(calls.find(x => x[0] === 'bash'), ['bash', '/tmp/install-agent.sh', '-p', '/tmp/agent socket', '-k', keys[0], '-t', token, '-url', url, '--china-mirrors']);
    assert.equal(existsSync(join(root, 'injected')), false);
    assert.equal(calls.some(x => x[0] === 'sudo'), uid !== '0');
    assert.deepEqual(calls.filter(x => x[0] === 'systemctl').map(x => x.slice(1)), [['daemon-reload'], ['restart', 'beszel-agent'], ['is-active', '--quiet', 'beszel-agent']]);
    assert.match(result.stdout, /all 3 cluster hubs/);
  }));
}

test('missing, duplicate, unrelated or malformed cluster keys never yield a single-hub command', () => {
  for (const bad of [[], keys.slice(0, 2), [keys[0], keys[0], keys[2]], keys.map(k => k + 'X'), [keys[0], keys[1], "ssh-ed25519 bad'; touch /tmp/injected"]]) {
    assert.throws(() => clusterLinuxCommand('45876', keys[0], 'token', 'http://hub/monitor', bad), /Three valid/);
  }
});

test('an installer failure stops setup and never reports success', () => fixture(root => {
  const result = spawnSync('/bin/bash', ['-c', clusterLinuxCommand('45876', keys[0], 'token', 'http://hub/monitor', keys)], {
    env: { ...process.env, PATH: join(root, 'bin') + ':/usr/bin:/bin', FIXTURE_ROOT: root, FIXTURE_UID: '0', FAIL_CMD: 'bash' }, encoding: 'utf8'
  });
  assert.notEqual(result.status, 0);
  assert.equal(existsSync(root + '/etc/beszel-agent/hub-keys.pub'), false);
  assert.doesNotMatch(result.stdout, /configured/);
}));

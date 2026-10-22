import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(path.join(process.env.NTFY_WEB_SOURCE, 'web/src/app/Api.js'), 'utf8');
const adminSource = fs.readFileSync(path.join(process.env.NTFY_WEB_SOURCE, 'web/src/components/ServerUsersApi.js'), 'utf8');
test('server administration uses authenticated same-origin requests and never retries an ambiguous Save', async () => {
  const requests = [];
  let response = {ok: true, json: async () => ({revision: 7, users: [], actor: 'admin'})};
  const context = vm.createContext({TextEncoder, AbortSignal, btoa,
    fetch: async (...args) => { requests.push(args); if (response instanceof Error) throw response; return response; },
  });
  vm.runInContext(adminSource.replace('export async function', 'async function') + '\nglobalThis.request = serverUsersRequest;', context);
  const credential = {username: 'admin', password: 'mật-khẩu'};
  assert.equal((await context.request(credential)).revision, 7);
  const [url, options] = requests[0];
  assert.equal(url, '/v1/admin/users');
  assert.equal(Buffer.from(options.headers.Authorization.slice(6), 'base64').toString(), 'admin:mật-khẩu');
  assert.equal(options.cache, 'no-store'); assert.equal(options.credentials, 'omit'); assert.equal(options.redirect, 'error');
  await context.request(credential, '/iphone/password', 'PUT', {password: 'new'});
  assert.equal(requests[1][0], '/v1/admin/users/iphone/password');
  assert.equal(requests[1][1].body, '{"password":"new"}');
  for (const status of [401, 403, 503]) {
    response = {ok: false, status};
    const before = requests.length;
    await assert.rejects(context.request(credential, '/iphone/token', 'POST', {label: 'Phone'}));
    assert.equal(requests.length, before + 1);
  }
  response = new Error('connection lost');
  const before = requests.length;
  await assert.rejects(context.request(credential, '/iphone/token', 'POST', {}), /may have applied/);
  assert.equal(requests.length, before + 1);
});
test('official quick/test publisher uses native topic API, Unicode metadata and existing credentials', async () => {
  const sent = [];
  const context = vm.createContext({URL, console: {log() {}},
    userManager: {get: async () => ({token: 'test-only'})},
    topicUrl: (base, topic) => `${base}/${topic}`,
    maybeWithAuth: (headers, user) => ({...headers, Authorization: `Bearer ${user.token}`}),
    fetchOrThrow: async (...args) => sent.push(args),
  });
  vm.runInContext(source.slice(source.indexOf('class Api {'), source.indexOf('const api =')) + '\nglobalThis.api = new Api();', context);
  await context.api.publish('http://192.168.1.186:2586', 'desk-events', 'Xin chào', {title: 'Thông báo', priority: 4, tags: ['warning', 'computer']});
  const [url, request] = sent[0], parsed = new URL(url);
  assert.equal(parsed.pathname, '/desk-events');
  assert.equal(parsed.searchParams.get('title'), 'Thông báo');
  assert.equal(parsed.searchParams.get('priority'), '4');
  assert.equal(parsed.searchParams.get('tags'), 'warning,computer');
  assert.equal(request.method, 'PUT');assert.equal(request.body, 'Xin chào');
  assert.equal(request.headers.Authorization, 'Bearer test-only');
  await context.api.publish('http://192.168.1.186:2586', 'desk-events', 'Quick message');
  assert.equal(sent[1][0], 'http://192.168.1.186:2586/desk-events');
});

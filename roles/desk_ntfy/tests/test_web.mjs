import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import test from 'node:test';

const source = fs.readFileSync(path.join(process.env.NTFY_WEB_SOURCE, 'web/src/app/Api.js'), 'utf8');
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

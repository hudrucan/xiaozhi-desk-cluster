const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../files/policy_wait.js'), 'utf8');
function fixture(states, pending = true) {
  let mutations = 0, polls = 0;
  const initial = () => new Response(JSON.stringify({updated: 0}), {
    status: pending ? 202 : 200, headers: pending ? {
      'X-Desk-DNS-Operation': 'fixture-operation', 'X-Desk-DNS-State': 'pending'
    } : {}
  });
  const context = {window: {}, document: {baseURI:'http://192.168.1.186/dns/'},
    URL, Headers, Response, DOMException, Date, setTimeout: callback => callback()};
  context.window.fetch = async (input) => {
    if (String(input).endsWith('/desk_policy')) {
      const state = states[Math.min(polls++, states.length - 1)];
      return new Response(JSON.stringify(state), {status:200});
    }
    mutations++;
    return initial();
  };
  vm.runInNewContext(source, context);
  return {fetch:context.window.fetch, counts:()=>({mutations,polls})};
}
const complete = {operation_id:'fixture-operation', operation:'idle', ready:['one','two','three'],
  revision:4, operation_status:200, operation_result:{updated:2}};
test('pending native refresh waits for actual count without repeating the mutation', async () => {
  const f = fixture([{...complete, operation:'running'}, complete]);
  const response = await f.fetch('control/filtering/refresh', {method:'POST', body:'{}'});
  assert.equal(response.status,200);
  assert.deepEqual(await response.json(), {updated:2});
  assert.equal(response.headers.get('X-Desk-DNS-Coverage'), '3/3');
  assert.deepEqual(f.counts(), {mutations:1, polls:2});
});
test('ordinary native responses pass through without policy polling', async () => {
  const f = fixture([],false);
  assert.equal((await f.fetch('control/stats')).status,200);
  assert.deepEqual(f.counts(), {mutations:1,polls:0});
});
test('async native rejection becomes the original UI error status', async () => {
  const f = fixture([{...complete,operation:'failed',error:'policy_operation_failed',operation_status:409}]);
  const response = await f.fetch('control/filtering/add_url',{method:'POST'});
  assert.equal(response.status,409);
  assert.deepEqual(await response.json(),{error:'policy_operation_failed'});
});
test('a new operation never substitutes its result for an earlier client', async () => {
  const f = fixture([{...complete,operation_id:'another-operation'}]);
  await assert.rejects(f.fetch('control/filtering/refresh',{method:'POST'}),/operation changed/);
});
test('caller abort stops polling without cancelling the server job', async () => {
  const f=fixture([complete]);const controller=new AbortController();controller.abort();
  await assert.rejects(f.fetch('control/filtering/refresh',{method:'POST',signal:controller.signal}),{name:'AbortError'});
  assert.deepEqual(f.counts(),{mutations:1,polls:0});
});
test('partial native apply retains its actual coverage and accepted status', async () => {
  const f=fixture([{...complete,ready:['one','two'],operation_status:202}]);
  const response=await f.fetch('control/filtering/refresh',{method:'POST'});
  assert.equal(response.status,202);assert.equal(response.headers.get('X-Desk-DNS-Coverage'),'2/3');
  assert.deepEqual(await response.json(),{updated:2});
});

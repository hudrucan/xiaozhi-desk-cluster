// Uses the official frontend's existing Playwright dependency; all APIs are local fixtures.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import test from 'node:test';

const web = path.join(process.env.NTFY_WEB_SOURCE, 'web');
const {chromium} = await import(pathToFileURL(path.join(web, 'node_modules/@playwright/test/index.mjs')));

test('Separate Server users page manages accounts, keeps passwords ephemeral, and fences ambiguous Saves', async () => {
  let revision = 6, adminPassword = 'admin-test-password';
  const users = [{username: 'admin', role: 'admin', managed: false, access: [], tokens: []},
    {username: 'desk-panel', role: 'user', managed: true, access: [{topic: 'desk-events', read: true, write: false}], tokens: []}];
  const writes = [];
  let ambiguous = false;
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const reply = (code, value) => { res.writeHead(code, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}); res.end(JSON.stringify(value)); };
    if (url.pathname.startsWith('/v1/admin/users')) {
      if (req.headers.authorization !== `Basic ${Buffer.from(`admin:${adminPassword}`).toString('base64')}`) return reply(401, {});
      if (req.method === 'GET') return reply(200, {revision, actor: 'admin', users});
      let raw = ''; for await (const chunk of req) raw += chunk;
      const body = JSON.parse(raw), parts = url.pathname.split('/'), user = users.find((u) => u.username === parts[4]);
      writes.push({method: req.method, path: url.pathname, body});
      if (parts.length === 4) users.push({username: body.username, role: 'user', managed: false, access: [], tokens: []});
      else if (parts[5] === 'password' && user.username === 'admin') adminPassword = body.password;
      else if (parts[5] === 'access') user.access = [{...body}];
      else if (parts[5] === 'token' && req.method === 'POST') user.tokens.push({id: 'safe-revocation-handle', label: body.label, expires: null});
      else if (parts[5] === 'token') user.tokens = [];
      revision++;
      if (ambiguous) { ambiguous = false; return reply(503, {}); }
      return reply(200, {revision, ...(parts[5] === 'token' && req.method === 'POST' ? {token: 'new-test-token'} : {})});
    }
    if (url.pathname.startsWith('/v1/')) return reply(404, {});
    const root = path.join(web, 'build');
    let file = path.resolve(root, `.${decodeURIComponent(url.pathname)}`);
    if (!file.startsWith(`${root}/`)) file = path.join(root, 'index.html');
    try { if (!(await fs.stat(file)).isFile()) file = path.join(root, 'index.html'); }
    catch { file = path.join(root, 'index.html'); }
    res.setHeader('Content-Type', ({'.js':'application/javascript', '.css':'text/css', '.html':'text/html', '.json':'application/json'})[path.extname(file)] || 'application/octet-stream');
    res.end(await fs.readFile(file));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, ...(process.env.NTFY_TEST_BROWSER_CHANNEL ? {channel: process.env.NTFY_TEST_BROWSER_CHANNEL} : {})});
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/settings`);
    const card = page.locator('[aria-label="Server users"]');
    await page.getByRole('button', {name:'Server users', exact:true}).waitFor();
    assert.equal(await card.count(), 0);
    await page.getByRole('button', {name:'Server users', exact:true}).click();
    assert.equal(new URL(page.url()).pathname, '/server-users');
    await card.waitFor({state:'attached', timeout:5000}).catch(async () => {
      throw new Error(`Server users did not render: ${JSON.stringify(errors)}; ${await page.locator('body').innerText()}`);
    });
    const login = async (password) => {
      await card.getByLabel(/^Administrator password\s*\*?$/).fill(password).catch(async () => {
        throw new Error(`Login form labels: ${await card.locator('input,label').evaluateAll((nodes) => nodes.map((n) => `${n.tagName}:${n.id}:${n.getAttribute('for')}:${n.textContent}`).join('; '))}`);
      });
      await card.getByRole('button', {name:'Load server users', exact:true}).click();
    };
    await login('wrong');
    await card.getByText('Incorrect or expired administrator credentials.').waitFor();
    await login(adminPassword);
    await card.getByText('Signed in as admin · Revision 6').waitFor();
    const save = async () => {
      await page.getByRole('dialog').getByRole('button', {name:'Save to cluster', exact:true}).click();
      await page.getByRole('dialog').waitFor({state:'hidden'});
    };
    await card.getByRole('button', {name:'Create server user', exact:true}).click();
    await page.getByRole('dialog').getByLabel(/^Username\s*\*?$/).fill('iphone');
    await page.getByLabel(/^New password\s*\*?$/).fill('phone-test-password');
    await page.getByLabel(/^Confirm new password\s*\*?$/).fill('mismatch');
    assert.equal(await page.getByRole('button', {name:'Save to cluster', exact:true}).isDisabled(), true);
    await page.getByLabel(/^Confirm new password\s*\*?$/).fill('phone-test-password');
    await save();
    await card.getByRole('button', {name:'Manage iphone', exact:true}).click();
    const phone = card.getByRole('region', {name:'iphone account', exact:true});
    await phone.getByRole('button', {name:'Set topic access', exact:true}).click();
    await page.getByRole('dialog').getByLabel(/^Topic\s*\*?$/).fill('desk-events');
    await save();
    assert.deepEqual(writes.at(-1).body, {topic:'desk-events', read:true, write:false});
    if (process.env.NTFY_UI_SCREENSHOTS) {
      await fs.mkdir(process.env.NTFY_UI_SCREENSHOTS, {recursive:true});
      await page.screenshot({path:path.join(process.env.NTFY_UI_SCREENSHOTS,'desktop.png'),fullPage:true});
      await page.setViewportSize({width:390,height:844});
      await page.getByLabel('Select user', {exact:true}).waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),true);
      await page.screenshot({path:path.join(process.env.NTFY_UI_SCREENSHOTS,'mobile.png'),fullPage:true});
      await page.setViewportSize({width:1280,height:720});
      await card.getByRole('button', {name:'Manage iphone', exact:true}).waitFor();
    }
    await phone.getByRole('button', {name:'Change password', exact:true}).click();
    await page.getByLabel(/^New password\s*\*?$/).fill('changed-phone-password');
    await page.getByLabel(/^Confirm new password\s*\*?$/).fill('changed-phone-password');
    await save();
    assert.equal(writes.at(-1).path, '/v1/admin/users/iphone/password');
    assert.equal(writes.at(-1).method, 'PUT');
    assert.equal(await page.getByLabel(/^New password\s*\*?$/).count(), 0);
    await phone.getByRole('button', {name:'Issue token', exact:true}).click();
    await page.getByLabel(/^Token label\s*\*?$/).fill('Phone');
    ambiguous = true;
    const before = writes.length;
    await save();
    await card.getByText(/Quorum did not acknowledge/).waitFor();
    assert.equal(writes.length, before + 1);
    assert.equal(await card.getByRole('button', {name:'Create server user', exact:true}).count(), 0);
    await card.getByRole('button', {name:'Refresh server users', exact:true}).click();
    await phone.getByRole('button', {name:'Revoke token', exact:true}).click();
    await save();
    assert.equal(writes.at(-1).path, '/v1/admin/users/iphone/token/safe-revocation-handle');
    await phone.getByRole('button', {name:'Issue token', exact:true}).click();
    await page.getByLabel(/^Token label\s*\*?$/).fill('Phone');
    await save();
    await card.getByText('new-test-token', {exact:true}).waitFor();
    await card.getByRole('button', {name:'Refresh server users', exact:true}).click();
    await phone.getByRole('button', {name:'Revoke token', exact:true}).waitFor();
    assert.equal(await card.getByText('new-test-token', {exact:true}).count(), 0);
    await card.getByRole('button', {name:'Manage admin', exact:true}).click();
    const admin = card.getByRole('region', {name:'admin account', exact:true});
    await admin.getByRole('button', {name:'Change password', exact:true}).click();
    await page.getByLabel(/^New password\s*\*?$/).fill('changed-admin-password');
    await page.getByLabel(/^Confirm new password\s*\*?$/).fill('changed-admin-password');
    await save();
    await card.getByRole('button', {name:'Create server user', exact:true}).waitFor();
    await admin.getByRole('button', {name:'Change password', exact:true}).click();
    await page.getByLabel(/^New password\s*\*?$/).fill('ambiguous-admin-password');
    await page.getByLabel(/^Confirm new password\s*\*?$/).fill('ambiguous-admin-password');
    ambiguous = true;
    await save();
    await card.getByLabel(/^Administrator password\s*\*?$/).waitFor();
    assert.equal(await card.getByLabel(/^Administrator password\s*\*?$/).inputValue(), '');
    await login('ambiguous-admin-password');
    await card.getByRole('button', {name:'Create server user', exact:true}).waitFor();
    await page.getByRole('button', {name:'Settings', exact:true}).click();
    await card.waitFor({state:'detached'});
    assert.equal(await card.count(), 0);
    await page.getByRole('button', {name:'Server users', exact:true}).click();
    assert.equal(await card.getByLabel(/^Administrator password\s*\*?$/).inputValue(), '');
    await login('ambiguous-admin-password');
    await card.getByRole('button', {name:'Create server user', exact:true}).waitFor();
    await card.getByRole('button', {name:'Lock administration', exact:true}).click();
    assert.equal(await card.getByLabel(/^Administrator password\s*\*?$/).inputValue(), '');
    await page.reload();
    assert.equal(await card.getByLabel(/^Administrator password\s*\*?$/).inputValue(), '');
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await new Promise((resolve) => server.close(resolve));
  }
});

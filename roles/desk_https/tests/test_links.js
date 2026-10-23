const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),path=require('node:path');
const root=path.resolve(__dirname,'../../..');
const source=fs.readFileSync(path.join(root,'roles/desk_ui/templates/index.html.j2'),'utf8');
const code=source.slice(source.indexOf('function httpsServiceLinks(){'),source.indexOf('function renderClient(){'));
for(const protocol of ['http:','https:']){
 const links=[{href:'http://192.168.1.186:8123/'},{href:'http://192.168.1.186:2586/'},{href:'http://192.168.1.128:8080/'}];
 const location={protocol,hostname:protocol==='https:'?'cluster.huynm.dev':'192.168.1.186',origin:protocol+'//cluster.huynm.dev'};
 const ctx=vm.createContext({location,URL,document:{querySelectorAll:()=>links}});
 vm.runInContext(code,ctx);ctx.httpsServiceLinks();
 assert.equal(links[0].href,protocol==='https:'?'https://ha.cluster.huynm.dev/':'http://192.168.1.186:8123/');
 assert.equal(links[1].href,protocol==='https:'?'https://ntfy.cluster.huynm.dev/':'http://192.168.1.186:2586/');
 assert.equal(links[2].href,'http://192.168.1.128:8080/');
}
const patch=fs.readFileSync(path.join(root,'roles/desk_monitoring/files/beszel-ui.patch'),'utf8');
const added=patch.slice(patch.lastIndexOf('diff --git a/internal/site/src/lib/utils.ts')).split('\n').filter(l=>l.startsWith('+')&&!l.startsWith('+++')).map(l=>l.slice(1)).join('\n').replace('export const getHubURL','globalThis.getHubURL');
for(const protocol of ['http:','https:']){
 const ctx=vm.createContext({BESZEL:{HUB_URL:'http://192.168.1.186/monitor',BASE_PATH:'/monitor/'},window:{location:{protocol,hostname:'cluster.huynm.dev',origin:protocol+'//cluster.huynm.dev'}}});
 vm.runInContext(added,ctx);
 assert.equal(ctx.getHubURL(),protocol==='https:'?'https://cluster.huynm.dev/monitor':'http://192.168.1.186/monitor');
}
console.log('PASS HTTPS navigation and new agent URLs; legacy HTTP and device UI unchanged');

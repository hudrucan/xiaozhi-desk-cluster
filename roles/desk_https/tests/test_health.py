"""Real HAProxy checks through three Caddys and their local Unix HTTP proxies."""
import copy
import csv
import http.server
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from test_https import probe, proxy, render

NAMES=['cluster.huynm.dev','ntfy.cluster.huynm.dev','ha.cluster.huynm.dev']
NODES=['deskb1x','deskb2x','deskb3x']


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]


class Ready(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status=200 if self.path in ['/api/cluster/panel','/dns/','/ready','/api/desk_cluster/ready'] else 404
        if self.path=='/api/cluster/panel' and not self.server.dashboard_available:status=503
        self.send_response(status)
        self.end_headers()
    def log_message(self,*args):pass


@unittest.skipUnless(os.environ.get('CADDY_BIN') and os.environ.get('HAPROXY_BIN'), 'Requires native Caddy and SSL-enabled HAProxy 2.6 test builds')
class EndToEndHealth(unittest.TestCase):
    def test_dashboard_failure_keeps_ingress_up_but_local_proxy_failure_withdraws_and_recovers(self):
        with tempfile.TemporaryDirectory(prefix='https-health-') as tmp:
            tmp=Path(tmp);processes=[];logs=[]
            upstream=http.server.ThreadingHTTPServer(('127.0.0.1',0),Ready)
            upstream.dashboard_available=True
            thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
            def start(binary,config):
                log=(tmp/(config.name+'.log')).open('ab');logs.append(log)
                args=[binary,'run','--config',str(config)] if binary==os.environ['CADDY_BIN'] else [binary,'-db','-f',str(config)]
                p=subprocess.Popen(args,stdout=log,stderr=log,env={**os.environ,'XDG_DATA_HOME':str(tmp/'data'),'XDG_CONFIG_HOME':str(tmp/'config')})
                processes.append(p);return p
            def await_state(predicate):
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    try:
                        if predicate():return
                    except OSError:pass
                    time.sleep(.1)
                self.fail('Health state timed out: '+str({p.name:p.read_text()[-2500:] for p in tmp.glob('*.log')}))
            try:
                cert,key=tmp/'cert.pem',tmp/'key.pem';openssl=tmp/'openssl.cnf'
                openssl.write_text('[req]\ndistinguished_name=dn\n[dn]\n[SAN]\nsubjectAltName='+','.join('DNS:'+n for n in NAMES)+'\n')
                subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=cluster.huynm.dev','-config',str(openssl),'-extensions','SAN'],check=True,capture_output=True)
                original,fragment,caddyfile=render()
                adapted=subprocess.run([os.environ['CADDY_BIN'],'adapt','--adapter','caddyfile','--config','/dev/stdin'],input=caddyfile,text=True,capture_output=True,check=True)
                base=json.loads(adapted.stdout);base['admin']={'disabled':True}
                base['apps']['tls']={'certificates':{'load_files':[{'certificate':str(cert),'key':str(key)}]}}
                composed=proxy.https_candidate(original,fragment)
                caddy_ports={node:port() for node in NODES};local_configs={};locals_={};caddys={}
                plain_port=port()
                for node in NODES:
                    local='global\n    maxconn 100\ndefaults\n    mode http\n    timeout connect 1s\n    timeout client 2s\n    timeout server 2s\n'
                    for suffix in ['cluster','ntfy','ha']:
                        block=re.search(r'^# BEGIN DESK HTTPS '+suffix.upper()+r'\n(.*?)^# END DESK HTTPS '+suffix.upper()+r'\n',composed,re.M|re.S)[1]
                        block=block.replace('/run/haproxy/desk-https-'+suffix+'.sock',str(tmp/(node+'-'+suffix+'.sock'))).replace(' user desk-caddy group desk-caddy mode 600','')
                        block=block.replace('10.10.10.11 10.10.10.12 10.10.10.13','127.0.0.1')
                        plain_bind=f'    bind 127.0.0.1:{plain_port}\n' if node=='deskb1x' and suffix=='cluster' else ''
                        local+=f'frontend {suffix}\n'+plain_bind+block+'    http-request deny unless { src 192.168.1.0/24 127.0.0.1 }\n'+f'    http-response set-header X-Test-Node {node}\n    default_backend ready\n'
                    local+=f'backend ready\n    server local 127.0.0.1:{upstream.server_port}\n'
                    path=tmp/(node+'-local.cfg');path.write_text(local);local_configs[node]=path
                    locals_[node]=start(os.environ['HAPROXY_BIN'],path)
                    cfg=copy.deepcopy(base);server=next(iter(cfg['apps']['http']['servers'].values()))
                    server['listen']=[f'127.0.0.1:{caddy_ports[node]}'];server['automatic_https']={'disable':True};server['tls_connection_policies']=[{}]
                    def localize(value):
                        if isinstance(value,dict):
                            for k,v in value.items():value[k]=localize(v)
                        elif isinstance(value,list):return [localize(v) for v in value]
                        elif isinstance(value,str):
                            if value.startswith('10.10.10.'):return '127.0.0.1/32' if value.endswith('/32') else '127.0.0.1'
                            if value.startswith('unix//run/haproxy/desk-https-'):
                                suffix=value.split('desk-https-')[1].removesuffix('.sock');return 'unix/'+str(tmp/(node+'-'+suffix+'.sock'))
                        return value
                    cfg=localize(cfg);path=tmp/(node+'-caddy.json');path.write_text(json.dumps(cfg));caddys[node]=start(os.environ['CADDY_BIN'],path)
                stats=tmp/'stats.sock';public_port=port()
                fragment=fragment.replace('192.168.1.186:443',f'127.0.0.1:{public_port}').replace('source 10.10.10.11','source 127.0.0.1').replace('/etc/ssl/certs/ca-certificates.crt',str(cert))
                for i,node in enumerate(NODES):fragment=fragment.replace(f'10.10.10.{11+i}:8443',f'127.0.0.1:{caddy_ports[node]}')
                fragment=fragment.replace('inter 2s','inter 100ms')
                path=tmp/'router.cfg';path.write_text(f'global\n    stats socket {stats} level admin\ndefaults\n    mode tcp\n    timeout connect 1s\n    timeout client 2s\n    timeout server 2s\n'+fragment)
                start(os.environ['HAPROXY_BIN'],path)
                def states():
                    with socket.socket(socket.AF_UNIX) as s:
                        s.settimeout(2);s.connect(str(stats));s.sendall(b'show stat\n');data=b''
                        while True:
                            chunk=s.recv(65536)
                            if not chunk:break
                            data+=chunk
                    rows=csv.DictReader(io.StringIO(data.decode().replace('# ', '', 1)))
                    return {(r['pxname'],r['svname']):r['status'] for r in rows if r['svname'] in NODES}
                await_state(lambda: len(states())==9 and set(states().values())=={'UP'})
                context=ssl.create_default_context(cafile=str(cert))
                with socket.create_connection(('127.0.0.1',plain_port),timeout=3) as raw:
                    raw.sendall(b'GET /__desk_https_health HTTP/1.1\r\nHost: cluster.huynm.dev\r\nX-Forwarded-For: 10.10.10.11\r\nConnection: close\r\n\r\n')
                    self.assertIn(b' 404 ',raw.makefile('rb').readline())
                upstream.dashboard_available=False
                for name,uri,status in [(NAMES[0],'/api/cluster/panel',503),(NAMES[0],'/dns/',200),(NAMES[1],'/ready',200),(NAMES[2],'/api/desk_cluster/ready',200)]:
                    with socket.create_connection(('127.0.0.1',public_port),timeout=3) as raw:
                        with context.wrap_socket(raw,server_hostname=name) as tls:
                            tls.sendall(f'GET {uri} HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n'.encode())
                            self.assertIn(f' {status} '.encode(),tls.makefile('rb').readline())
                # More than fall=2 check intervals with Dashboard returning 503.
                for _ in range(6):
                    time.sleep(.2)
                    self.assertEqual(len(states()),9);self.assertEqual(set(states().values()),{'UP'})
                upstream.dashboard_available=True
                for name in NAMES:
                    with socket.create_connection(('127.0.0.1',caddy_ports['deskb1x']),timeout=3) as raw:
                        raw.sendall(probe.proxy_v2('192.168.1.123','127.0.0.1'))
                        with context.wrap_socket(raw,server_hostname=name) as tls:
                            tls.sendall(f'GET /__desk_https_health HTTP/1.1\r\nHost: {name}\r\nX-Forwarded-For: 127.0.0.1\r\nConnection: close\r\n\r\n'.encode())
                            self.assertIn(b' 404 ',tls.makefile('rb').readline())
                locals_['deskb1x'].terminate();locals_['deskb1x'].wait(timeout=5)
                await_state(lambda: all(status==('DOWN' if node=='deskb1x' else 'UP') for (_,node),status in states().items()))
                self.assertIsNone(caddys['deskb1x'].poll(),'Caddy must remain alive while its local HAProxy is down')
                # TLS itself still succeeds on the failed node: this is the P1
                # regression, not a Caddy process/network failure.
                with socket.create_connection(('127.0.0.1',caddy_ports['deskb1x']),timeout=2) as raw:
                    raw.sendall(b'\r\n\r\n\0\r\nQUIT\n\x20\x00\x00\x00')
                    with context.wrap_socket(raw,server_hostname=NAMES[0]):pass
                for name,uri in zip(NAMES,['/api/cluster/panel','/ready','/api/desk_cluster/ready']):
                    with socket.create_connection(('127.0.0.1',public_port),timeout=3) as raw:
                        with context.wrap_socket(raw,server_hostname=name) as tls:
                            tls.sendall(f'GET {uri} HTTP/1.1\r\nHost: {name}\r\nConnection: close\r\n\r\n'.encode())
                            response=tls.makefile('rb');self.assertIn(b' 200 ',response.readline())
                            headers=response.read().decode().lower();self.assertNotIn('x-test-node: deskb1x',headers)
                            self.assertRegex(headers,r'x-test-node: deskb[23]x')
                locals_['deskb1x']=start(os.environ['HAPROXY_BIN'],local_configs['deskb1x'])
                await_state(lambda: len(states())==9 and set(states().values())=={'UP'})
            finally:
                for p in reversed(processes):
                    if p.poll() is None:p.terminate();p.wait(timeout=5)
                upstream.shutdown();upstream.server_close();thread.join(timeout=2)
                for log in logs:log.close()


if __name__=='__main__':unittest.main()

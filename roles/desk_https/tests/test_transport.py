"""Real Caddy TLS -> Unix PROXY v2 -> source ACL; no production/DNS access."""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from test_https import render

SIGNATURE=b'\r\n\r\n\0\r\nQUIT\n'


class Backend(socketserver.StreamRequestHandler):
    def handle(self):
        header=self.rfile.read(16)
        if len(header)!=16 or header[:12]!=SIGNATURE or header[12:14]!=b'\x21\x11':return
        addresses=self.rfile.read(struct.unpack('!H',header[14:16])[0])
        client=str(ipaddress.ip_address(addresses[:4]))
        line=self.rfile.readline();headers={}
        while line:
            line=self.rfile.readline()
            if line in (b'\r\n',b''):break
            key,value=line.decode().split(':',1);headers[key.lower()]=value.strip()
        status=200 if ipaddress.ip_address(client) in ipaddress.ip_network('192.168.1.0/24') else 403
        body=json.dumps({'client':client,'headers':headers}).encode()
        self.wfile.write(f'HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)


@unittest.skipUnless(os.environ.get('CADDY_BIN'), 'Set CADDY_BIN to the native test build')
class Transport(unittest.TestCase):
    def test_real_tls_and_unix_proxy_preserve_client_and_ignore_spoofed_headers(self):
        with tempfile.TemporaryDirectory(prefix='desk-https-') as tmp:
            tmp=Path(tmp);sock=tmp/'backend.sock'
            backend=socketserver.UnixStreamServer(str(sock),Backend)
            thread=threading.Thread(target=backend.serve_forever,daemon=True);thread.start()
            caddy=None
            try:
                names=['cluster.huynm.dev','ntfy.cluster.huynm.dev','ha.cluster.huynm.dev']
                cert,key=tmp/'cert.pem',tmp/'key.pem'
                openssl=tmp/'openssl.cnf';openssl.write_text('[req]\ndistinguished_name=dn\n[dn]\n[SAN]\nsubjectAltName='+','.join('DNS:'+name for name in names)+'\n')
                subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=cluster.huynm.dev','-config',str(openssl),'-extensions','SAN'],check=True,capture_output=True)
                _,_,config=render()
                p=subprocess.run([os.environ['CADDY_BIN'],'adapt','--adapter','caddyfile','--config','/dev/stdin'],input=config,text=True,capture_output=True,check=True)
                cfg=json.loads(p.stdout);cfg['admin']={'disabled':True}
                cfg['apps']['tls']={'certificates':{'load_files':[{'certificate':str(cert),'key':str(key)}]}}
                with socket.socket() as reservation:
                    reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
                server=next(iter(cfg['apps']['http']['servers'].values()))
                server['listen']=[f'127.0.0.1:{port}'];server['automatic_https']={'disable':True}
                server['tls_connection_policies']=[{}]
                server['listener_wrappers'][1]['allow']=['127.0.0.1/32']
                def localize(value):
                    if isinstance(value,dict):
                        if value.get('handler')=='reverse_proxy':value['upstreams']=[{'dial':'unix/'+str(sock)}]
                        for item in value.values():localize(item)
                    elif isinstance(value,list):
                        for item in value:localize(item)
                localize(cfg)
                path=tmp/'caddy.json';path.write_text(json.dumps(cfg))
                with (tmp/'log').open('wb') as log:
                    caddy=subprocess.Popen([os.environ['CADDY_BIN'],'run','--config',str(path)],stdout=log,stderr=log,env={**os.environ,'XDG_DATA_HOME':str(tmp/'data'),'XDG_CONFIG_HOME':str(tmp/'config')})
                    for _ in range(100):
                        if caddy.poll() is not None:self.fail((tmp/'log').read_text())
                        try:
                            with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                        except OSError:time.sleep(.05)
                    context=ssl.create_default_context(cafile=str(cert))
                    for name in names:
                        for client in ['192.168.1.123','203.0.113.10']:
                            prefix=SIGNATURE+b'\x21\x11\x00\x0c'+ipaddress.ip_address(client).packed+socket.inet_aton('127.0.0.1')+struct.pack('!HH',49152,port)
                            with socket.create_connection(('127.0.0.1',port),timeout=3) as raw:
                                raw.sendall(prefix)
                                with context.wrap_socket(raw,server_hostname=name) as tls:
                                    tls.sendall(f'GET / HTTP/1.1\r\nHost: {name}\r\nX-Forwarded-For: 192.168.1.101\r\nX-Forwarded-Proto: http\r\nX-Forwarded-Host: evil.example\r\nForwarded: for=192.168.1.101\r\nCF-Connecting-IP: 192.168.1.101\r\nConnection: close\r\n\r\n'.encode())
                                    response=tls.makefile('rb');status=int(response.readline().split()[1])
                                    while response.readline()!=b'\r\n':pass
                                    result=json.loads(response.read())
                            self.assertEqual(status,200 if client.startswith('192.168.1.') else 403)
                            self.assertEqual(result['client'],client)
                            self.assertEqual(result['headers']['x-forwarded-for'],client)
                            self.assertEqual(result['headers']['x-forwarded-proto'],'https')
                            self.assertEqual(result['headers']['x-forwarded-host'],name)
                            self.assertNotIn('forwarded',result['headers']);self.assertNotIn('cf-connecting-ip',result['headers'])
            finally:
                if caddy:
                    caddy.terminate();caddy.wait(timeout=10)
                backend.shutdown();backend.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()

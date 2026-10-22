"""Run with HAPROXY=/path/to/haproxy; all sockets are ephemeral loopback sockets."""
import http.server
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import jinja2

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.environ.get('HAPROXY'), 'Set HAPROXY to run the actual proxy regression')
class Failover(unittest.TestCase):
    def test_local_first_ready_peer_fallback_failback_and_no_publish_replay(self):
        class Backend(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = self.server.node.encode()
                self.send_response(200 if self.path != '/ready' or self.server.ready else 503)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.server.publishes += 1
                self.close_connection = True  # Simulate an ambiguous response after accepting the body.

        backends = []
        process = None
        try:
            for i in range(1, 4):
                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Backend)
                server.node, server.ready, server.publishes = f'deskb{i}x', True, 0
                threading.Thread(target=server.serve_forever, daemon=True).start()
                backends.append(server)
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0))
                port = reserve.getsockname()[1]
            env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
            template = env.from_string((ROOT/'roles/desk_ntfy/templates/ingress.cfg.j2').read_text())
            nodes = [s.node for s in backends]
            config = template.render(inventory_hostname='deskb2x', cluster_ip='10.10.10.12', desk_ntfy_vip='192.168.1.186',
                groups={'deskboxes':nodes}, hostvars={n:{'cluster_ip':f'10.10.10.1{i}'} for i,n in enumerate(nodes,1)})
            config = config.split('# This per-node boundary')[0]
            config = config.replace('192.168.1.186:2586',f'127.0.0.1:{port}').replace('inter 2s','inter 100ms')
            for i,s in enumerate(backends,1):
                config = config.replace(f'10.10.10.1{i}:2588',f'127.0.0.1:{s.server_port}')
            config = 'global\n    maxconn 64\ndefaults\n    mode http\n    timeout connect 1s\n    timeout client 3s\n    timeout server 3s\n'+config+'\n'
            with tempfile.TemporaryDirectory() as temp:
                file=Path(temp)/'haproxy.cfg'
                file.write_text(config)
                subprocess.run([os.environ['HAPROXY'],'-c','-f',str(file)],check=True,capture_output=True)
                process=subprocess.Popen([os.environ['HAPROXY'],'-db','-f',str(file)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                url=f'http://127.0.0.1:{port}/desk-events'

                def reaches(node):
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        try:
                            with opener.open(url,timeout=.5) as response:
                                if response.read().decode()==node:
                                    return
                        except (OSError,urllib.error.URLError):
                            pass
                        time.sleep(.05)
                    self.fail(f'proxy never selected {node}')

                reaches('deskb2x')
                backends[1].ready=False
                reaches('deskb1x')
                backends[0].ready=False
                reaches('deskb3x')
                backends[1].ready=True
                reaches('deskb2x')
                try:
                    opener.open(urllib.request.Request(url,data=b'once'),timeout=2)
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code,502)
                    error.close()
                self.assertEqual(sum(s.publishes for s in backends),1)
        finally:
            if process:
                process.terminate()
                process.wait(timeout=3)
            for server in backends:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()

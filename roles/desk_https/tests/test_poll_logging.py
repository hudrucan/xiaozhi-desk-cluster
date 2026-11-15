"""Exercise generated polling policy over HTTP and the trusted PROXY Unix socket."""
import http.client
import http.server
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fixture_env import fixture_env
from test_https import load, probe, proxy, render

POLL = ['/api/cluster/panel', '/api/cluster/status', '/monitor/api/realtime']


class App(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        failure = query.get('failure', [''])[0]
        if failure == 'empty':
            self.close_connection = True
            return
        if failure == 'timeout':
            time.sleep(0.4)
        if failure == 'late':
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.close_connection = True
            return
        self.send_response(int(query.get('code', ['200'])[0]))
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_HEAD = do_GET

    def log_message(self, *args):
        pass


class PollLogging(unittest.TestCase):
    def test_both_generators_keep_routes_and_protected_base(self):
        guard = load('poll_ui_guard', 'roles/desk_ui/files/ui_guard.py')
        for node in ('deskb1x', 'deskb2x', 'deskb3x'):
            current, fragment, _ = render(node)
            self.assertEqual(current.count(proxy.POLL_LOGGING), 1)
            # Simulate the installed config preceding this change.
            before = current.replace(proxy.POLL_LOGGING, '')
            backend = 'backend desk_monitoring_hubs\n    server one 127.0.0.1:9001\n'
            after = proxy.candidate(before, backend)
            self.assertEqual(after.count(proxy.POLL_LOGGING), 1)
            self.assertEqual(proxy.candidate(after, backend), after)
            self.assertEqual(guard.base(before), guard.base(after))
            with patch.object(proxy, 'POLL_LOGGING', ''):
                control = proxy.candidate(before, backend)
            # Existing composition can reorder managed blocks. Compare against
            # that same composition without the policy, preserving every byte.
            self.assertEqual(after.replace(proxy.POLL_LOGGING, ''), control)
            https_before = proxy.https_candidate(before, fragment)
            https_after = proxy.candidate(https_before, backend)
            self.assertEqual(proxy.https_fragment(https_after), fragment)
            self.assertEqual(guard.base(https_before), guard.base(https_after))
            # The three pre-existing health log actions retain their exact bytes.
            for name in ('CLUSTER', 'NTFY', 'HA', 'IDENTITY'):
                pattern = rf'^# BEGIN DESK HTTPS {name}\n.*?^# END DESK HTTPS {name}\n'
                self.assertEqual(re.search(pattern, https_before, re.M|re.S)[0],
                                 re.search(pattern, https_after, re.M|re.S)[0])
        # Monitoring-only installations use the existing monitoring boundary.
        minimal = 'frontend xiaozhi_settings\n    bind 127.0.0.1:8080\n    default_backend xiaozhi_control_planes\nbackend xiaozhi_control_planes\n    server one 127.0.0.1:8004\n'
        after = proxy.candidate(minimal, backend)
        self.assertEqual(after.count(proxy.POLL_LOGGING), 1)
        self.assertEqual(guard.base(minimal), guard.base(after))

    def test_2xx_get_silent_but_audit_and_all_pre_response_errors_logged(self):
        binary = os.environ.get('HAPROXY_BIN') or shutil.which('haproxy')
        if not binary:
            self.skipTest('native HAProxy unavailable')
        with tempfile.TemporaryDirectory(prefix='desk-poll-log-') as temp:
            directory = Path(temp)
            backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), App)
            worker = threading.Thread(target=backend.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(backend.server_close)
            self.addCleanup(worker.join, 2)
            self.addCleanup(backend.shutdown)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as logs, socket.socket() as refused:
                logs.bind(('127.0.0.1', 0))
                logs.settimeout(0.2)
                refused.bind(('127.0.0.1', 0))  # Bound but not listening: deterministic refusal.
                with socket.socket() as reserve:
                    reserve.bind(('127.0.0.1', 0))
                    port = reserve.getsockname()[1]
                current, _, _ = render()
                frontend = current.split('frontend xiaozhi_settings\n', 1)[1].split('\nbackend ', 1)[0]
                frontend = re.sub(r'^    bind .*\n', '', frontend, flags=re.M)
                # Keep actual generated route/ACL/logging rules and real backend
                # rewrites. Insert only fixture failure dispatch before routing.
                faults = ''.join(f'    use_backend fail_{name} if {{ urlp(failure) -m str {name} }}\n' for name in ('refused', 'nosrv'))
                frontend = frontend.replace('    use_backend ', faults+'    use_backend ', 1)
                config = (f'global\n    log 127.0.0.1:{logs.getsockname()[1]} local0\n'
                          'defaults\n    mode http\n    log global\n    option httplog\n'
                          '    timeout connect 100ms\n    timeout client 1s\n    timeout server 150ms\n'
                          f'frontend xiaozhi_settings\n    bind 127.0.0.1:{port}\n'
                          f'    bind {directory}/cluster.sock accept-proxy\n'+frontend+'\n')
                names = set(re.findall(r'^    (?:use_backend|default_backend) (\S+)', frontend, re.M))
                for name in sorted(names):
                    config += f'backend {name}\n'
                    if name == 'fail_nosrv':
                        continue
                    target = refused.getsockname()[1] if name == 'fail_refused' else backend.server_port
                    if name == 'desk_monitoring_hubs':
                        config += '    http-request replace-path ^/monitor/(.*)$ /\\1\n'
                    config += f'    server app 127.0.0.1:{target}\n'
                path = directory/'haproxy.cfg'
                path.write_text(config)
                env = fixture_env(directory)
                valid = subprocess.run([binary, '-c', '-f', str(path)], env=env, capture_output=True, text=True)
                self.assertEqual(valid.returncode, 0, valid.stderr)
                with (directory/'process.log').open('w') as output:
                    process = subprocess.Popen([binary, '-db', '-f', str(path)], env=env, stdout=output, stderr=output)
                    try:
                        def request(transport, uri, method='GET'):
                            if transport == 'http':
                                sock = socket.create_connection(('127.0.0.1', port), timeout=2)
                            else:
                                sock = socket.socket(socket.AF_UNIX)
                                sock.settimeout(2)
                                sock.connect(str(directory/'cluster.sock'))
                                sock.sendall(probe.proxy_v2('192.168.1.42', '192.168.1.186'))
                            with sock:
                                sock.sendall(f'{method} {uri} HTTP/1.1\r\nHost: cluster.huynm.dev\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'.encode())
                                response = http.client.HTTPResponse(sock, method=method)
                                response.begin()
                                try:
                                    response.read()
                                except http.client.IncompleteRead:
                                    if 'failure=late' not in uri:
                                        raise
                                return response.status
                        deadline = time.monotonic()+5
                        while True:
                            try:
                                request('http', '/startup')
                                break
                            except ConnectionRefusedError:
                                if time.monotonic()>deadline:
                                    self.fail((directory/'process.log').read_text())
                                time.sleep(0.05)
                        expected, silent = {}, []
                        case = 0
                        def exercise(transport, uri, code, method='GET', quiet=False):
                            nonlocal case
                            case += 1
                            marker = f'case={case:04d}'
                            uri += ('&' if '?' in uri else '?')+marker
                            self.assertEqual(request(transport, uri, method), code, uri)
                            if quiet:
                                silent.append(marker)
                            else:
                                expected[marker] = code
                        for transport in ('http', 'unix'):
                            for uri in POLL:
                                for code in (200,201,204,206,299,301,400,403,404,500,503):
                                    exercise(transport, uri+f'?code={code}', code, quiet=200<=code<300)
                                for method in ('POST','PUT','DELETE','HEAD'):
                                    exercise(transport, uri+'?code=200', 200, method)
                                for failure, code in [('refused',503),('nosrv',503),('empty',502),('timeout',504)]:
                                    exercise(transport, uri+'?failure='+failure, code)
                            # Prove the documented limit: a truncated body after
                            # a 2xx header is already silent on HAProxy 2.6's hook.
                            exercise(transport, POLL[0]+'?failure=late', 200, quiet=True)
                            for uri in ('/api/cluster/operations', '/api/cluster/panel/', '/api/cluster/status/extra', '/monitor/api/realtime/', '/api/cluster/PANEL'):
                                exercise(transport, uri, 200)
                        messages = []
                        deadline = time.monotonic()+4
                        while time.monotonic()<deadline:
                            try:
                                messages.append(logs.recv(65535).decode())
                            except socket.timeout:
                                if all(any(marker in line for line in messages) for marker in expected):
                                    break
                        joined = ''.join(messages)
                        for marker in silent:
                            self.assertNotIn(marker, joined)
                        for marker, code in expected.items():
                            matching = [line for line in messages if marker in line]
                            self.assertEqual(len(matching), 1, (marker, joined))
                            self.assertIn(f' {code} ', matching[0])
                        self.assertTrue(any('<NOSRV>' in line and ' 503 ' in line for line in messages), joined)
                    finally:
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()

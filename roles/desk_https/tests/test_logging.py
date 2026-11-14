"""Exercise the composed health logging policy using native HAProxy and UDP syslog."""
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

from fixture_env import fixture_env
from test_https import probe, proxy, render


class Ready(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status = 503 if 'failed=1' in self.path else 500 if self.path == '/error' else 200
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *args):
        pass


class HealthLogging(unittest.TestCase):
    def test_composition_is_scoped_idempotent_and_reversible(self):
        response_rule = '    http-response set-log-level silent if { var(txn.desk_https_health) -m bool } { status 200 }\n'
        request_rule = '    http-request set-log-level silent if { var(txn.desk_https_health) -m bool }\n'
        for node in ('deskb1x', 'deskb2x', 'deskb3x'):
            current, fragment, _ = render(node)
            updated = proxy.https_candidate(current, fragment)
            self.assertEqual(updated.count(response_rule), 2)
            self.assertEqual(updated.count(request_rule), 1)
            # Pin the complete log action set to the two HAProxy 2.6-compatible forms.
            actions = re.findall(r'^    http-\S+ set-log-level[^\n]*\n', updated, re.MULTILINE)
            self.assertCountEqual(actions, [request_rule, response_rule, response_rule])
            self.assertNotIn('http-after-response set-log-level', updated)
            for name in ('cluster', 'ntfy', 'ha'):
                block = updated.split(f'# BEGIN DESK HTTPS {name.upper()}\n')[1].split(f'# END DESK HTTPS {name.upper()}\n')[0]
                if name == 'cluster':
                    self.assertNotIn(response_rule, block)
                    self.assertIn(request_rule + '    http-request return status 200 content-type text/plain string ok if { var(txn.desk_https_health) -m bool }\n', block)
                else:
                    self.assertIn(response_rule, block)
                    self.assertNotIn(request_rule, block)
            self.assertEqual(proxy.https_candidate(updated), updated)
            self.assertEqual(proxy.https_candidate(updated, ''), current.rstrip() + '\n')
            # The public TCP passthrough and its checks are untouched by composition.
            self.assertEqual(proxy.https_fragment(updated), fragment)
            self.assertNotIn('dontlog-normal', updated)

    def test_success_is_silent_but_failures_and_client_requests_are_logged(self):
        binary = os.environ.get('HAPROXY_BIN') or shutil.which('haproxy')
        if not binary:
            self.skipTest('native HAProxy unavailable')
        with tempfile.TemporaryDirectory(prefix='desk-health-logging-') as temp:
            directory = Path(temp)
            backend = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Ready)
            worker = threading.Thread(target=backend.serve_forever, daemon=True)
            worker.start()
            self.addCleanup(backend.server_close)
            self.addCleanup(worker.join, 2)
            self.addCleanup(backend.shutdown)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as logs:
                logs.bind(('127.0.0.1', 0))
                logs.settimeout(0.2)
                current, fragment, _ = render()
                composed = proxy.https_candidate(current, fragment)
                config = (f'global\n    log 127.0.0.1:{logs.getsockname()[1]} local0\n'
                          'defaults\n    mode http\n    log global\n    option httplog\n'
                          '    timeout connect 1s\n    timeout client 2s\n    timeout server 1s\n')
                ports = {}
                # Extract the actual generated block, including the trust and rewrite rules.
                for name in ('cluster', 'ntfy', 'ha'):
                    with socket.socket() as reserve:
                        reserve.bind(('127.0.0.1', 0))
                        ports[name] = reserve.getsockname()[1]
                    block = composed.split(f'# BEGIN DESK HTTPS {name.upper()}\n')[1].split(f'# END DESK HTTPS {name.upper()}\n')[0]
                    block = re.sub(r'^    bind .*\n', '', block, flags=re.MULTILINE)
                    config += (f'frontend {name}\n    bind 127.0.0.1:{ports[name]} accept-proxy\n'
                               + block + '\n    default_backend app\n')
                config += f'backend app\n    server app 127.0.0.1:{backend.server_port}\n'
                path = directory / 'haproxy.cfg'
                path.write_text(config)
                result = subprocess.run([binary, '-c', '-f', str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                with (directory / 'process.log').open('w') as output:
                    process = subprocess.Popen([binary, '-db', '-f', str(path)], stdout=output, stderr=output,
                                               env=fixture_env(directory))
                    try:
                        def request(name, path, source='10.10.10.11', method='GET'):
                            with socket.create_connection(('127.0.0.1', ports[name]), timeout=2) as sock:
                                sock.sendall(probe.proxy_v2(source, '127.0.0.1'))
                                sock.sendall(f'{method} {path} HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'.encode())
                                response = http.client.HTTPResponse(sock)
                                response.begin()
                                response.read()
                                return response.status
                        deadline = time.monotonic() + 5
                        while True:
                            try:
                                self.assertEqual(request('cluster', '/startup'), 200)
                                break
                            except ConnectionRefusedError:
                                if time.monotonic() > deadline:
                                    self.fail('HAProxy did not start: ' + (directory / 'process.log').read_text())
                                time.sleep(0.05)
                        for name in ports:
                            self.assertEqual(request(name, '/__desk_https_health?silent=1'), 200)
                            self.assertEqual(request(name, '/ok', source='192.168.1.42'), 200)
                            self.assertEqual(request(name, '/error'), 500)
                            self.assertEqual(request(name, '/__desk_https_health/client'), 200)
                            expected = 404 if name == 'cluster' else 200
                            self.assertEqual(request(name, '/__desk_https_health?client=1', source='192.168.1.42'), expected)
                            self.assertEqual(request(name, '/__desk_https_health?post=1', method='POST'), expected)
                            if name != 'cluster':
                                self.assertEqual(request(name, '/__desk_https_health?failed=1'), 503)
                        messages = []
                        # UDP delivery is asynchronous; wait for all expected transaction logs.
                        expected_count = 1 + 3 * 5 + 2
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            try:
                                messages.append(logs.recv(65535).decode())
                            except socket.timeout:
                                if len(messages) >= expected_count:
                                    break
                        joined = ''.join(messages)
                        self.assertNotIn('silent=1', joined)
                        for name in ports:
                            lines = [line for line in messages if f' {name} ' in line]
                            for marker in ('/ok ', '/error ', '/__desk_https_health/client ', 'client=1', 'post=1'):
                                self.assertTrue(any(marker in line for line in lines), (name, marker, joined))
                            if name != 'cluster':
                                self.assertTrue(any('failed=1' in line and ' 503 ' in line for line in lines), joined)
                        self.assertEqual(len(messages), expected_count, joined)
                    finally:
                        process.terminate()
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()

"""Execute the YAML-rendered stats command against a local HAProxy socket fixture."""
import csv
import io
import os
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import unittest
import jinja2
import yaml

ROOT=Path(__file__).resolve().parents[3]
TASK=next(t for t in yaml.safe_load((ROOT/'roles/desk_https/tasks/verify.yml').read_text()) if t.get('register')=='desk_https_stats')
ARGV=[jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(a).render() for a in TASK['ansible.builtin.command']['argv']]


class Stats(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        self.server.requests.append(self.rfile.readline())
        self.wfile.write(self.server.payload)


class VerifyStats(unittest.TestCase):
    def rows(self):
        return [(f'desk_caddy_{host}',node,'UP','L7OK','200')
                for host in ['cluster','ntfy','ha'] for node in ['deskb1x','deskb2x','deskb3x']]

    def test_nine_up_pass_and_unrelated_rows_do_not_affect_the_gate(self):
        self.run_gate(self.rows()+[('desk_caddy_cluster','BACKEND','UP','',''),('other_service','deskb1x','DOWN','','')],True)
        self.assertEqual((TASK['retries'],TASK['delay'],TASK['until']),(10,2,'desk_https_stats.rc == 0'))
        self.assertFalse(TASK['changed_when'])

    def test_missing_backend_fails(self):self.run_gate(self.rows()[:-1],False)

    def test_down_backend_fails(self):
        rows=self.rows();rows[-1]=(*rows[-1][:2],'DOWN','L7STS','503');self.run_gate(rows,False)

    def test_extra_backend_fails(self):self.run_gate(self.rows()+[self.rows()[0]],False)

    def test_empty_stats_fail_closed(self):self.run_gate([],False)

    def run_gate(self,rows,accepted):
        out=io.StringIO();writer=csv.writer(out)
        writer.writerow(['# pxname','svname','status','check_status','check_code']);writer.writerows(rows)
        with tempfile.TemporaryDirectory(prefix='verify-stats-') as tmp:
            tmp=Path(tmp);address=tmp/'stats.sock'
            # Remap only the production socket path. Execute the complete rendered
            # argv unchanged, including its interpreter, parser and assertions.
            (tmp/'sitecustomize.py').write_text("""import os,socket
original=socket.socket.connect
def connect(self,address):
    assert address=='/run/xiaozhi-haproxy.sock'
    assert self.gettimeout()==5
    return original(self,os.environ['HTTPS_TEST_SOCKET'])
socket.socket.connect=connect
""")
            server=socketserver.UnixStreamServer(str(address),Stats)
            server.payload=out.getvalue().encode();server.requests=[]
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                result=subprocess.run(ARGV,env={**os.environ,'PYTHONPATH':str(tmp),'HTTPS_TEST_SOCKET':str(address)},capture_output=True,text=True,timeout=10)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
            self.assertEqual(result.returncode==0,accepted,result.stderr)
            self.assertEqual(server.requests,[b'show stat\n'])
            self.assertEqual(result.stdout,'')
            if accepted:self.assertEqual(result.stderr,'')
            else:self.assertIn('HTTPS TLS health checks not all UP',result.stderr)


if __name__=='__main__':unittest.main()

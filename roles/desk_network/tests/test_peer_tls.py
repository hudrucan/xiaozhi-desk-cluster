"""Peer RPC TLS session resumption: faster handshakes without weaker pinning."""

import datetime
import http.server
import importlib.util
import ipaddress
import pathlib
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES))
spec = importlib.util.spec_from_file_location("ha", FILES / "ha.py")
ha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha)

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    HAVE_TLS_FIXTURE = True
except ImportError:
    HAVE_TLS_FIXTURE = False


@unittest.skipUnless(
    HAVE_TLS_FIXTURE, "cryptography is required to mint a throwaway test certificate"
)
class PeerTlsResumption(unittest.TestCase):
    @staticmethod
    def mint(directory, name, san):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .sign(key, hashes.SHA256())
        )
        crt = pathlib.Path(directory) / (name + ".crt")
        pem = pathlib.Path(directory) / (name + ".key")
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        pem.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return str(crt), str(pem)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        loopback = x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
        self.good = self.mint(self.dir, "peer", loopback)
        self.reused = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.reused.append(self.connection.session_reused)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.Handler = Handler

    def serve(self, pair, port=0):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*pair)
        server = ha.Server(("127.0.0.1", port), self.Handler)
        server.cluster = SimpleNamespace(members={"a": "127.0.0.1"})
        server.tls_context = context
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop():
            if server.socket.fileno() != -1:
                server.shutdown()
                server.server_close()

        self.addCleanup(stop)
        return server

    def opener(self, cafile):
        context = ssl.create_default_context(cafile=cafile)
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), ha.resuming_https_handler(context)
        )

    def post(self, opener, port):
        request = urllib.request.Request(
            "https://127.0.0.1:%d/peer/status" % port, data=b"{}"
        )
        with opener.open(request, timeout=5) as response:
            return response.status, response.read()

    def test_session_is_resumed_after_the_first_full_handshake(self):
        server = self.serve(self.good)
        port = server.server_address[1]
        opener = self.opener(self.good[0])
        for _ in range(4):
            self.assertEqual(self.post(opener, port), (200, b'{"ok":true}'))
        self.assertEqual(self.reused, [False, True, True, True])

    def test_resumption_never_weakens_certificate_pinning_or_identity(self):
        server = self.serve(self.good)
        port = server.server_address[1]
        loopback = x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
        other = self.mint(self.dir, "other", loopback)
        wrong_name = self.mint(self.dir, "named", x509.DNSName("peer.example"))
        self.assertEqual(self.post(self.opener(self.good[0]), port)[0], 200)
        with self.assertRaises(urllib.error.URLError):  # not the pinned certificate
            self.post(self.opener(other[0]), port)
        named = self.serve(wrong_name)
        with self.assertRaises(urllib.error.URLError):  # hostname mismatch
            self.post(self.opener(wrong_name[0]), named.server_address[1])
        self.assertEqual(self.reused, [False])

    def test_server_restart_falls_back_to_a_full_verified_handshake(self):
        first = self.serve(self.good)
        port = first.server_address[1]
        opener = self.opener(self.good[0])
        self.post(opener, port)
        self.post(opener, port)
        first.shutdown()
        first.server_close()
        self.serve(
            self.good, port
        )  # new SSLContext: the old ticket cannot be decrypted
        self.assertEqual(self.post(opener, port), (200, b'{"ok":true}'))
        self.assertEqual(self.reused, [False, True, False])
        self.post(opener, port)
        self.assertTrue(self.reused[-1])

    def test_unreachable_peer_still_raises_urlerror(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(urllib.error.URLError):
            self.post(self.opener(self.good[0]), port)


if __name__ == "__main__":
    unittest.main()

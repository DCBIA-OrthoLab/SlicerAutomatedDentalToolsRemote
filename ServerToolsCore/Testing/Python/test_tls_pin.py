"""The out-of-band pin for a server on this machine (tls.py, rule 3).

Every live test runs a real TLS server on 127.0.0.1 with a self-signed
certificate and points `tls.local_tls_dir` at a throwaway folder. What is
checked is what the rule promises: the pin is accepted only for the loopback
hosts, only when the file is where and what it should be, only for the very
certificate it names; and nothing else about the doctrine moves (http refused,
the public bundle everywhere else, no `verify=False`).
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from tls_fixture import make_self_signed, serve_tls_with  # noqa: E402
from ServerToolsCoreLib import InsecureServerUrl, client as client_module, tls  # noqa: E402
from ServerToolsCoreLib.client import ToolServerClient  # noqa: E402

POSIX = os.name == "posix"
_REAL_LOCAL_TLS_DIR = tls.local_tls_dir      # before any test patches it


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PinCase(unittest.TestCase):
    """A pin folder with certificate A in it, and a spare certificate B."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="tls_pin_")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.pin_dir = os.path.join(self.work, "pin")
        os.mkdir(self.pin_dir, 0o700)
        self.other_dir = os.path.join(self.work, "other")
        os.mkdir(self.other_dir, 0o700)
        self.cert_a, self.key_a = make_self_signed(self.other_dir, "a")
        self.cert_b, self.key_b = make_self_signed(self.other_dir, "b")
        patcher = mock.patch.object(tls, "local_tls_dir", return_value=self.pin_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pin(self, source=None):
        """Install a certificate as THE pin, with the mode SlicerCloud gives it."""
        target = os.path.join(self.pin_dir, tls.LOCAL_CERT_NAME)
        shutil.copyfile(source or self.cert_a, target)
        os.chmod(target, 0o644)
        return target

    def serve(self, cert, key, host="127.0.0.1"):
        server = serve_tls_with(ThreadingHTTPServer((host, 0), _Handler), cert, key)
        server.handle_error = lambda *_args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]


class PinnedHandshakeTest(PinCase):
    def test_the_right_certificate_is_accepted(self):
        self.pin()
        port = self.serve(self.cert_a, self.key_a)
        self.assertTrue(ToolServerClient(f"https://127.0.0.1:{port}", "token").health())

    def test_localhost_is_not_covered(self):
        """A name goes through the hosts file; only the two literal addresses
        are pinned, so `localhost` falls back to the public authorities."""
        self.pin()
        port = self.serve(self.cert_a, self.key_a)
        self.assertIsNone(tls.pin_for_url(f"https://localhost:{port}"))
        self.assertFalse(ToolServerClient(f"https://localhost:{port}", "token").health())

    def test_urllib_accepts_it_as_well(self):
        self.pin()
        port = self.serve(self.cert_a, self.key_a)
        with tls.urlopen(f"https://127.0.0.1:{port}/health", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"status": "ok"})

    def test_another_certificate_is_refused(self):
        """A squatter on the port presents a perfectly valid self-signed
        certificate of its own: it is not the pinned one."""
        self.pin(self.cert_a)
        port = self.serve(self.cert_b, self.key_b)
        self.assertFalse(ToolServerClient(f"https://127.0.0.1:{port}", "token").health())
        import urllib.error
        with self.assertRaises(urllib.error.URLError):
            tls.urlopen(f"https://127.0.0.1:{port}/health", timeout=5)

    def test_the_certificate_alone_is_not_enough_without_its_key(self):
        """Only certificate A's public half is in the pin; a server that has
        A's certificate but not A's key cannot complete the handshake."""
        self.pin(self.cert_a)
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        with self.assertRaises(ssl.SSLError):
            context.load_cert_chain(self.cert_a, self.key_b)

    def test_no_file_means_the_public_authorities_and_a_refusal(self):
        port = self.serve(self.cert_a, self.key_a)
        self.assertFalse(os.path.exists(os.path.join(self.pin_dir, tls.LOCAL_CERT_NAME)))
        self.assertFalse(ToolServerClient(f"https://127.0.0.1:{port}", "token").health())

    def test_the_environment_cannot_stand_in_for_the_pin(self):
        port = self.serve(self.cert_a, self.key_a)
        with mock.patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": self.cert_a,
                                          "CURL_CA_BUNDLE": self.cert_a, "SSL_CERT_FILE": self.cert_a}):
            self.assertFalse(ToolServerClient(f"https://127.0.0.1:{port}", "token").health())

    def test_a_non_loopback_host_never_uses_the_pin(self):
        """The pinned certificate even names 127.0.0.2, and a server there
        presents it: the pin is still not applied, because that is not one of
        the three hosts the rule covers."""
        cert, key = make_self_signed(self.other_dir, "c", "IP:127.0.0.1,IP:127.0.0.2")
        self.pin(cert)
        port = self.serve(cert, key, host="127.0.0.2")
        self.assertIsNone(tls.pin_for_url(f"https://127.0.0.2:{port}"))
        self.assertFalse(ToolServerClient(f"https://127.0.0.2:{port}", "token").health())

    def test_the_verification_argument_is_the_pin_only_for_loopback(self):
        import certifi
        self.pin()
        seen = []

        def spy(adapter, request, **kwargs):
            seen.append((request.url, kwargs.get("verify")))
            raise requests.exceptions.ConnectionError("not sent")

        client = ToolServerClient("https://127.0.0.1:9", "token")
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", spy):
            client.health()
            ToolServerClient("https://tools.example.org", "token").health()
        pin = os.path.join(self.pin_dir, tls.LOCAL_CERT_NAME)
        self.assertEqual(seen[0], ("https://127.0.0.1:9/health", pin))
        self.assertEqual(seen[1][1], certifi.where())
        for _url, verify in seen:
            self.assertNotIn(verify, (False, True, None))

    def test_a_redirect_is_still_never_followed(self):
        self.pin()

        class Redirect(_Handler):
            def do_GET(self):
                self.send_response(307)
                self.send_header("Location", "http://localhost:9/health")
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = serve_tls_with(ThreadingHTTPServer(("127.0.0.1", 0), Redirect), self.cert_a, self.key_a)
        server.handle_error = lambda *_args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        session = client_module._pooled_session()
        with self.assertRaises(client_module.RedirectRefused):
            session.get(f"https://127.0.0.1:{server.server_address[1]}/x", verify=tls.ca_bundle())


class PlainHttpStaysRefusedTest(PinCase):
    def test_http_loopback_is_refused_by_every_door_even_with_a_pin(self):
        self.pin()
        port = self.serve(self.cert_a, self.key_a)
        for url in (f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"):
            with self.subTest(url=url):
                with self.assertRaises(InsecureServerUrl):
                    ToolServerClient(url, "token")
                with self.assertRaises(InsecureServerUrl):
                    tls.urlopen(f"{url}/health", timeout=1)
                self.assertIsNone(tls.pin_for_url(url))
        with mock.patch("socket.create_connection") as connect:
            with self.assertRaises(client_module.PlainHttpRefused):
                client_module._pooled_session().get(f"http://127.0.0.1:{port}/health")
        connect.assert_not_called()


class PinnedCertificateTest(PinCase):
    def test_only_two_hosts_are_covered(self):
        path = self.pin()
        for host in ("127.0.0.1", "::1", "[::1]"):
            self.assertEqual(tls.pinned_certificate(host), path, host)
        for host in ("localhost", "LOCALHOST", "127.0.0.2", "127.1", "0.0.0.0", "dev.localhost", "10.0.0.5", "",
                     None, "tools.example.org", "localhost.example.org", "127.0.0.1.example.org"):
            self.assertIsNone(tls.pinned_certificate(host), host)

    def test_no_file(self):
        self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    def test_a_missing_folder(self):
        with mock.patch.object(tls, "local_tls_dir", return_value=os.path.join(self.work, "nope")):
            self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    def test_garbage_is_not_a_certificate(self):
        path = self.pin()
        for content in (b"", b"hello", b"-----BEGIN CERTIFICATE-----\nnot base64 !!\n-----END CERTIFICATE-----\n",
                        b"\xff\xfe"):
            with self.subTest(content=content[:20]):
                with open(path, "wb") as handle:
                    handle.write(content)
                self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    def test_a_bundle_is_not_a_pin(self):
        """Two certificates would be two trust anchors."""
        path = self.pin()
        with open(self.cert_a, "rb") as first, open(self.cert_b, "rb") as second, open(path, "wb") as out:
            out.write(first.read() + second.read())
        self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    def test_only_a_plain_certificate_block_is_a_pin(self):
        """OpenSSL loads TRUSTED CERTIFICATE as a trust anchor, and the old
        check only counted `BEGIN CERTIFICATE` lines."""
        path = self.pin()
        with open(self.cert_a, "r", encoding="ascii") as handle:
            good = handle.read()
        with open(self.cert_b, "r", encoding="ascii") as handle:
            other = handle.read()

        def relabel(pem, label):
            return pem.replace("CERTIFICATE", label)

        rogue = relabel(other, "TRUSTED CERTIFICATE")
        cases = {
            "trusted block alone": rogue,
            "x509 block alone": relabel(good, "X509 CERTIFICATE"),
            "good + trusted block": good + rogue,
            "trusted block + good": rogue + good,
            "audit: good + trusted + stray END": good + rogue.replace(
                "-----END TRUSTED CERTIFICATE-----", "-----END CERTIFICATE-----"),
            "stray marker line": good + "-----BEGIN TRUSTED CERTIFICATE-----\n",
            "text outside the block": "not a comment\n" + good,
            "trailing junk": good + "AAAA\n",
        }
        for name, content in cases.items():
            with self.subTest(name):
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(content)
                self.assertIsNone(tls.pinned_certificate("127.0.0.1"))
        for name, content in {"plain": good, "comments": "# pin\n" + good + "\n# end\n"}.items():
            with self.subTest(name):
                with open(path, "w", encoding="ascii") as handle:
                    handle.write(content)
                self.assertEqual(tls.pinned_certificate("127.0.0.1"), path)

    def test_a_file_holding_a_private_key_is_not_a_pin(self):
        path = self.pin()
        with open(path, "ab") as out, open(self.key_a, "rb") as key:
            out.write(key.read())
        self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    def test_an_oversized_file_is_refused(self):
        path = self.pin()
        with open(path, "ab") as out:
            out.write(b"# padding\n" * 4000)
        self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    @unittest.skipUnless(POSIX, "mode bits")
    def test_a_file_others_can_write_is_refused(self):
        path = self.pin()
        for mode in (0o664, 0o666, 0o646):
            os.chmod(path, mode)
            self.assertIsNone(tls.pinned_certificate("127.0.0.1"), oct(mode))
        os.chmod(path, 0o600)
        self.assertEqual(tls.pinned_certificate("127.0.0.1"), path)

    @unittest.skipUnless(POSIX, "mode bits")
    def test_a_folder_others_can_write_is_refused(self):
        self.pin()
        os.chmod(self.pin_dir, 0o777)
        self.addCleanup(os.chmod, self.pin_dir, 0o700)
        self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_a_symbolic_link_is_refused(self):
        target = os.path.join(self.other_dir, "elsewhere.crt")
        shutil.copyfile(self.cert_a, target)
        os.symlink(target, os.path.join(self.pin_dir, tls.LOCAL_CERT_NAME))
        self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_a_symbolic_link_for_the_folder_is_refused(self):
        self.pin()
        link = os.path.join(self.work, "link")
        os.symlink(self.pin_dir, link)
        with mock.patch.object(tls, "local_tls_dir", return_value=link):
            self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    @unittest.skipUnless(POSIX, "ownership")
    def test_a_file_owned_by_someone_else_is_refused(self):
        self.pin()
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1):
            self.assertIsNone(tls.pinned_certificate("127.0.0.1"))

    def test_the_default_folder_is_under_the_account_home_not_HOME(self):
        folder = _REAL_LOCAL_TLS_DIR()
        self.assertTrue(folder.endswith(os.path.join(".slicercloud", "tls")))
        if POSIX:
            with mock.patch.dict(os.environ, {"HOME": "/tmp/attacker-home"}):
                self.assertNotIn("attacker-home", _REAL_LOCAL_TLS_DIR())


if __name__ == "__main__":
    unittest.main()

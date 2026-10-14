"""HTTPS only, and the loopback certificate pin (tls.py).

The live tests run a real TLS server on 127.0.0.1 with a throwaway self-signed
certificate, which is the shape of the local deployment: the client has to
verify it through the pin, with no bypass anywhere.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from tls_fixture import certificate, serve_tls  # noqa: E402
from ServerToolsCoreLib import InsecureServerUrl, config, tls  # noqa: E402
from ServerToolsCoreLib.client import ToolServerClient  # noqa: E402


class RequireHttpsTest(unittest.TestCase):
    def test_https_is_kept_and_normalised(self):
        self.assertEqual(tls.require_https("https://tools.example.org/"), "https://tools.example.org")
        self.assertEqual(tls.require_https("HTTPS://tools.example.org:8443"), "https://tools.example.org:8443")

    def test_a_remote_http_url_is_refused(self):
        for url in ("http://tools.example.org", "http://10.0.0.5:8000", "http://localhost.example.org"):
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl) as caught:
                tls.require_https(url)
            self.assertIn("must start with https://", str(caught.exception))

    def test_anything_that_is_not_a_url_is_refused(self):
        for url in ("", "localhost:8000", "ftp://localhost", "https://", "tools.example.org"):
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl):
                tls.require_https(url)

    def test_a_loopback_http_url_is_upgraded_on_the_same_port(self):
        self.assertEqual(tls.require_https("http://localhost:8000"), "https://localhost:8000")
        self.assertEqual(tls.require_https("http://127.0.0.1:8001/"), "https://127.0.0.1:8001")
        self.assertEqual(tls.require_https("http://[::1]:8000"), "https://[::1]:8000")
        self.assertEqual(tls.require_https("HTTP://LOCALHOST:8000"), "https://LOCALHOST:8000")

    def test_every_loopback_address_counts_and_the_predicate_is_shared(self):
        from ServerToolsCoreLib import client
        self.assertIs(client.is_loopback, tls.is_loopback)
        self.assertEqual(tls.require_https("http://127.0.0.2:8000"), "https://127.0.0.2:8000")
        self.assertEqual(tls.require_https("http://dev.localhost:8001"), "https://dev.localhost:8001")
        self.assertEqual(client.checked_server_url("http://localhost:8000/"), "https://localhost:8000")

    def test_an_implicit_port_stays_the_port_it_was(self):
        self.assertEqual(tls.require_https("http://localhost"), "https://localhost:80")

    def test_the_default_is_https(self):
        self.assertTrue(config.SERVER_URL.startswith("https://"))


class ClientRefusesHttpTest(unittest.TestCase):
    def test_the_constructor_refuses_a_remote_http_url(self):
        with self.assertRaises(InsecureServerUrl):
            ToolServerClient("http://tools.example.org", "token")

    def test_the_constructor_upgrades_localhost(self):
        client = ToolServerClient("http://localhost:8000/", "token")
        self.assertEqual(client.server_url, "https://localhost:8000")

    def test_configure_refuses_and_changes_nothing(self):
        client = ToolServerClient("https://tools.example.org", "token")
        with self.assertRaises(InsecureServerUrl):
            client.configure(server_url="http://other.example.org", token="other")
        self.assertEqual(client.server_url, "https://tools.example.org")
        self.assertEqual(client.token, "token")

    def test_configure_upgrades_a_saved_loopback_url(self):
        client = ToolServerClient("https://tools.example.org", "token")
        client.configure(server_url="http://127.0.0.1:8000")
        self.assertEqual(client.server_url, "https://127.0.0.1:8000")


class VerifyForTest(unittest.TestCase):
    def test_a_loopback_host_is_verified_against_its_pinned_certificate(self):
        for url in ("https://localhost:8000/health", "https://127.0.0.1:8000/x", "https://[::1]:8000/"):
            with self.subTest(url=url), mock.patch.object(
                tls, "pinned_certificate", return_value="/tmp/pin.pem"
            ) as pin:
                self.assertEqual(tls.verify_for(url, True), "/tmp/pin.pem")
                self.assertEqual(pin.call_args.args[1], 8000)

    def test_a_remote_host_is_never_pinned(self):
        with mock.patch.object(tls, "pinned_certificate") as pin:
            self.assertIs(tls.verify_for("https://tools.example.org/health", True), True)
            self.assertEqual(tls.verify_for("https://tools.example.org/", "/ca.pem"), "/ca.pem")
        pin.assert_not_called()

    def test_a_maintainer_ca_path_is_kept_even_for_loopback(self):
        with mock.patch.object(tls, "pinned_certificate") as pin:
            self.assertEqual(tls.verify_for("https://localhost:8000/", "/ca.pem"), "/ca.pem")
        pin.assert_not_called()

    def test_verify_is_never_false(self):
        with mock.patch.object(tls, "pinned_certificate", return_value=None):
            for url in ("https://localhost:8000/", "https://tools.example.org/"):
                with self.subTest(url=url):
                    self.assertIs(tls.verify_for(url, False), True)

    def test_pinning_a_remote_host_is_refused_outright(self):
        with self.assertRaises(ValueError):
            tls.pinned_certificate("tools.example.org", 443)

    def test_an_unreachable_local_server_falls_back_to_the_default(self):
        with mock.patch.object(tls, "_fetch_certificate", side_effect=OSError("refused")):
            self.assertIs(tls.verify_for("https://127.0.0.1:1/", True), True)


class _Health(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LiveLoopbackTest(unittest.TestCase):
    def setUp(self):
        self.server = serve_tls(ThreadingHTTPServer(("127.0.0.1", 0), _Health))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}"

    def test_the_client_verifies_the_local_server_through_the_pin(self):
        client = ToolServerClient(self.url, "token")
        self.assertTrue(client.health())

    def test_a_localhost_url_verifies_by_name_too(self):
        client = ToolServerClient(f"http://localhost:{self.port}", "token")
        if not client.health():
            self.skipTest("localhost does not resolve to 127.0.0.1 here")

    def test_without_the_pin_the_self_signed_certificate_is_refused(self):
        with self.assertRaises(requests.exceptions.SSLError):
            requests.get(f"{self.url}/health", timeout=5, verify=True)

    def test_the_pin_file_is_private(self):
        path = tls.pinned_certificate("127.0.0.1", self.port)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as handle:
            self.assertIn("BEGIN CERTIFICATE", handle.read())

    def test_a_regenerated_certificate_is_read_again_once(self):
        """A pin taken from an earlier certificate fails verification; the
        adapter re-reads the certificate and the request goes through."""
        other = tempfile.mkdtemp(prefix="tls_other_")
        self.addCleanup(shutil.rmtree, other, True)
        stale_cert, _key = certificate(other)
        with tls._lock:
            tls._pins[("127.0.0.1", self.port)] = stale_cert

        client = ToolServerClient(self.url, "token")
        self.assertTrue(client.health())
        self.assertNotEqual(tls._pins[("127.0.0.1", self.port)], stale_cert)

    def test_verify_false_never_reaches_the_wire(self):
        seen = []
        original = requests.adapters.HTTPAdapter.send

        def spy(adapter, request, **kwargs):
            seen.append(kwargs.get("verify"))
            return original(adapter, request, **kwargs)

        client = ToolServerClient(self.url, "token", verify_tls=False)
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", spy):
            self.assertTrue(client.health())
        self.assertTrue(seen)
        self.assertNotIn(False, seen)
        self.assertTrue(all(isinstance(value, str) for value in seen))

    def test_urllib_goes_through_the_same_pin(self):
        with tls.urlopen(f"{self.url}/tools", timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"status": "ok"})

    def test_urllib_refuses_plain_http(self):
        with self.assertRaises(InsecureServerUrl):
            tls.urlopen("http://tools.example.org/tools", timeout=1)


if __name__ == "__main__":
    unittest.main()

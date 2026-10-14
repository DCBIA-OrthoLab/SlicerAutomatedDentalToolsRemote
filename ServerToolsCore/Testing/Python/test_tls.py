"""HTTPS only, verified against the public authorities, no knob (tls.py).

The live tests run a real TLS server on 127.0.0.1 whose `localhost`
certificate comes from a throwaway CA. Only the TEST trusts that CA, through
`REQUESTS_CA_BUNDLE`; the client itself passes verify=True as it always does.
"""

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from tls_fixture import serve_tls, trust_test_ca  # noqa: E402
from ServerToolsCoreLib import InsecureServerUrl, client as client_module, config, tls  # noqa: E402
from ServerToolsCoreLib.client import ToolServerClient, checked_server_url  # noqa: E402

_HTTP_URLS = (
    "http://localhost:8000",
    "http://127.0.0.1:8001",
    "http://[::1]:8000",
    "http://127.0.0.2:8000",
    "http://dev.localhost",
    "http://tools.example.org",
    "http://10.0.0.5:8000",
)


class RequireHttpsTest(unittest.TestCase):
    def test_https_is_kept_and_normalised(self):
        self.assertEqual(tls.require_https("https://tools.example.org/"), "https://tools.example.org")
        self.assertEqual(tls.require_https("HTTPS://tools.example.org:8443"), "https://tools.example.org:8443")

    def test_every_http_url_is_refused_loopback_included(self):
        for url in _HTTP_URLS:
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl) as caught:
                tls.require_https(url)
            self.assertIn("must start with https://", str(caught.exception))

    def test_anything_that_is_not_a_url_is_refused(self):
        for url in ("", "localhost:8000", "ftp://localhost", "https://", "tools.example.org",
                    "https://host:notaport"):
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl):
                tls.require_https(url)

    def test_checked_server_url_is_the_same_rule(self):
        self.assertEqual(checked_server_url("https://name.tailnet.ts.net/"), "https://name.tailnet.ts.net")
        for url in _HTTP_URLS:
            with self.subTest(url=url), self.assertRaises(ValueError):
                checked_server_url(url)

    def test_one_loopback_predicate(self):
        self.assertIs(client_module.is_loopback, tls.is_loopback)
        for host in ("localhost", "127.0.0.1", "127.0.0.2", "::1", "[::1]", "dev.localhost"):
            self.assertTrue(tls.is_loopback(host), host)
        for host in ("", None, "localhost.example.org", "10.0.0.5", "name.tailnet.ts.net"):
            self.assertFalse(tls.is_loopback(host), host)

    def test_the_default_is_https_and_a_placeholder(self):
        self.assertTrue(config.SERVER_URL.startswith("https://"))
        self.assertTrue(config.SERVER_URL.endswith(".invalid"))

    def test_there_is_no_verification_knob(self):
        self.assertFalse(hasattr(config, "VERIFY_TLS"))
        self.assertFalse(hasattr(ToolServerClient("https://a.example.org", "t"), "verify_tls"))
        with self.assertRaises(TypeError):
            ToolServerClient("https://a.example.org", "t", verify_tls="/etc/ssl/custom-ca.pem")
        with self.assertRaises(TypeError):
            ToolServerClient("https://a.example.org", "t").configure(verify_tls=False)


class ClientRefusesHttpTest(unittest.TestCase):
    def test_the_constructor_refuses_every_http_url(self):
        for url in _HTTP_URLS:
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl):
                ToolServerClient(url, "token")

    def test_configure_refuses_and_changes_nothing(self):
        client = ToolServerClient("https://tools.example.org", "token")
        for url in ("http://localhost:8000", "http://other.example.org"):
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl):
                client.configure(server_url=url, token="other")
        self.assertEqual(client.server_url, "https://tools.example.org")
        self.assertEqual(client.token, "token")


class _Health(BaseHTTPRequestHandler):
    requests_seen = []

    def log_message(self, *args):
        pass

    def _redirect(self):
        type(self).requests_seen.append((self.command, self.path))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.send_response(307)
        self.send_header("Location", "http://localhost:9/health")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._redirect()

    def do_HEAD(self):
        self._redirect()

    def do_GET(self):
        if self.path.startswith("/redirect"):
            return self._redirect()
        type(self).requests_seen.append((self.command, self.path))
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SessionRefusesPlainHttpTest(unittest.TestCase):
    def test_an_http_url_never_leaves_the_session(self):
        session = client_module._pooled_session()
        with mock.patch("socket.create_connection") as connect:
            for url in ("http://localhost:8000/health", "http://tools.example.org/run/x"):
                with self.subTest(url=url), self.assertRaises(client_module.PlainHttpRefused):
                    session.post(url, data=b"scan bytes")
        connect.assert_not_called()
        self.assertEqual(session.max_redirects, 0)

    def test_urllib_refuses_every_redirect(self):
        import urllib.error
        import urllib.request
        handler = tls._RefuseRedirects()
        request = urllib.request.Request("https://tools.example.org/tools")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 307, "Temporary Redirect", {},
                                     "http://tools.example.org/tools")
        with self.assertRaises(urllib.error.URLError):
            tls._opener(tls.ssl.create_default_context()).open("http://tools.example.org/", timeout=1)


class LiveServerTest(unittest.TestCase):
    def setUp(self):
        self.server = serve_tls(ThreadingHTTPServer(("127.0.0.1", 0), _Health))
        # Refused handshakes are the point of some tests here; not noise.
        self.server.handle_error = lambda *_args: None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"https://localhost:{self.server.server_address[1]}"

    def test_a_certificate_no_trusted_authority_signed_is_refused(self):
        """Without the test's CA, the client refuses the server outright:
        nothing in the production code trusts it."""
        with mock.patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": ""}):
            os.environ.pop("REQUESTS_CA_BUNDLE")
            self.assertFalse(ToolServerClient(self.url, "token").health())

    def test_a_trusted_certificate_is_accepted_with_the_hostname_checked(self):
        trust_test_ca(self)
        self.assertTrue(ToolServerClient(self.url, "token").health())
        # The certificate names `localhost`, not the address.
        other = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.assertFalse(ToolServerClient(other, "token").health())

    def test_every_request_passes_verify_true(self):
        trust_test_ca(self)
        seen = []
        original = requests.adapters.HTTPAdapter.send

        def spy(adapter, request, **kwargs):
            seen.append(kwargs.get("verify"))
            return original(adapter, request, **kwargs)

        client = ToolServerClient(self.url, "token")
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", spy), \
                mock.patch.object(requests.Session, "merge_environment_settings",
                                  lambda self, url, proxies, stream, verify, cert: {
                                      "proxies": {}, "stream": stream, "verify": verify, "cert": cert}):
            client.health()
            client.batch_policy()
        self.assertTrue(seen)
        self.assertTrue(all(value is True for value in seen), seen)

    def test_a_redirect_is_never_followed_whatever_the_method(self):
        trust_test_ca(self)
        _Health.requests_seen = []
        session = client_module._pooled_session()
        with self.assertRaises(client_module.RedirectRefused):
            session.get(f"{self.url}/redirect", verify=True)
        with self.assertRaises(client_module.RedirectRefused):
            session.post(f"{self.url}/run/x", data=b"scan bytes", verify=True)
        with self.assertRaises(client_module.RedirectRefused):
            session.head(f"{self.url}/redirect", verify=True, allow_redirects=False)
        # Each request reached the server once; nothing was resent anywhere.
        self.assertEqual([command for command, _path in _Health.requests_seen],
                         ["GET", "POST", "HEAD"])

    def test_a_redirected_client_call_is_a_failure_not_an_answer(self):
        trust_test_ca(self)
        client = ToolServerClient(self.url, "token")
        with mock.patch.object(client, "_server_url", f"{self.url}/redirect"):
            self.assertFalse(client.health())

    def test_the_range_probe_refuses_a_redirect(self):
        trust_test_ca(self)
        from ServerToolsCoreLib import transfer
        self.assertIsNone(transfer.probe_ranged(client_module._pooled_session(), f"{self.url}/redirect"))

    def test_urllib_refuses_plain_http(self):
        for url in ("http://localhost:8000/tools", "http://tools.example.org/tools"):
            with self.subTest(url=url), self.assertRaises(InsecureServerUrl):
                tls.urlopen(url, timeout=1)


if __name__ == "__main__":
    unittest.main()

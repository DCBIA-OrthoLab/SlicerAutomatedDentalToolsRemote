"""The certificate SlicerCloud makes for a server on this machine (local_cert.py).

    python3 -m unittest test_local_cert

No Slicer, no Qt. It runs both generators (`cryptography` and the `openssl`
command) when both exist, checks what lands on disk (modes, SAN, validity,
algorithm), and finishes with the real thing: a TLS server started with the
generated pair is reached by the client's pinned connection.
"""

import datetime
import json
import os
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "ServerToolsCore"))

sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "ServerToolsCore", "Testing", "Python"))

from tls_fixture import make_self_signed  # noqa: E402
from ServerToolsCoreLib import tls  # noqa: E402
from ServerToolsCoreLib.client import ToolServerClient  # noqa: E402
from SlicerCloudLib import local_cert  # noqa: E402
from SlicerCloudLib.local_cert import CertificateError, ensure_local_certificate  # noqa: E402

POSIX = os.name == "posix"

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False
HAVE_OPENSSL = shutil.which("openssl") is not None


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


def _modes(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Workspace(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="local_cert_")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.directory = os.path.join(self.work, "home", ".slicercloud", "tls")
        patcher = mock.patch.object(tls, "local_tls_dir", return_value=self.directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, **kwargs):
        return ensure_local_certificate(self.directory, **kwargs)


class GeneratedFilesTest(Workspace):
    def test_the_folder_and_the_files_are_private(self):
        pair = self.make()
        self.assertTrue(pair["created"])
        self.assertEqual(pair["cert"], os.path.join(self.directory, tls.LOCAL_CERT_NAME))
        self.assertEqual(pair["key"], os.path.join(self.directory, tls.LOCAL_KEY_NAME))
        if POSIX:
            self.assertEqual(_modes(self.directory), 0o700)
            self.assertEqual(_modes(pair["key"]), 0o600)
            self.assertEqual(_modes(pair["cert"]), 0o644)
            for path in (self.directory, pair["key"], pair["cert"]):
                self.assertEqual(os.stat(path).st_uid, os.geteuid())

    def test_nothing_else_is_left_in_the_folder(self):
        self.make()
        self.assertEqual(sorted(os.listdir(self.directory)),
                         sorted([tls.LOCAL_CERT_NAME, tls.LOCAL_KEY_NAME]))

    def test_the_key_is_created_with_its_final_mode(self):
        """Not written loose and chmod-ed afterwards: no moment when it is readable."""
        seen = {}
        real_open = os.open

        def spy(path, flags, mode=0o777, **kwargs):
            seen[os.path.basename(str(path))] = mode
            return real_open(path, flags, mode, **kwargs)

        with mock.patch("os.open", spy):
            self.make()
        key_modes = [mode for name, mode in seen.items() if name.startswith(tls.LOCAL_KEY_NAME)]
        self.assertEqual(key_modes, [0o600])

    def test_the_certificate_says_what_it_should(self):
        pair = self.make()
        decoded = local_cert._decode(pair["cert"])
        self.assertIsNotNone(decoded)
        self.assertEqual(frozenset(decoded["subjectAltName"]), local_cert.EXPECTED_SAN)
        starts = ssl.cert_time_to_seconds(decoded["notBefore"])
        ends = ssl.cert_time_to_seconds(decoded["notAfter"])
        days = (ends - starts) / 86400
        self.assertTrue(360 <= days <= 370, days)
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        self.assertLess(starts, now)
        self.assertGreater(ends, now + 300 * 86400)

    @unittest.skipUnless(HAVE_OPENSSL, "needs the openssl command to print the certificate")
    def test_algorithm_constraints_and_usage(self):
        pair = self.make()
        text = subprocess.run(["openssl", "x509", "-in", pair["cert"], "-noout", "-text"],
                              stdout=subprocess.PIPE, text=True, check=True).stdout
        self.assertIn("ecdsa-with-SHA256", text)
        self.assertRegex(text, r"prime256v1|P-256")
        self.assertIn("CA:FALSE", text)
        self.assertIn("TLS Web Server Authentication", text)
        self.assertNotIn("Certificate Sign", text)
        self.assertNotIn("DNS:", text)

    def test_the_key_belongs_to_the_certificate(self):
        pair = self.make()
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(pair["cert"], pair["key"])
        self.assertIsNone(local_cert.certificate_problem(self.directory))

    def test_the_client_accepts_the_file_as_a_pin(self):
        pair = self.make()
        self.assertEqual(tls.pinned_certificate("127.0.0.1"), pair["cert"])


class BothGeneratorsTest(Workspace):
    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "no cryptography module here")
    def test_cryptography_path(self):
        with mock.patch.object(local_cert, "_generate_with_openssl", side_effect=AssertionError("used")):
            pair = self.make()
        self.assertTrue(pair["created"])
        self.assertIsNone(local_cert.certificate_problem(self.directory))

    @unittest.skipUnless(HAVE_OPENSSL, "no openssl command here")
    def test_openssl_path(self):
        with mock.patch.object(local_cert, "_generate_with_cryptography", side_effect=ImportError):
            pair = self.make()
        self.assertTrue(pair["created"])
        self.assertIsNone(local_cert.certificate_problem(self.directory))
        if POSIX:
            self.assertEqual(_modes(pair["key"]), 0o600)
        # the scratch folder it worked in is gone
        self.assertEqual(sorted(os.listdir(self.directory)),
                         sorted([tls.LOCAL_CERT_NAME, tls.LOCAL_KEY_NAME]))

    def test_neither_available_says_so(self):
        with mock.patch.object(local_cert, "_generate_with_cryptography", side_effect=ImportError), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(CertificateError) as caught:
                self.make()
        self.assertIn("cryptography", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.directory, tls.LOCAL_KEY_NAME)))

    @unittest.skipUnless(HAVE_OPENSSL, "no openssl command here")
    def test_an_openssl_failure_is_reported_not_swallowed(self):
        failed = subprocess.CompletedProcess([], 1, "", "boom")
        with mock.patch.object(local_cert, "_generate_with_cryptography", side_effect=ImportError), \
                mock.patch("subprocess.run", return_value=failed):
            with self.assertRaises(CertificateError) as caught:
                self.make()
        self.assertIn("boom", str(caught.exception))

    @unittest.skipUnless(HAVE_OPENSSL, "no openssl command here")
    def test_the_environment_given_by_the_caller_reaches_openssl(self):
        seen = []
        real = subprocess.run

        def spy(command, **kwargs):
            seen.append(kwargs.get("env"))
            return real(command, **kwargs)

        env = dict(os.environ, MARKER="1")
        with mock.patch.object(local_cert, "_generate_with_cryptography", side_effect=ImportError), \
                mock.patch("subprocess.run", spy):
            self.make(subprocess_kwargs={"env": env})
        self.assertTrue(seen and all(e is env for e in seen))


class ReuseAndRenewalTest(Workspace):
    def test_a_good_pair_is_left_alone(self):
        first = self.make()
        before = [_read(first[k]) for k in ("cert", "key")]
        second = self.make()
        self.assertFalse(second["created"])
        self.assertEqual(before, [_read(second[k]) for k in ("cert", "key")])

    def test_without_the_decoder_a_good_pair_is_still_reused(self):
        """`ssl._ssl._test_decode_cert` is a CPython helper some builds may lack.
        Without it SAN and dates cannot be read back: the pair is reused when
        the key matches and the files are private (the handshake checks the
        rest), and still replaced when the key does not match."""
        first = self.make()
        with mock.patch.object(local_cert, "_decode", return_value=None):
            self.assertIsNone(local_cert.certificate_problem(self.directory))
            self.assertFalse(self.make()["created"])
            other = os.path.join(self.work, "other")
            ensure_local_certificate(other)
            shutil.copyfile(os.path.join(other, tls.LOCAL_KEY_NAME), first["key"])
            self.assertIn("does not belong", local_cert.certificate_problem(self.directory))

    def test_the_decoder_really_is_missing_in_that_case(self):
        with mock.patch.object(local_cert.ssl, "_ssl", object()):
            self.assertIsNone(local_cert._decode(self.make()["cert"]))

    def test_a_certificate_about_to_expire_is_replaced(self):
        first = self.make()
        old = _read(first["cert"])
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=345)
        self.assertEqual(local_cert.certificate_problem(self.directory, soon), "the certificate expires soon")
        again = self.make(now=soon)
        self.assertTrue(again["created"])
        self.assertNotEqual(old, _read(again["cert"]))

    def test_a_certificate_from_the_future_is_replaced(self):
        self.make()
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
        self.assertIn("not valid yet", local_cert.certificate_problem(self.directory, past))

    def test_a_key_that_does_not_match_is_replaced(self):
        first = self.make()
        other = os.path.join(self.work, "other")
        ensure_local_certificate(other)
        shutil.copyfile(os.path.join(other, tls.LOCAL_KEY_NAME), first["key"])
        self.assertIn("does not belong", local_cert.certificate_problem(self.directory))
        self.assertTrue(self.make()["created"])
        self.assertIsNone(local_cert.certificate_problem(self.directory))

    @unittest.skipUnless(POSIX, "mode bits")
    def test_a_key_others_can_read_is_replaced_not_trusted(self):
        first = self.make()
        old = _read(first["key"])
        os.chmod(first["key"], 0o644)
        self.assertIn("readable", local_cert.certificate_problem(self.directory))
        again = self.make()
        self.assertTrue(again["created"])
        self.assertNotEqual(old, _read(again["key"]))
        self.assertEqual(_modes(again["key"]), 0o600)

    def test_a_certificate_naming_other_hosts_is_replaced(self):
        first = self.make()
        cert, key = make_self_signed(self.work, "wide", "IP:127.0.0.1,IP:::1,DNS:evil.example.org")
        shutil.copyfile(cert, first["cert"])
        shutil.copyfile(key, first["key"])
        os.chmod(first["key"], 0o600)
        self.assertIn("other than this computer", local_cert.certificate_problem(self.directory))
        self.assertTrue(self.make()["created"])
        self.assertIsNone(local_cert.certificate_problem(self.directory))

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_a_folder_that_is_a_link_is_refused(self):
        target = os.path.join(self.work, "elsewhere")
        os.makedirs(target)
        os.makedirs(os.path.dirname(self.directory))
        os.symlink(target, self.directory)
        with self.assertRaises(CertificateError):
            self.make()
        self.assertEqual(os.listdir(target), [])

    @unittest.skipUnless(POSIX, "mode bits")
    def test_a_loose_folder_is_tightened(self):
        os.makedirs(self.directory, mode=0o755)
        os.chmod(self.directory, 0o755)
        self.make()
        self.assertEqual(_modes(self.directory), 0o700)


class _Health(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EndToEndTest(Workspace):
    """A server started with the generated pair, the client pinned to it."""

    def serve(self, pair, host="127.0.0.1"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(pair["cert"], pair["key"])
        server = ThreadingHTTPServer((host, 0), _Health)
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        server.handle_error = lambda *_args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _verifies(self):
        pair = self.make()
        port = self.serve(pair)
        for host in ("127.0.0.1",):
            with self.subTest(host=host):
                self.assertTrue(ToolServerClient(f"https://{host}:{port}", "t").health())

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, "no cryptography module here")
    def test_cryptography_certificate_verifies_on_every_covered_address(self):
        self._verifies()

    @unittest.skipUnless(HAVE_OPENSSL, "no openssl command here")
    def test_openssl_certificate_verifies_on_every_covered_address(self):
        with mock.patch.object(local_cert, "_generate_with_cryptography", side_effect=ImportError):
            self._verifies()

    def test_ipv6_loopback_verifies_where_the_machine_has_it(self):
        pair = self.make()
        try:
            class V6(ThreadingHTTPServer):
                address_family = __import__("socket").AF_INET6
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(pair["cert"], pair["key"])
            server = V6(("::1", 0), _Health)
        except OSError:
            self.skipTest("no IPv6 loopback on this machine")
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        server.handle_error = lambda *_args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertTrue(ToolServerClient(f"https://[::1]:{server.server_address[1]}", "t").health())

    def test_a_regenerated_pair_is_a_different_pin(self):
        """Replacing the certificate invalidates what a server still running
        with the old one presents: the client refuses it until it restarts."""
        old = self.make()
        port = self.serve(old)
        self.assertTrue(ToolServerClient(f"https://127.0.0.1:{port}", "t").health())
        os.unlink(old["cert"])
        os.unlink(old["key"])
        self.make()
        self.assertFalse(ToolServerClient(f"https://127.0.0.1:{port}", "t").health())


if __name__ == "__main__":
    unittest.main()

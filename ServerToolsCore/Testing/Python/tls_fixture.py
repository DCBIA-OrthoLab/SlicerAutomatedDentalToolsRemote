"""A throwaway certificate authority for the tests that run a real server.

The client speaks HTTPS only and verifies every certificate against the public
authorities, with no knob to change that. So a test server on 127.0.0.1 gets a
certificate for `localhost` issued by a throwaway CA made here, and the TEST --
never the production code -- points requests at that CA through the
`REQUESTS_CA_BUNDLE` environment variable for its own duration. The client
still passes `verify=True` and checks the hostname, exactly as in production.
"""

import atexit
import os
import shutil
import ssl
import subprocess
import tempfile
from unittest import mock

_DIRECTORY = None


def _openssl(*args):
    subprocess.run(["openssl", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _generate(directory):
    path = lambda name: os.path.join(directory, name)  # noqa: E731
    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    _openssl("req", "-x509", *ec, "-keyout", path("ca.key"), "-out", path("ca.pem"),
             "-days", "2", "-subj", "/CN=throwaway test CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    _openssl("req", *ec, "-keyout", path("key.pem"), "-out", path("server.csr"),
             "-subj", "/CN=localhost")
    with open(path("ext.cnf"), "w") as handle:
        handle.write("subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\n"
                     "extendedKeyUsage=serverAuth\n")
    _openssl("x509", "-req", "-in", path("server.csr"), "-CA", path("ca.pem"),
             "-CAkey", path("ca.key"), "-CAcreateserial", "-out", path("cert.pem"),
             "-days", "2", "-extfile", path("ext.cnf"))


def _directory():
    global _DIRECTORY
    if _DIRECTORY is None:
        _DIRECTORY = tempfile.mkdtemp(prefix="tls_fixture_")
        atexit.register(shutil.rmtree, _DIRECTORY, True)
        _generate(_DIRECTORY)
    return _DIRECTORY


def ca_bundle():
    return os.path.join(_directory(), "ca.pem")


def serve_tls(server):
    """Wrap a socketserver's listening socket in TLS, in place, with the
    `localhost` certificate. The handshake is deferred to the first read, in
    the handler thread, so one client failing it never blocks serve_forever.
    """
    directory = _directory()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem"))
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server


def trust_test_ca(testcase):
    """For this test only, make requests trust the throwaway CA."""
    patcher = mock.patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": ca_bundle()})
    patcher.start()
    testcase.addCleanup(patcher.stop)

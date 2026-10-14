"""A throwaway self-signed certificate for the tests that run a real server.

The client speaks HTTPS only, so a test server on 127.0.0.1 has to as well.
The certificate has the shape the local deployment's has (EC P-256, CA:FALSE,
SAN localhost / 127.0.0.1 / ::1), so the client verifies it through the same
loopback pin it uses in production -- there is no test-only bypass.
"""

import os
import shutil
import ssl
import subprocess
import tempfile

_DIRECTORY = None


def certificate(directory=None):
    """(cert_path, key_path) of a fresh self-signed loopback certificate.

    Without `directory`, one certificate is made per process and reused.
    """
    global _DIRECTORY
    if directory is None:
        if _DIRECTORY is None:
            _DIRECTORY = tempfile.mkdtemp(prefix="tls_fixture_")
            import atexit
            atexit.register(shutil.rmtree, _DIRECTORY, True)
            _generate(_DIRECTORY)
        directory = _DIRECTORY
    else:
        _generate(directory)
    return os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")


def _generate(directory):
    subprocess.run(
        [
            "openssl", "req", "-x509", "-nodes", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-keyout", os.path.join(directory, "key.pem"),
            "-out", os.path.join(directory, "cert.pem"),
            "-days", "2", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
            "-addext", "basicConstraints=critical,CA:FALSE",
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def serve_tls(server, directory=None):
    """Wrap a socketserver's listening socket in TLS, in place.

    The handshake is deferred to the first read, which happens in the handler
    thread, so one client failing it never blocks `serve_forever`.
    """
    cert, key = certificate(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server

"""The self-signed certificate a server on THIS machine is started with.

Imports neither `slicer` nor `qt` (ARCHITECTURE.md's dependency rule): standard
library, plus `cryptography` when the interpreter has it, plus the `openssl`
command otherwise.

What it writes, and where (`ServerToolsCoreLib.tls.local_tls_dir()`, a fixed
folder under the user's home):

    server.key   private key, mode 0600, never leaves the folder
    server.crt   the certificate, mode 0644: the PIN. The client verifies the
                 local server against this file and nothing else.

The certificate is made HERE, by the user's own SlicerCloud, and handed to the
server as a path to read. Nothing in this module or in the client ever learns a
certificate from a server. That is the whole point of pinning out of band:
see `ServerToolsCoreLib/tls.py`, rule 3.

Shape of the certificate: ECDSA P-256 with SHA-256, a subject alternative name
of exactly 127.0.0.1 and ::1, `CA:FALSE`, serverAuth only, valid for
one year and replaced when fewer than `RENEW_BEFORE_DAYS` remain.
"""

import datetime
import ipaddress
import os
import shutil
import ssl
import stat
import subprocess
import tempfile

from ServerToolsCoreLib import tls

VALID_DAYS = 365
RENEW_BEFORE_DAYS = 30
COMMON_NAME = "SlicerCloud local server"
#: What the SAN must be, exactly, as `ssl` decodes it (IPv6 comes back expanded).
EXPECTED_SAN = frozenset({
    ("IP Address", "127.0.0.1"),
    ("IP Address", "0:0:0:0:0:0:0:1"),
})


class CertificateError(Exception):
    """The certificate could not be made; the message says what to do."""


# -- checking an existing pair ---------------------------------------------

def _decode(path):
    """The certificate as `ssl` decodes it, or None where that is not possible.
    `_test_decode_cert` is CPython's own helper (it is what the stdlib tests
    use); if a build lacks it the caller falls back to the TLS handshake."""
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decoder is None:
        return None
    try:
        return decoder(path)
    except (OSError, ValueError):
        return None


def certificate_problem(directory=None, now=None):
    """Why the pair in `directory` cannot be used as is, or None if it can.

    Checks, in order: both files exist as real files owned by this user, the
    key is not readable by anyone else, the key belongs to the certificate, the
    SAN is exactly `EXPECTED_SAN`, and at least `RENEW_BEFORE_DAYS` of validity
    remain.
    """
    directory = directory or tls.local_tls_dir()
    cert = os.path.join(directory, tls.LOCAL_CERT_NAME)
    key = os.path.join(directory, tls.LOCAL_KEY_NAME)
    try:
        key_info = os.lstat(key)
        os.lstat(cert)
    except OSError:
        return "no certificate yet"
    if not stat.S_ISREG(key_info.st_mode):
        return "the private key is not a regular file"
    if os.name != "nt" and (key_info.st_uid != os.geteuid() or key_info.st_mode & 0o077):
        return "the private key is readable by other accounts"
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
    except (ssl.SSLError, OSError):
        return "the private key does not belong to the certificate"
    decoded = _decode(cert)
    if decoded is None:
        return None
    if frozenset(decoded.get("subjectAltName", ())) != EXPECTED_SAN:
        return "the certificate names hosts other than this computer"
    expires = ssl.cert_time_to_seconds(decoded["notAfter"])
    starts = ssl.cert_time_to_seconds(decoded["notBefore"])
    moment = (now or datetime.datetime.now(datetime.timezone.utc)).timestamp()
    if starts > moment + 86400:
        return "the certificate is not valid yet (check the clock)"
    if expires - moment < RENEW_BEFORE_DAYS * 86400:
        return "the certificate expires soon"
    return None


# -- making one --------------------------------------------------------------

def _private_directory(directory):
    """`directory` as a real folder, mode 0700, owned by this user."""
    if os.path.islink(directory):
        raise CertificateError(f"{directory} is a symbolic link; refusing to put a private key there.")
    parent = os.path.dirname(directory)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.name != "nt":
        info = os.lstat(directory)
        if info.st_uid != os.geteuid():
            raise CertificateError(f"{directory} belongs to another account.")
        os.chmod(directory, 0o700)


def _write(path, data: bytes, mode: int):
    """Write `data` to `path` atomically with `mode` from its first byte."""
    temporary = f"{path}.{os.getpid()}.new"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if os.name != "nt":
        os.chmod(path, mode)


def _generate_with_cryptography(now):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    start = now.replace(tzinfo=None) - datetime.timedelta(days=1)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + datetime.timedelta(days=VALID_DAYS + 1))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("::1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()),
    )


_OPENSSL_CONFIG = f"""[req]
distinguished_name = dn
prompt = no
x509_extensions = ext
[dn]
CN = {COMMON_NAME}
[ext]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1,IP:::1
"""


def _generate_with_openssl(directory, subprocess_kwargs):
    """The same certificate through the `openssl` command, for an interpreter
    without `cryptography`. Uses only options OpenSSL and the LibreSSL that
    macOS ships both have (`ecparam` then `req -newkey ec:<file>`, a config file
    rather than `-addext`). Runs in a scratch folder inside the 0700 one."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise CertificateError(
            "Cannot create the local server's certificate: this Python has no 'cryptography' "
            "module and the 'openssl' command was not found.")
    scratch = tempfile.mkdtemp(prefix="gen-", dir=directory)
    try:
        path = lambda name: os.path.join(scratch, name)  # noqa: E731
        with open(path("req.cnf"), "w", encoding="ascii") as handle:
            handle.write(_OPENSSL_CONFIG)
        steps = (
            [openssl, "ecparam", "-name", "prime256v1", "-out", path("params.pem")],
            [openssl, "req", "-x509", "-newkey", "ec:" + path("params.pem"), "-nodes",
             "-keyout", path("key.pem"), "-out", path("cert.pem"), "-days", str(VALID_DAYS),
             "-sha256", "-config", path("req.cnf")],
        )
        for command in steps:
            try:
                done = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, timeout=60, check=False, **subprocess_kwargs)
            except (OSError, subprocess.SubprocessError) as exc:
                raise CertificateError(f"Could not run openssl: {exc}") from None
            if done.returncode != 0:
                raise CertificateError("openssl could not create the certificate: "
                                       + (done.stderr or done.stdout).strip()[-300:])
        with open(path("cert.pem"), "rb") as cert, open(path("key.pem"), "rb") as key:
            return cert.read(), key.read()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def ensure_local_certificate(directory=None, now=None, subprocess_kwargs=None, progress_cb=None):
    """A usable certificate and key in the private folder, made if needed.

    Returns `{"cert": path, "key": path, "created": bool}`. An existing pair
    that `certificate_problem` accepts is left alone; anything else is replaced
    (key first, then certificate, each atomically), so a half-written pair is
    never left behind for the client to pin.
    """
    directory = directory or tls.local_tls_dir()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cert = os.path.join(directory, tls.LOCAL_CERT_NAME)
    key = os.path.join(directory, tls.LOCAL_KEY_NAME)
    problem = certificate_problem(directory, now)
    if problem is None:
        return {"cert": cert, "key": key, "created": False}

    if progress_cb:
        progress_cb(f"Creating the certificate for the local server ({problem}).")
    _private_directory(directory)
    try:
        cert_pem, key_pem = _generate_with_cryptography(now)
    except ImportError:
        cert_pem, key_pem = _generate_with_openssl(directory, subprocess_kwargs or {})
    _write(key, key_pem, 0o600)
    _write(cert, cert_pem, 0o644)
    problem = certificate_problem(directory, now)
    if problem:
        raise CertificateError(f"The certificate just created is not usable: {problem}.")
    return {"cert": cert, "key": key, "created": True}

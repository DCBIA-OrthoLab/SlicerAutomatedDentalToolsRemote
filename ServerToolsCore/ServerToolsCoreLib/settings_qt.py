"""Persists a user-editable override of config.py's defaults via Slicer's
native qt.QSettings (an ini/plist file on disk, independent of the Slicer
process - survives restarts).

Not imported by `__init__.py` on purpose: it depends on `qt`, and the package
must stay importable outside Slicer for client.py's unit tests. Only Slicer-
side code (ServerToolsCore.py at startup, ServerToolsSettingsWidget) uses it.

`config.py` remains the compiled-in defaults; this module is the optional
layer on top, edited via the "Server Tools Settings" module.
"""

import qt

_GROUP = "ServerTools"
_KEY_SERVER_URL = f"{_GROUP}/ServerUrl"
_KEY_API_TOKEN = f"{_GROUP}/ApiToken"
_KEY_VERIFY_TLS = f"{_GROUP}/VerifyTls"
_KEY_TIMEOUT = f"{_GROUP}/Timeout"


def load_overrides() -> dict:
    """Return only the settings the user has actually saved, e.g. {} if none."""
    settings = qt.QSettings()
    overrides = {}
    if settings.contains(_KEY_SERVER_URL):
        overrides["server_url"] = str(settings.value(_KEY_SERVER_URL))
    if settings.contains(_KEY_API_TOKEN):
        overrides["token"] = str(settings.value(_KEY_API_TOKEN))
    # `verify_tls` is deliberately NOT read back. It was a saved setting until
    # 2026-10-13, and a machine that had stored False would otherwise keep
    # certificate verification off for ever -- the one setting a user must not
    # be able to turn off on a client that sends medical images. `config.py`
    # owns it now, and a stale key in the file decides nothing.
    if settings.contains(_KEY_TIMEOUT):
        overrides["timeout"] = int(settings.value(_KEY_TIMEOUT, 600))
    return overrides


def save_overrides(server_url: str, token: str, timeout: int) -> None:
    """Persist what a user may change. Certificate verification is not in that
    set -- see config.VERIFY_TLS."""
    settings = qt.QSettings()
    settings.setValue(_KEY_SERVER_URL, server_url)
    settings.setValue(_KEY_API_TOKEN, token)
    settings.setValue(_KEY_TIMEOUT, timeout)
    # Drop the key this used to write, so a value an older client saved stops
    # sitting in the file looking as though it still decided something.
    settings.remove(_KEY_VERIFY_TLS)
    settings.sync()


def clear_overrides() -> None:
    settings = qt.QSettings()
    settings.remove(_GROUP)
    settings.sync()


def apply_saved_overrides(client) -> bool:
    """Apply whatever was saved onto `client` (e.g. get_client()). Returns
    whether an override was found and applied - used at Slicer startup."""
    overrides = load_overrides()
    if overrides:
        client.configure(**overrides)
    return bool(overrides)

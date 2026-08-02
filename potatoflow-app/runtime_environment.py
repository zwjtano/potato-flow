"""Shared runtime setup for the unified Linux service and its bridge jobs."""

from __future__ import annotations

import os
import platform
import ssl
from pathlib import Path


LINUX_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/ca-bundle.pem",
)


def configure_linux_ca_environment() -> str | None:
    """Make all Python/curl subprocesses use the Linux system trust store."""
    if platform.system() != "Linux":
        return None
    for variable in (
        "BILIBILI_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
    ):
        configured = os.environ.get(variable, "").strip()
        if configured and Path(configured).is_file():
            bundle = str(Path(configured).expanduser().resolve())
            break
    else:
        candidates: list[str] = []
        default_cafile = ssl.get_default_verify_paths().cafile
        if default_cafile:
            candidates.append(default_cafile)
        candidates.extend(LINUX_CA_BUNDLES)
        bundle = next(
            (str(Path(path).resolve()) for path in candidates if Path(path).is_file()),
            "",
        )
    if not bundle:
        return None
    os.environ.setdefault("BILIBILI_CA_BUNDLE", bundle)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
    os.environ.setdefault("CURL_CA_BUNDLE", bundle)
    os.environ.setdefault("SSL_CERT_FILE", bundle)
    return bundle

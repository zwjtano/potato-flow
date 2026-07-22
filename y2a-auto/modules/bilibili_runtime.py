#!/usr/bin/env python
# -*- coding: utf-8 -*-

import hashlib
import logging
import os
import pathlib
import platform
import re
import ssl
import subprocess
import tempfile
from typing import Optional

logger = logging.getLogger("bilibili_runtime")

_INITIALIZED = False
_LAST_ERROR: Optional[str] = None
_PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


def _existing_ca_bundle_from_env() -> Optional[str]:
    for name in (
        "BILIBILI_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    ):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        path = pathlib.Path(value).expanduser()
        if path.is_file():
            return str(path.resolve())
        logger.warning("忽略不存在的 %s 证书文件: %s", name, path)
    return None


def _read_macos_keychain_certificates() -> list[bytes]:
    command = (
        "security",
        "find-certificate",
        "-a",
        "-p",
        "/Library/Keychains/System.keychain",
    )
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return _PEM_CERTIFICATE.findall(result.stdout)


def _find_linux_ca_bundle() -> Optional[str]:
    candidates = []
    try:
        default_cafile = ssl.get_default_verify_paths().cafile
        if default_cafile:
            candidates.append(default_cafile)
    except (AttributeError, OSError):
        pass
    candidates.extend(
        (
            "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu
            "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / CentOS / Fedora
            "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
            "/etc/ssl/ca-bundle.pem",  # openSUSE
        )
    )
    for candidate in candidates:
        path = pathlib.Path(candidate)
        if path.is_file():
            return str(path.resolve())
    return None


def resolve_bilibili_ca_bundle() -> Optional[str]:
    """Return a CA bundle usable by curl_cffi without disabling TLS checks."""
    explicit_bundle = _existing_ca_bundle_from_env()
    if explicit_bundle:
        return explicit_bundle

    try:
        import certifi

        certifi_path = pathlib.Path(certifi.where())
        base_certificates = _PEM_CERTIFICATE.findall(certifi_path.read_bytes())
    except (ImportError, OSError):
        return None

    system_name = platform.system()
    if system_name == "Linux":
        return _find_linux_ca_bundle() or str(certifi_path)
    if system_name != "Darwin":
        return str(certifi_path)

    certificates = base_certificates + _read_macos_keychain_certificates()
    unique_certificates: list[bytes] = []
    seen = set()
    for certificate in certificates:
        digest = hashlib.sha256(certificate).digest()
        if digest in seen:
            continue
        seen.add(digest)
        unique_certificates.append(certificate)

    # certifi alone is still the safest fallback if Keychain export is unavailable.
    if len(unique_certificates) <= len(base_certificates):
        return str(certifi_path)

    content = b"\n".join(unique_certificates) + b"\n"
    content_digest = hashlib.sha256(content).hexdigest()[:16]
    cache_dir = pathlib.Path(tempfile.gettempdir()) / "y2a-auto"
    bundle_path = cache_dir / f"bilibili-ca-{content_digest}.pem"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not bundle_path.exists():
            temporary_path = bundle_path.with_suffix(".tmp")
            temporary_path.write_bytes(content)
            temporary_path.replace(bundle_path)
        return str(bundle_path)
    except OSError as exc:
        logger.warning("生成 macOS Bilibili CA 证书包失败: %s", exc)
        return str(certifi_path)


def configure_bilibili_runtime() -> bool:
    """Configure the internal Bilibili SDK network runtime once per process."""
    global _INITIALIZED, _LAST_ERROR
    if _INITIALIZED:
        return True

    try:
        from .bili_sdk import request_settings

        ca_bundle = resolve_bilibili_ca_bundle()
        if ca_bundle:
            request_settings.set("verify_ssl", ca_bundle)
        impersonate = os.environ.get("BILIBILI_IMPERSONATE", "chrome131").strip()
        if impersonate:
            request_settings.set("impersonate", impersonate)
        _INITIALIZED = True
        _LAST_ERROR = None
        return True
    except Exception as exc:
        _LAST_ERROR = str(exc)
        logger.warning("配置 bilibili-api 网络运行时失败: %s", exc)
        return False


def get_bilibili_runtime_error() -> Optional[str]:
    return _LAST_ERROR

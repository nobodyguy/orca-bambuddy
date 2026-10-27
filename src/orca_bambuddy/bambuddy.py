"""Thin client for the Bambuddy REST API (standard library only).

Verified against Bambuddy 1.2.5.7 (tag v1.2.5.7, git 188df76): every path, field and
error shape used here was read from backend/app/api/routes/* and checked against a
running 1.2.5.7 instance (also works with 1.3.0b1). See NOTES.md for the endpoint list.

Nothing in this module knows about OrcaSlicer, so it can be tested on its own.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

API_PREFIX = "/api/v1"

# The permissions /auth/me must report for each feature, and the API-key flag that
# grants them (backend/app/core/auth.py, _APIKEY_SCOPE_BY_PERMISSION).
REQUIRED_PERMISSIONS = {
    "printers:read": ("can_read_status", "list printers and show their status"),
    "queue:read": ("can_read_status", "follow queued jobs"),
    "camera:view": ("can_read_status", "show camera snapshots"),
    "library:upload": ("can_manage_library", "upload files"),
    "queue:create": ("can_queue", "queue and start prints"),
}
# How Bambuddy's Settings > API Keys dialog labels each flag.
SCOPE_LABELS = {"can_read_status": "Read Status", "can_queue": "Manage Queue", "can_manage_library": "Manage Library"}

# Headers the plugin sets itself; extra headers must not replace them.
RESERVED_HEADERS = frozenset(
    h.lower()
    for h in ("X-API-Key", "Accept", "Content-Type", "Content-Length", "Transfer-Encoding", "Host",
              "Connection", "CF-Access-Client-Id", "CF-Access-Client-Secret")
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

# Cloudflare's free plan rejects request bodies above 100 MB with HTTP 413.
CLOUDFLARE_FREE_UPLOAD_LIMIT_MB = 100

# AppImage builds of OrcaSlicer bundle a Python whose OpenSSL looks for CA files at a
# path from the build machine, so it starts with an empty trust store. These are the
# usual system bundles; the first one that exists is loaded.
SYSTEM_CA_FILES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Gentoo
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/usr/local/etc/openssl@3/cert.pem",
)


class BambuddyError(Exception):
    """A failure with a message that can be shown to the user as is.

    ``kind`` lets callers react to a class of failure: unreachable, timeout, tls,
    bad_url, auth, permission, cloudflare, proxy, too_large, not_found, bad_request,
    server, cancelled.
    """

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Connection:
    base_url: str
    api_key: str = ""
    cf_client_id: str = ""
    cf_client_secret: str = ""
    verify_tls: bool = True
    ca_file: str = ""
    timeout: float = 15.0
    upload_timeout: float = 120.0
    # Sent with every request, for reverse proxies that want their own header (name, value).
    extra_headers: tuple = ()
    header_problems: tuple = ()  # lines of the setting that were skipped, for Test connection

    @property
    def uses_cloudflare_access(self) -> bool:
        return bool(self.cf_client_id and self.cf_client_secret)


def parse_extra_headers(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse "Name: value" lines into headers, plus a list of problems.

    Problems name the line and header, never the value: values are secrets.
    """
    headers: list[tuple[str, str]] = []
    problems: list[str] = []
    seen = set()
    for number, line in enumerate((text or "").splitlines(), start=1):
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not _HEADER_NAME.match(name):
            problems.append(f"Extra header line {number} is not 'Name: value'.")
        elif name.lower() in RESERVED_HEADERS:
            problems.append(f"Extra header {name} is set by the plugin itself and cannot be replaced.")
        elif not value or any(c in value for c in "\r\n\0"):
            problems.append(f"Extra header {name} has no usable value.")
        elif name.lower() in seen:
            problems.append(f"Extra header {name} is listed twice.")
        else:
            seen.add(name.lower())
            headers.append((name, value))
    return headers, problems


def normalize_base_url(url: str) -> str:
    """Accept what users paste: a bare host, a trailing slash or an /api/v1 suffix."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    if url.endswith(API_PREFIX):
        url = url[: -len(API_PREFIX)]
    return url


def redact(text: str) -> str:
    """Strip query tokens from URLs before they reach a log line."""
    return re.sub(r"(token=)[^&\s]+", r"\1***", text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would resend X-API-Key and the Cloudflare secret to wherever a redirect
    # points (Cloudflare Access redirects to its login host). Surface 3xx instead.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ssl_context(conn: Connection) -> ssl.SSLContext:
    if not conn.verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if conn.ca_file:
        return ssl.create_default_context(cafile=conn.ca_file)
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for path in SYSTEM_CA_FILES:
            if os.path.isfile(path):
                ctx.load_verify_locations(cafile=path)
                break
    return ctx


@dataclass
class _Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def _is_cloudflare_access_redirect(location: str) -> bool:
    return "cloudflareaccess.com" in location or "/cdn-cgi/access/" in location


def _looks_like_cloudflare(headers: dict, body: bytes) -> bool:
    ctype = headers.get("content-type", "")
    if "json" in ctype:
        return False
    sample = body[:20000].lower()
    return b"cloudflare" in sample or "cf-ray" in headers


def _detail(body: bytes) -> str:
    try:
        detail = json.loads(body.decode("utf-8")).get("detail")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ""
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(str(d.get("msg", d)) for d in detail if isinstance(d, dict)) or str(detail)
    return str(detail) if detail else ""


class BambuddyClient:
    def __init__(self, conn: Connection):
        self.conn = conn
        self.base_url = normalize_base_url(conn.base_url)
        handlers = [_NoRedirect(), urllib.request.HTTPSHandler(context=_ssl_context(conn))]
        self._opener = urllib.request.build_opener(*handlers)
        self._camera_token: tuple[str, float] | None = None
        self.behind_cloudflare = conn.uses_cloudflare_access

    # ---- plumbing -------------------------------------------------------------

    def web_url(self, path: str = "/") -> str:
        """A link into the Bambuddy web UI."""
        return self.base_url + path

    def _headers(self, auth: bool = True) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "orca-bambuddy-plugin"}
        if auth and self.conn.api_key:
            headers["X-API-Key"] = self.conn.api_key
        # Cloudflare Access sits in front of every path, the public ones included.
        if self.conn.uses_cloudflare_access:
            headers["CF-Access-Client-Id"] = self.conn.cf_client_id
            headers["CF-Access-Client-Secret"] = self.conn.cf_client_secret
        for name, value in self.conn.extra_headers:
            headers[name] = value
        return headers

    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.base_url + API_PREFIX + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body=None,
        data=None,
        headers: dict | None = None,
        auth: bool = True,
        timeout: float | None = None,
    ) -> _Response:
        if not self.base_url:
            raise BambuddyError("bad_url", "The Bambuddy URL is not set.")
        all_headers = self._headers(auth)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        if headers:
            all_headers.update(headers)
        url = self._url(path, params)
        req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout or self.conn.timeout) as resp:
                result = _Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as e:
            body = e.read() if e.fp is not None else b""
            result = _Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body)
        except BambuddyError:
            raise
        except Exception as e:  # noqa: BLE001 - every transport failure is mapped to a message
            raise self._transport_error(e) from e
        if "cf-ray" in result.headers:
            self.behind_cloudflare = True
        if result.status >= 300:
            raise self._http_error(result)
        return result

    def _json(self, method: str, path: str, **kwargs):
        resp = self._request(method, path, **kwargs)
        try:
            return resp.json()
        except (ValueError, UnicodeDecodeError):
            if _looks_like_cloudflare(resp.headers, resp.body):
                raise self._cloudflare_error() from None
            raise BambuddyError(
                "bad_url",
                f"{self.base_url} answered, but not with the Bambuddy API. "
                "Check the URL: it is the address you open Bambuddy at, e.g. http://192.168.1.50:8000.",
                resp.status,
            ) from None

    def _transport_error(self, e: Exception) -> BambuddyError:
        reason = e.reason if isinstance(e, urllib.error.URLError) else e
        host = urllib.parse.urlsplit(self.base_url).netloc
        if isinstance(reason, ssl.SSLCertVerificationError):
            return BambuddyError(
                "tls",
                f"The TLS certificate of {host} is not trusted ({reason.verify_message}). "
                "For a self-signed certificate set 'CA file', or turn off 'Verify TLS certificate'.",
            )
        if isinstance(reason, ssl.SSLError):
            return BambuddyError("tls", f"TLS connection to {host} failed: {reason}. Is the URL http or https?")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return BambuddyError("timeout", f"{host} did not answer in time. Is Bambuddy running and reachable?")
        if isinstance(reason, socket.gaierror):
            return BambuddyError("unreachable", f"Host name {host.split(':')[0]} could not be resolved. Check the URL.")
        if isinstance(reason, ConnectionRefusedError):
            return BambuddyError("unreachable", f"Connection to {host} refused. Is Bambuddy running on that port?")
        if isinstance(reason, OSError):
            return BambuddyError("unreachable", f"Cannot reach {host}: {reason.strerror or reason}.")
        return BambuddyError("unreachable", f"Cannot reach {host}: {reason}.")

    def _cloudflare_error(self, status: int | None = None) -> BambuddyError:
        if self.conn.uses_cloudflare_access:
            msg = (
                "Cloudflare Access rejected the service token. Check the client ID and secret, and that the "
                "Access application has a policy with the 'Service Auth' action that includes this token."
            )
        else:
            msg = (
                "Cloudflare Access returned its login page instead of the Bambuddy API. This server is behind "
                "Cloudflare Zero Trust: create a service token and enter its client ID and secret in the settings."
            )
        return BambuddyError("cloudflare", msg, status)

    def _http_error(self, resp: _Response) -> BambuddyError:
        status = resp.status
        location = resp.headers.get("location", "")
        if 300 <= status < 400:
            if _is_cloudflare_access_redirect(location):
                return self._cloudflare_error(status)
            here, there = urllib.parse.urlsplit(self.base_url), urllib.parse.urlsplit(location)
            if here.scheme == "http" and there.scheme == "https" and here.hostname == there.hostname:
                # Typical behind Cloudflare or a reverse proxy: plain HTTP is redirected to HTTPS.
                secure = urllib.parse.urlunsplit(("https", there.netloc, "", "", ""))
                return BambuddyError(
                    "bad_url", f"The server accepts only HTTPS. Change the Bambuddy URL to {secure}.", status
                )
            return BambuddyError(
                "bad_url",
                f"The server redirected to {redact(location) or 'another address'}. If that is the login "
                "page of a reverse proxy, add the headers it expects under 'Extra HTTP headers'; otherwise "
                "use the final address as the Bambuddy URL (check http vs https).",
                status,
            )
        if status in (401, 403) and _looks_like_cloudflare(resp.headers, resp.body):
            return self._cloudflare_error(status)
        detail = _detail(resp.body)
        if status in (401, 403, 407) and not detail:
            # Bambuddy answers auth failures with a JSON "detail"; a bare page comes from a proxy.
            return BambuddyError(
                "proxy",
                f"A proxy in front of Bambuddy refused the request (HTTP {status}). Add the headers it "
                "expects under 'Extra HTTP headers', or check the ones you entered.",
                status,
            )
        if status == 401:
            return BambuddyError("auth", "The API key was rejected. Create a new key in Bambuddy (Settings > API Keys).", status)
        if status == 403:
            return BambuddyError("permission", f"Bambuddy refused the request: {detail or 'permission denied'}.", status)
        if status == 404:
            return BambuddyError("not_found", detail or "Not found.", status)
        if status == 413:
            return BambuddyError(
                "too_large",
                "The file is too large for the server or proxy (HTTP 413). Cloudflare's free plan accepts "
                f"uploads up to {CLOUDFLARE_FREE_UPLOAD_LIMIT_MB} MB.",
                status,
            )
        if status >= 500 and detail:  # Bambuddy's own error, e.g. 503 "Failed to capture camera frame"
            return BambuddyError("server", detail, status)
        if status in (502, 503, 504, 520, 521, 522, 523, 524):
            return BambuddyError("unreachable", f"Bambuddy is not reachable behind the proxy (HTTP {status}).", status)
        if 400 <= status < 500:
            return BambuddyError("bad_request", detail or f"Request rejected (HTTP {status}).", status)
        return BambuddyError("server", f"Bambuddy reported an error (HTTP {status}){': ' + detail if detail else ''}.", status)

    # ---- connection check -----------------------------------------------------

    def version(self) -> str:
        """Server version; public endpoint, so it also tests URL and Cloudflare."""
        try:
            data = self._json("GET", "/updates/version", auth=False)
        except BambuddyError as e:
            if e.kind == "not_found":
                raise BambuddyError(
                    "bad_url",
                    f"No Bambuddy API found at {self.base_url}. Check the URL "
                    "(the address you open Bambuddy at, without /api/v1).",
                    e.status,
                ) from None
            raise
        if not isinstance(data, dict) or "version" not in data:
            raise BambuddyError("bad_url", f"{self.base_url} does not look like a Bambuddy server.")
        return str(data["version"])

    def auth_status(self) -> dict:
        return self._json("GET", "/auth/status", auth=False)

    def me(self) -> dict:
        return self._json("GET", "/auth/me")

    # ---- printers -------------------------------------------------------------

    def printers(self) -> list[dict]:
        return self._json("GET", "/printers/")

    def printer_status(self, printer_id: int) -> dict:
        return self._json("GET", f"/printers/{int(printer_id)}/status")

    def camera_snapshot(self, printer_id: int) -> bytes:
        """One JPEG frame. The snapshot route takes a stream token instead of the API key."""
        token = self._camera_token
        if token is None or token[1] < time.monotonic():
            # Tokens live 60 minutes on the server; renew a little earlier.
            data = self._json("POST", "/printers/camera/stream-token")
            token = (data["token"], time.monotonic() + 50 * 60)
            self._camera_token = token
        try:
            resp = self._request(
                "GET", f"/printers/{int(printer_id)}/camera/snapshot", params={"token": token[0]}, timeout=30
            )
        except BambuddyError as e:
            if e.status == 401:
                self._camera_token = None
            raise
        return resp.body

    # ---- library and queue ----------------------------------------------------

    def upload_library_file(
        self,
        path: str,
        filename: str | None = None,
        *,
        folder_id: int | None = None,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        chunk_size: int = 256 * 1024,
    ) -> dict:
        """Upload a file to the library and return Bambuddy's FileUploadResponse.

        The body is streamed from disk; ``progress(sent, total)`` is called per chunk and
        ``cancelled()`` is polled between chunks.
        """
        filename = filename or os.path.basename(path)
        boundary = "----orca-bambuddy-" + uuid.uuid4().hex
        quoted = filename.replace("\\", "\\\\").replace('"', '\\"')
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{quoted}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("ascii")
        file_size = os.path.getsize(path)
        total = len(head) + file_size + len(tail)

        def body() -> Iterator[bytes]:
            sent = 0
            yield head
            with open(path, "rb") as f:
                while True:
                    if cancelled and cancelled():
                        raise BambuddyError("cancelled", "Upload cancelled.")
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if progress:
                        progress(sent, file_size)
                    yield chunk
            yield tail

        return self._json(
            "POST",
            "/library/files",
            params={"folder_id": folder_id},
            data=body(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(total)},
            timeout=self.conn.upload_timeout,
        )

    def library_file(self, file_id: int) -> dict:
        return self._json("GET", f"/library/files/{int(file_id)}")

    def delete_library_file(self, file_id: int) -> None:
        self._request("DELETE", f"/library/files/{int(file_id)}")

    def add_to_queue(
        self,
        library_file_id: int,
        *,
        printer_id: int | None = None,
        target_model: str | None = None,
        manual_start: bool = False,
        plate_id: int | None = None,
        options: dict | None = None,
    ) -> dict:
        """Create a print queue item (POST /queue/).

        ``manual_start=False`` with no ``scheduled_time`` is Bambuddy's "ASAP": the
        scheduler starts it as soon as the printer is idle. ``manual_start=True`` stages
        it until someone presses Start in Bambuddy. ``printer_id`` and ``target_model``
        are mutually exclusive; ``target_model`` means "any printer of this model".
        ``options`` are PrintQueueItemCreate print options (bed_levelling, ams_mapping, ...).
        """
        payload: dict = dict(options or {})
        payload.update({"library_file_id": int(library_file_id), "manual_start": bool(manual_start)})
        if printer_id is not None:
            payload["printer_id"] = int(printer_id)
        if target_model:
            payload["target_model"] = target_model
        if plate_id is not None:
            payload["plate_id"] = int(plate_id)
        return self._json("POST", "/queue/", json_body=payload)

    def queue_item(self, item_id: int) -> dict:
        return self._json("GET", f"/queue/{int(item_id)}")

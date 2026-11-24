#!/usr/bin/env python3
"""Authenticated, rate-limit aware GitHub access for the builder scripts.

Unauthenticated requests to ``api.github.com`` are limited per source IP. Hosted
runners share those addresses, so a build that resolves the Horizon Nightly
without a token can fail with ``HTTP 403: rate limit exceeded``. These helpers
reuse the workflow's ``GITHUB_TOKEN`` when present and retry transient statuses.
"""
from __future__ import annotations

import datetime
import json
import email.utils
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")
RETRY_STATUS = frozenset({403, 429, 500, 502, 503, 504})
TOKEN_HOSTS = frozenset({"api.github.com", "github.com"})
USER_AGENT = "horizon-native-instance-builder"
DEFAULT_ATTEMPTS = 3


def token() -> str | None:
    """Return the workflow token, if one is available in the environment."""
    for name in TOKEN_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def build_headers(url: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    host = urllib.parse.urlparse(url).hostname or ""
    value = token()
    if value and host in TOKEN_HOSTS:
        headers["Authorization"] = f"Bearer {value}"
    return headers


MAX_JSON_BYTES = 8 * 1024 * 1024


def read_json_limited(url: str, *, max_bytes: int = MAX_JSON_BYTES, opener=None):
    """Fetch GitHub JSON with a byte cap and a final-host recheck."""
    if opener is None:
        opener = open_url
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in TOKEN_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"Refusing non-GitHub API JSON URL: {url}")
    with opener(url) as response:
        geturl = getattr(response, "geturl", None)
        if geturl is not None:
            final = urllib.parse.urlparse(geturl())
            if (final.scheme, final.hostname, final.port) != (
                "https", parsed.hostname, parsed.port
            ):
                raise ValueError(f"Refusing redirect to a non-GitHub host: {geturl()}")
        headers = getattr(response, "headers", None) or {}
        declared = headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError(
                f"GitHub JSON response exceeds the {max_bytes}-byte safety limit: {url}"
            )
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"GitHub JSON response exceeds the {max_bytes}-byte safety limit: {url}"
        )
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid JSON from {url}: {error}") from error

def retry_delay(error: Exception, attempt: int) -> float:
    headers = getattr(error, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    if isinstance(retry_after, str):
        if retry_after.strip().isdigit():
            return min(float(retry_after), 60.0)
        try:
            when = email.utils.parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            remaining = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            return min(max(remaining, 0.0), 60.0)
    return min(2.0**attempt, 30.0)


def open_url(
    url: str,
    *,
    timeout: float = 60,
    accept: str = "application/vnd.github+json",
    attempts: int = DEFAULT_ATTEMPTS,
    sleep: object = time.sleep,
):
    """Open ``url`` with authentication and retries for transient failures."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        headers = build_headers(url, accept)
        authorization = headers.pop("Authorization", None)
        request = urllib.request.Request(url, headers=headers)
        # urllib drops unredirected headers when it follows a redirect, so the
        # workflow token cannot be forwarded to a redirected host.
        if authorization is not None:
            request.add_unredirected_header("Authorization", authorization)
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in RETRY_STATUS or attempt == attempts:
                raise
        except urllib.error.URLError as error:
            last_error = error
            if attempt == attempts:
                raise
        sleep(retry_delay(last_error, attempt))
    raise RuntimeError(f"request failed without an error: {url}") from last_error

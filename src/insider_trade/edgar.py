"""Rate-limited HTTP client for SEC EDGAR."""
from __future__ import annotations

import threading
import time

import requests

from . import config


class NotFound(Exception):
    pass


def _is_missing_object(r: requests.Response) -> bool:
    """Missing files under /Archives (e.g. the daily index on holidays) come back from the
    S3 backend as 403 AccessDenied XML. Throttling 403s are HTML pages instead."""
    body = r.text[:500]
    return "<Code>AccessDenied</Code>" in body or "<Code>NoSuchKey</Code>" in body


class _RateLimiter:
    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_time > now:
                time.sleep(self.next_time - now)
                now = self.next_time
            self.next_time = now + self.interval


class SecClient:
    def __init__(self, rps: float | None = None):
        self.user_agent = config.user_agent()
        self.limiter = _RateLimiter(rps or config.SEC_MAX_RPS)
        self.local = threading.local()

    def _session(self) -> requests.Session:
        s = getattr(self.local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"})
            self.local.session = s
        return s

    def get(self, url: str, retries: int = 5, timeout: float = 60) -> requests.Response:
        delay = 2.0
        for attempt in range(retries):
            self.limiter.wait()
            try:
                r = self._session().get(url, timeout=timeout)
            except requests.RequestException:
                if attempt == retries - 1:
                    raise
            else:
                if r.status_code == 200:
                    return r
                if r.status_code == 404 or (r.status_code == 403 and _is_missing_object(r)):
                    raise NotFound(url)
                # 403 is what SEC returns when throttling, so it's retried too.
                if r.status_code not in (403, 429, 500, 502, 503, 504) or attempt == retries - 1:
                    if r.status_code == 403:
                        raise PermissionError(
                            f"SEC returned 403 for {url}. Set SEC_USER_AGENT to 'Your Name your@email.com' "
                            "(SEC requires a contact email) and keep requests under 10/second."
                        )
                    r.raise_for_status()
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"unreachable: {url}")

    def get_text(self, url: str) -> str:
        return self.get(url).text

    def get_json(self, url: str):
        return self.get(url).json()

"""Small bounded HTTP client with injectable transport for offline tests."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener, ProxyHandler


class HTTPClientError(RuntimeError):
    def __init__(self, message: str, *, code: str = "http_error", status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


@dataclass
class HTTPResponse:
    status: int
    url: str
    body: bytes
    headers: Mapping[str, str]
    elapsed: float = 0.0

    @property
    def text(self) -> str:
        content_type = str(self.headers.get("Content-Type", "")).lower()
        enc = "gbk" if "gbk" in content_type or "gb2312" in content_type else "utf-8"
        try:
            return self.body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPClientError("响应不是合法 JSON", code="invalid_json") from exc


Transport = Callable[..., HTTPResponse]


class HTTPClient:
    """Network boundary.

    ``transport`` may be supplied by tests or a source-specific network path.
    It receives ``method, url, params, data, headers, timeout`` and returns an
    ``HTTPResponse``.  Default requests use TLS-verified urllib and a bounded
    retry policy; 429 and 5xx are retryable, 4xx errors are not.
    """

    def __init__(self, transport: Transport | None = None, *, opener=None, user_agent: str = "a-share-data/1.0"):
        self.transport = transport
        self.opener = opener
        self.user_agent = user_agent
        self.request_count = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: bytes | str | Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        retries: int = 1,
    ) -> HTTPResponse:
        if retries < 0 or retries > 3:
            raise ValueError("retries 必须在 0–3 之间")
        method = method.upper()
        params = params or {}
        headers_out = {"User-Agent": self.user_agent, "Accept": "application/json,text/plain,*/*"}
        headers_out.update(headers or {})
        last_error: HTTPClientError | None = None
        for attempt in range(retries + 1):
            try:
                self.request_count += 1
                if self.transport is not None:
                    response = self.transport(
                        method,
                        url,
                        params=dict(params),
                        data=data,
                        headers=headers_out,
                        timeout=timeout,
                    )
                else:
                    response = self._urllib(method, url, params, data, headers_out, timeout)
                if response.status < 200 or response.status >= 300:
                    retryable = response.status == 429 or response.status >= 500
                    last_error = HTTPClientError(
                        f"HTTP {response.status}: {response.url}",
                        code="http_status",
                        status=response.status,
                        retryable=retryable,
                    )
                    if not retryable or attempt >= retries:
                        raise last_error
                else:
                    return response
            except HTTPClientError as exc:
                last_error = exc
                if not exc.retryable or attempt >= retries:
                    raise
            except HTTPError as exc:
                # Keep an HTTP status from urllib's exception path.  Without
                # this branch a real 404 becomes a generic network error and
                # date-scoped sources cannot distinguish "not published" from
                # a disconnected network.
                status = int(exc.code) if getattr(exc, "code", None) is not None else None
                retryable = status == 429 or (status is not None and status >= 500)
                last_error = HTTPClientError(
                    f"HTTP {status}: {exc}",
                    code="http_status" if status is not None else "network_error",
                    status=status,
                    retryable=retryable,
                )
                if not retryable or attempt >= retries:
                    raise last_error from exc
            except (URLError, TimeoutError, OSError) as exc:
                last_error = HTTPClientError(
                    f"请求失败: {type(exc).__name__}: {exc}", code="network_error", retryable=True
                )
                if attempt >= retries:
                    raise last_error from exc
            if attempt < retries:
                time.sleep(min(0.5 * (attempt + 1), 1.0))
        raise last_error or HTTPClientError("请求失败", code="unknown_http_error")

    def get(self, url: str, **kwargs: Any) -> HTTPResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HTTPResponse:
        return self.request("POST", url, **kwargs)

    def _urllib(self, method: str, url: str, params: Mapping[str, Any], data: Any, headers: dict[str, str], timeout: float) -> HTTPResponse:
        query = urlencode([(str(k), str(v)) for k, v in params.items() if v is not None])
        request_url = f"{url}?{query}" if query else url
        body: bytes | None = None
        if data is not None:
            if isinstance(data, Mapping):
                body = urlencode([(str(k), str(v)) for k, v in data.items()]).encode("utf-8")
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
            elif isinstance(data, str):
                body = data.encode("utf-8")
            else:
                body = data
        request = Request(request_url, data=body, headers=headers, method=method)
        started = time.monotonic()
        opener = self.opener or build_opener()
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            response_headers = {str(k): str(v) for k, v in response.headers.items()}
            return HTTPResponse(int(response.status), response.geturl(), raw, response_headers, time.monotonic() - started)


def project_http_client(*, deadline: float | None = None) -> HTTPClient:
    """Build the project's measured-path, TLS-verifying HTTP client.

    The network-path module lives beside the dashboard scripts, so this helper
    resolves that directory lazily.  Tests and callers that inject a transport
    never pass through the path probe; real CLI/API entry points do.
    """
    try:
        scripts_dir = Path(__file__).resolve().parents[2] / "daily-stock-analysis" / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        import network_path
        import tls_context

        proxy = network_path.best_proxy_url(deadline=deadline)
        handlers = [HTTPSHandler(context=tls_context.build_context())]
        handlers.append(ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
        return HTTPClient(opener=build_opener(*handlers))
    except Exception:
        # Outside the dashboard, verified system TLS remains the safe fallback
        # if path discovery itself is unavailable.
        return HTTPClient()

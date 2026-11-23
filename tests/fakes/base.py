"""Shared plumbing for the fakes: routing, fault injection, request recording,
and patching the HTTP clients the bot uses.

The bot builds its own clients (`httpx.AsyncClient(timeout=...)` in every
module, `requests.get` in `tipranks_client.py`). Nothing in `stock_agent/`
uses `aiohttp` today. `FakeInternet.install(monkeypatch)` therefore:

- forces every new `httpx.Client` / `httpx.AsyncClient` to use an
  `httpx.MockTransport` that routes by host to a fake service;
- routes `requests` through the same handler (by patching
  `requests.adapters.HTTPAdapter.send`);
- makes `aiohttp.ClientSession._request` fail loudly, so a future use of
  aiohttp cannot reach the network unnoticed.

A request to a host that no fake serves is recorded as `unexpected` and
answered with an `httpx.ConnectError`, so it can never reach the network.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

import httpx

FAULT_KINDS = (
    "timeout",  # the client sees httpx.ReadTimeout; the service did nothing
    "connect_error",  # the client sees httpx.ConnectError; the service did nothing
    "5xx",  # HTTP 500 (or `status`), service did nothing
    "429",  # HTTP 429 with Retry-After
    "malformed_json",  # HTTP 200 with a body that is not JSON
    "empty_list",  # HTTP 200 with []
    "empty_object",  # HTTP 200 with {}
    "status",  # any HTTP status given in `status`, body from `body`
    "lost_response",  # the service DID process the request, then the client sees a timeout
)


@dataclass
class Fault:
    method: str  # "GET", "POST", ... or "*"
    path: str  # fnmatch pattern on the URL path, e.g. "/v2/orders*"
    kind: str
    times: int | None = 1  # None = every matching request
    status: int | None = None
    body: Any = None
    hits: int = 0

    def matches(self, method: str, path: str) -> bool:
        if self.times is not None and self.hits >= self.times:
            return False
        if self.method != "*" and self.method.upper() != method.upper():
            return False
        return fnmatch.fnmatchcase(path, self.path)


@dataclass
class RecordedRequest:
    service: str
    method: str
    url: str
    host: str
    path: str
    params: dict[str, str]
    json: Any
    headers: dict[str, str]
    at: datetime | None
    status: int | None = None  # what the client received (None = an exception)
    fault: str | None = None
    processed: bool = True  # False when a fault stopped the service from acting

    def __repr__(self) -> str:  # short, for assertion messages
        body = f" {self.json}" if self.json is not None else ""
        return f"<{self.service} {self.method} {self.path}{body} -> {self.status} {self.fault or ''}>"


_SECRET_HEADERS = {"authorization", "apca-api-key-id", "apca-api-secret-key", "x-apikey", "x-apitoken", "x-api-key"}


def _clean_headers(h: httpx.Headers) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _SECRET_HEADERS else v) for k, v in h.items()}


def json_response(data: Any, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


def request_json(request: httpx.Request) -> Any:
    raw = request.content
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


class FakeService:
    """Base class: a named fake HTTP service with its own fault list."""

    name = "service"
    hosts: tuple[str, ...] = ()

    def __init__(self):
        self.faults: list[Fault] = []

    def fail(
        self,
        path: str,
        kind: str,
        *,
        method: str = "*",
        times: int | None = 1,
        status: int | None = None,
        body: Any = None,
    ) -> Fault:
        """Inject a fault on requests whose path matches `path` (fnmatch).

        kind is one of FAULT_KINDS. `times=None` keeps failing forever.
        """
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}; choose from {FAULT_KINDS}")
        f = Fault(method=method, path=path, kind=kind, times=times, status=status, body=body)
        self.faults.append(f)
        return f

    def clear_faults(self) -> None:
        self.faults.clear()

    def handle(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover - abstract
        raise NotImplementedError

    def now(self) -> datetime | None:
        return None


class FakeInternet:
    """All fake services plus the request recorder."""

    def __init__(self, services: Iterable[FakeService] = ()):
        self.services: list[FakeService] = []
        self._by_host: dict[str, FakeService] = {}
        self.recorded: list[RecordedRequest] = []
        self.unexpected: list[RecordedRequest] = []
        self.transport = httpx.MockTransport(self.handle)
        for s in services:
            self.add(s)

    def add(self, service: FakeService) -> FakeService:
        self.services.append(service)
        for h in service.hosts:
            self._by_host[h] = service
        return service

    def service(self, name: str) -> FakeService:
        for s in self.services:
            if s.name == name:
                return s
        raise KeyError(name)

    # the one HTTP entry point
    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        svc = self._by_host.get(host)
        rec = RecordedRequest(
            service=svc.name if svc else "unexpected",
            method=request.method,
            url=str(request.url),
            host=host,
            path=path,
            params=dict(request.url.params),
            json=request_json(request),
            headers=_clean_headers(request.headers),
            at=svc.now() if svc else None,
        )
        self.recorded.append(rec)
        if svc is None:
            rec.processed = False
            self.unexpected.append(rec)
            raise httpx.ConnectError(f"fake internet: no fake serves host {host!r}", request=request)

        fault = next((f for f in svc.faults if f.matches(request.method, path)), None)
        if fault is not None:
            fault.hits += 1
            rec.fault = fault.kind
            return self._apply_fault(svc, fault, request, rec)

        resp = svc.handle(request)
        rec.status = resp.status_code
        return resp

    def _apply_fault(self, svc: FakeService, f: Fault, request: httpx.Request, rec: RecordedRequest) -> httpx.Response:
        k = f.kind
        if k == "lost_response":
            resp = svc.handle(request)  # the service acts on it ...
            rec.processed = True
            rec.status = None
            raise httpx.ReadTimeout(f"fake: response lost after the service answered {resp.status_code}", request=request)
        rec.processed = False
        if k == "timeout":
            raise httpx.ReadTimeout("fake: read timeout", request=request)
        if k == "connect_error":
            raise httpx.ConnectError("fake: connection refused", request=request)
        if k == "5xx":
            status = f.status or 500
            resp = httpx.Response(status, json=f.body if f.body is not None else {"message": "internal server error"})
        elif k == "429":
            resp = httpx.Response(
                429,
                json=f.body if f.body is not None else {"message": "too many requests"},
                headers={"Retry-After": "1"},
            )
        elif k == "malformed_json":
            resp = httpx.Response(200, content=b"{not json", headers={"Content-Type": "application/json"})
        elif k == "empty_list":
            resp = httpx.Response(200, json=[])
        elif k == "empty_object":
            resp = httpx.Response(200, json={})
        elif k == "status":
            resp = httpx.Response(f.status or 400, json=f.body if f.body is not None else {"message": "fault"})
        else:  # pragma: no cover
            raise ValueError(k)
        rec.status = resp.status_code
        return resp

    # queries for assertions
    def requests(
        self,
        service: str | None = None,
        method: str | None = None,
        path: str | None = None,
        where: Callable[[RecordedRequest], bool] | None = None,
    ) -> list[RecordedRequest]:
        out = []
        for r in self.recorded:
            if service and r.service != service:
                continue
            if method and r.method.upper() != method.upper():
                continue
            if path and not fnmatch.fnmatchcase(r.path, path):
                continue
            if where and not where(r):
                continue
            out.append(r)
        return out

    def assert_no_unexpected(self) -> None:
        assert not self.unexpected, f"requests to hosts no fake serves: {self.unexpected}"

    # patching
    def install(self, monkeypatch) -> "FakeInternet":
        transport = self.transport

        real_async_init = httpx.AsyncClient.__init__
        real_sync_init = httpx.Client.__init__

        def async_init(client_self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs.pop("proxy", None)
            kwargs.pop("proxies", None)
            real_async_init(client_self, *args, **kwargs)

        def sync_init(client_self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs.pop("proxy", None)
            kwargs.pop("proxies", None)
            real_sync_init(client_self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", async_init)
        monkeypatch.setattr(httpx.Client, "__init__", sync_init)
        _patch_requests(monkeypatch, self)
        _block_aiohttp(monkeypatch)
        return self


def _patch_requests(monkeypatch, internet: FakeInternet) -> None:
    try:
        import requests
        import requests.adapters
    except ImportError:  # pragma: no cover
        return

    def send(adapter_self, prepared, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        req = httpx.Request(prepared.method, prepared.url, headers=dict(prepared.headers), content=prepared.body or b"")
        try:
            resp = internet.handle(req)
        except httpx.TimeoutException as e:
            raise requests.exceptions.ReadTimeout(str(e), request=prepared) from e
        except httpx.ConnectError as e:
            raise requests.exceptions.ConnectionError(str(e), request=prepared) from e
        out = requests.Response()
        out.status_code = resp.status_code
        out.headers.update(resp.headers)
        out._content = resp.content
        out.url = prepared.url
        out.request = prepared
        out.encoding = "utf-8"
        out.reason = httpx.codes.get_reason_phrase(resp.status_code)
        return out

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)


def _block_aiohttp(monkeypatch) -> None:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover
        return

    async def refuse(*args, **kwargs):
        raise AssertionError(
            "aiohttp was used but the test fakes only serve httpx and requests; "
            "add aiohttp routing to tests/fakes/base.py"
        )

    monkeypatch.setattr(aiohttp.ClientSession, "_request", refuse)

"""Robinhood Agentic Trading integration.

Robinhood exposes agentic trading as a remote MCP server
(default https://agent.robinhood.com/mcp/trading) protected by OAuth 2.1.
This module:

* runs the OAuth "URL handshake" (authorization code + PKCE, dynamic client
  registration) through the official MCP SDK, capturing the redirect on a
  one-shot loopback server bound to 127.0.0.1;
* stores tokens in a file readable only by the current user (0600);
* verifies the configured account is the one enabled for agentic trading;
* builds, reviews (simulates) and places BUY orders for cluster signals,
  enforcing hard risk limits.

No order is placed unless the caller passes a HumanApproval that was issued
for that exact reviewed order within the last APPROVAL_TTL_SECONDS. The only
code that creates approvals is the terminal guardrail in main.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import html
import http.server
import json
import logging
import math
import os
import secrets
import stat
import threading
import time
import urllib.parse
import uuid
import webbrowser
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

log = logging.getLogger(__name__)

DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"
APPROVAL_TTL_SECONDS = 120


# --------------------------------------------------------------------- config
@dataclass
class TraderConfig:
    account_number: str                      # the Robinhood account enabled for agentic trading
    mcp_url: str = DEFAULT_MCP_URL
    token_path: Path = Path("~/.config/insider-tracker/robinhood_oauth.json").expanduser()
    redirect_port: int = 8765
    dry_run: bool = True                     # review only, never place
    order_type: str = "limit"                # "limit" (whole shares, marketable limit) or "market" (dollar-based)
    order_dollars: float = 100.0             # target notional per signal
    max_order_dollars: float = 250.0         # hard cap per order
    max_orders_per_day: int = 3
    limit_buffer_bps: float = 25.0           # limit = ask * (1 + bps/10_000)
    ledger_path: Path = Path("data/orders.jsonl")

    def validate(self) -> None:
        if not self.account_number or not self.account_number.isdigit():
            raise ValueError("ROBINHOOD_ACCOUNT_NUMBER must be set to your agentic account number")
        if not self.mcp_url.startswith("https://"):
            raise ValueError("ROBINHOOD_MCP_URL must be https")
        if self.order_type not in ("limit", "market"):
            raise ValueError("ORDER_TYPE must be 'limit' or 'market'")
        if self.order_dollars <= 0 or self.order_dollars > self.max_order_dollars:
            raise ValueError("ORDER_DOLLARS must be > 0 and <= MAX_ORDER_DOLLARS")


# --------------------------------------------------------------------- secure token storage
class FileTokenStorage(TokenStorage):
    """OAuth tokens + dynamic client registration persisted with 0600 permissions."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, stat.S_IRWXU)  # 0700

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(f"{self.path} is accessible by other users (mode {oct(mode)}); "
                                  f"run: chmod 600 {self.path}")
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    async def get_tokens(self) -> OAuthToken | None:
        d = self._read().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json")
        self._write(d)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        d = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        d = self._read()
        d["client_info"] = client_info.model_dump(mode="json")
        self._write(d)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


# --------------------------------------------------------------------- OAuth URL handshake
class _LoopbackCallback:
    """One-shot HTTP server on 127.0.0.1 that captures ?code=&state= from the OAuth redirect."""

    def __init__(self, port: int):
        self.port = port
        self.result: AuthorizationCodeResult | None = None
        self.error: str | None = None
        self._done = threading.Event()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.port}/callback"

    def serve_once(self, timeout: float = 300) -> AuthorizationCodeResult:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urllib.parse.urlparse(self.path)
                if u.path != "/callback":
                    self.send_error(404)
                    return
                q = urllib.parse.parse_qs(u.query)
                if "error" in q:
                    outer.error = q.get("error_description", q["error"])[0]
                elif "code" in q:
                    outer.result = AuthorizationCodeResult(
                        code=q["code"][0], state=q.get("state", [None])[0], iss=q.get("iss", [None])[0])
                else:
                    outer.error = "redirect had neither code nor error"
                msg = "Robinhood linked. You can close this tab." if outer.result else \
                    f"Authorization failed: {html.escape(outer.error or '')}"
                body = f"<html><body><h3>{msg}</h3></body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                outer._done.set()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        srv.timeout = 1
        deadline = time.monotonic() + timeout
        try:
            while not self._done.is_set() and time.monotonic() < deadline:
                srv.handle_request()
        finally:
            srv.server_close()
        if self.result:
            return self.result
        raise TimeoutError(self.error or "timed out waiting for Robinhood OAuth redirect")


async def _redirect_handler(authorization_url: str) -> None:
    print("\n=== Robinhood account link ===")
    print("Open this URL, sign in to Robinhood and approve access for this app:\n")
    print(f"  {authorization_url}\n")
    try:
        webbrowser.open(authorization_url)
    except Exception:
        pass


def build_oauth_provider(cfg: TraderConfig, storage: FileTokenStorage) -> OAuthClientProvider:
    cb = _LoopbackCallback(cfg.redirect_port)

    async def callback_handler() -> AuthorizationCodeResult:
        return await asyncio.to_thread(cb.serve_once)

    metadata = OAuthClientMetadata(
        client_name="insider-cluster-tracker",
        redirect_uris=[cb.redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",   # public native client; PKCE protects the code exchange
    )
    return OAuthClientProvider(
        server_url=cfg.mcp_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=_redirect_handler,
        callback_handler=callback_handler,
    )


# --------------------------------------------------------------------- orders & approvals
@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    type: str
    quantity: str | None = None
    dollar_amount: str | None = None
    limit_price: str | None = None
    time_in_force: str = "gfd"
    market_hours: str = "regular_hours"
    ref_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def tool_args(self, account_number: str) -> dict[str, Any]:
        args = dict(account_number=account_number, symbol=self.symbol, side=self.side, type=self.type,
                    time_in_force=self.time_in_force, market_hours=self.market_hours)
        for k in ("quantity", "dollar_amount", "limit_price"):
            v = getattr(self, k)
            if v is not None:
                args[k] = v
        return args

    def describe(self) -> str:
        size = f"{self.quantity} sh" if self.quantity else f"${self.dollar_amount}"
        px = f" limit ${self.limit_price}" if self.limit_price else " at market"
        return f"{self.side.upper()} {size} {self.symbol}{px} ({self.time_in_force}, {self.market_hours})"


@dataclass(frozen=True)
class HumanApproval:
    """Proof that a human typed 'Y' for this exact order. Created only by main.py's guardrail."""
    ref_id: str
    symbol: str
    approved_at: float
    nonce: str


@dataclass
class ReviewResult:
    order: OrderRequest
    raw: dict
    alerts: list
    nonce: str = field(default_factory=lambda: secrets.token_hex(8))


class GuardrailViolation(RuntimeError):
    pass


# --------------------------------------------------------------------- trader
class RobinhoodAgenticTrader:
    """Async context manager wrapping the Robinhood Agentic MCP server."""

    def __init__(self, cfg: TraderConfig):
        cfg.validate()
        self.cfg = cfg
        self.storage = FileTokenStorage(cfg.token_path)
        self._stack: AsyncExitStack | None = None
        self.client: Client | None = None
        self._reviews: dict[str, ReviewResult] = {}

    async def __aenter__(self) -> "RobinhoodAgenticTrader":
        self._stack = AsyncExitStack()
        auth = build_oauth_provider(self.cfg, self.storage)
        http = await self._stack.enter_async_context(
            httpx2.AsyncClient(auth=auth, timeout=httpx2.Timeout(30.0, read=120.0)))
        transport = streamable_http_client(self.cfg.mcp_url, http_client=http)
        try:
            self.client = await self._stack.enter_async_context(Client(transport))
            await self.verify_account()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()

    # ---- MCP plumbing
    async def _call(self, tool: str, args: dict[str, Any]) -> dict:
        assert self.client is not None, "use 'async with RobinhoodAgenticTrader(...)'"
        res = await self.client.call_tool(tool, args)
        payload: Any = res.structured_content
        if payload is None:
            text = "".join(getattr(c, "text", "") for c in res.content)
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"text": text}
        if res.is_error:
            raise RuntimeError(f"Robinhood {tool} failed: {payload}")
        return payload

    # ---- read-only
    async def verify_account(self) -> dict:
        data = await self._call("get_accounts", {})
        accounts = data.get("data", {}).get("accounts", [])
        acct = next((a for a in accounts if a.get("account_number") == self.cfg.account_number), None)
        if acct is None:
            raise GuardrailViolation("ROBINHOOD_ACCOUNT_NUMBER not found on the linked Robinhood login")
        if not acct.get("agentic_allowed"):
            raise GuardrailViolation("configured account is not enabled for agentic trading by this app")
        return acct

    async def quote(self, symbol: str) -> dict:
        data = await self._call("get_equity_quotes", {"symbols": [symbol]})
        results = data.get("data", {}).get("results", [])
        if not results:
            raise RuntimeError(f"no quote for {symbol}")
        return results[0]["quote"]

    # ---- order construction
    def _orders_today(self) -> int:
        if not self.cfg.ledger_path.exists():
            return 0
        today = dt.date.today().isoformat()
        n = 0
        for line in self.cfg.ledger_path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("event") == "placed" and rec.get("ts", "").startswith(today):
                n += 1
        return n

    def _ledger(self, event: str, **kw) -> None:
        self.cfg.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.ledger_path, "a") as f:
            f.write(json.dumps(dict(ts=dt.datetime.now().isoformat(), event=event, **kw), default=str) + "\n")

    async def build_buy(self, symbol: str) -> OrderRequest:
        q = await self.quote(symbol)
        if q.get("state") != "active" or not q.get("has_traded", True):
            raise GuardrailViolation(f"{symbol} is not actively trading (state={q.get('state')})")
        if self.cfg.order_type == "market":
            return OrderRequest(symbol=symbol, side="buy", type="market",
                                dollar_amount=f"{self.cfg.order_dollars:.2f}")
        ask = float(q.get("ask_price") or 0) or float(q.get("last_trade_price") or 0)
        if ask <= 0:
            raise GuardrailViolation(f"no usable ask/last price for {symbol}")
        limit = round(ask * (1 + self.cfg.limit_buffer_bps / 10_000), 2 if ask >= 1 else 4)
        qty = math.floor(self.cfg.order_dollars / limit)
        if qty < 1:
            raise GuardrailViolation(f"ORDER_DOLLARS ${self.cfg.order_dollars} buys < 1 share of {symbol} "
                                     f"at ${limit}; raise ORDER_DOLLARS or use ORDER_TYPE=market")
        return OrderRequest(symbol=symbol, side="buy", type="limit", quantity=str(qty), limit_price=f"{limit}")

    # ---- review (simulation; places nothing)
    async def review(self, order: OrderRequest) -> ReviewResult:
        est = float(order.dollar_amount or 0) or float(order.quantity) * float(order.limit_price)
        if est > self.cfg.max_order_dollars:
            raise GuardrailViolation(f"order ~${est:,.2f} exceeds MAX_ORDER_DOLLARS ${self.cfg.max_order_dollars}")
        raw = await self._call("review_equity_order", order.tool_args(self.cfg.account_number))
        data = raw.get("data", raw)
        alerts = data.get("alerts") or data.get("pre_trade_alerts") or []
        rv = ReviewResult(order=order, raw=raw, alerts=alerts)
        self._reviews[order.ref_id] = rv
        self._ledger("reviewed", order=order.__dict__, alerts=alerts)
        return rv

    # ---- placement (requires human approval)
    async def place(self, review: ReviewResult, approval: HumanApproval) -> dict:
        order = review.order
        if self._reviews.get(order.ref_id) is not review:
            raise GuardrailViolation("order was not reviewed in this session")
        if (approval.ref_id, approval.symbol, approval.nonce) != (order.ref_id, order.symbol, review.nonce):
            raise GuardrailViolation("approval does not match the reviewed order")
        if time.time() - approval.approved_at > APPROVAL_TTL_SECONDS:
            raise GuardrailViolation("approval expired; re-review the order")
        if self._orders_today() >= self.cfg.max_orders_per_day:
            raise GuardrailViolation(f"MAX_ORDERS_PER_DAY ({self.cfg.max_orders_per_day}) reached")
        if self.cfg.dry_run:
            self._ledger("dry_run", order=order.__dict__)
            return {"dry_run": True, "order": order.describe()}
        args = order.tool_args(self.cfg.account_number) | {"ref_id": order.ref_id}
        try:
            res = await self._call("place_equity_order", args)
        except (httpx2.TransportError, TimeoutError) as e:
            # network blip only: one retry with the SAME ref_id is safe, Robinhood dedupes on it
            log.warning("place transport error (%s); retrying once with same ref_id", e)
            res = await self._call("place_equity_order", args)
        del self._reviews[order.ref_id]
        self._ledger("placed", order=order.__dict__, response=res)
        return res


async def link_account(cfg: TraderConfig) -> dict:
    """Run the OAuth handshake (if needed) and confirm the agentic account."""
    async with RobinhoodAgenticTrader(cfg) as t:
        return await t.verify_account()

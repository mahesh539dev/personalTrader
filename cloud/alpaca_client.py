"""Alpaca REST API client. Standard library only (urllib).

Replaces the Claude MCP flow for order placement. The existing ticket.py
produces flat Alpaca field names via tool_input(); this client restructures
bracket legs into the nested format Alpaca's REST API expects.
"""

import json
import logging
import urllib.error
import urllib.request

from cloud import config

log = logging.getLogger(__name__)


class AlpacaError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Alpaca API {status}: {body}")


class AlpacaClient:
    def __init__(self):
        self.key = config.alpaca_key()
        self.secret = config.alpaca_secret()
        paper = config.alpaca_paper()
        self.base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self.paper = paper

    def _request(self, method: str, path: str, body: dict | None = None) -> dict | list:
        url = self.base + path
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("APCA-API-KEY-ID", self.key)
        req.add_header("APCA-API-SECRET-KEY", self.secret)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise AlpacaError(e.code, e.read().decode()) from e

    def place_order(self, order: dict) -> dict:
        """Place an order. Accepts the flat dict from ticket.py's tool_input().

        Restructures bracket legs from flat keys to nested Alpaca REST format:
          flat:  {"take_profit_limit_price": "370.04", "stop_loss_stop_price": "358.32"}
          REST:  {"take_profit": {"limit_price": "370.04"}, "stop_loss": {"stop_price": "358.32"}}
        """
        body = dict(order)

        # Remove non-Alpaca fields the tools add
        body.pop("why", None)
        body.pop("plan", None)

        # Restructure bracket legs for REST API
        if body.get("order_class") == "bracket" or body.get("take_profit_limit_price") or body.get("stop_loss_stop_price"):
            if not body.get("order_class"):
                body["order_class"] = "bracket"

            tp_price = body.pop("take_profit_limit_price", None)
            sl_stop = body.pop("stop_loss_stop_price", None)
            sl_limit = body.pop("stop_loss_limit_price", None)

            if tp_price is not None:
                body["take_profit"] = {"limit_price": str(tp_price)}
            if sl_stop is not None:
                sl = {"stop_price": str(sl_stop)}
                if sl_limit is not None:
                    sl["limit_price"] = str(sl_limit)
                body["stop_loss"] = sl

        log.info("Placing order: %s %s %s", body.get("side"), body.get("qty"), body.get("symbol"))
        result = self._request("POST", "/v2/orders", body)
        log.info("Order placed: %s (status: %s)", result.get("id"), result.get("status"))
        return result

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/orders/{order_id}")

    def get_orders(self, status: str = "open", after: str | None = None,
                   limit: int = 50) -> list:
        params = f"?status={status}&limit={limit}&nested=true"
        if after:
            params += f"&after={after}"
        return self._request("GET", f"/v2/orders{params}")

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/v2/orders/{order_id}")

    def get_account(self) -> dict:
        return self._request("GET", "/v2/account")

    def get_position(self, symbol: str) -> dict:
        return self._request("GET", f"/v2/positions/{symbol}")

    def get_positions(self) -> list:
        return self._request("GET", "/v2/positions")

    def close_position(self, symbol: str) -> dict:
        return self._request("DELETE", f"/v2/positions/{symbol}")

    def get_clock(self) -> dict:
        return self._request("GET", "/v2/clock")

    def is_market_open(self) -> bool:
        try:
            clock = self.get_clock()
            return clock.get("is_open", False)
        except AlpacaError:
            return False

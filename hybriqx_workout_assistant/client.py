"""Inspect empty quotes by default; --pay permits one capped paid request."""

import argparse
import base64
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import httpx
from x402.schemas.errors import PaymentError

from .contracts import WODScalingRequest, WODScalingResponse
from .fixtures import SYNTHETIC_REQUEST, SYNTHETIC_RESULT

DEFAULT_BASE_URL = "https://www.hybriqx.com"
ENDPOINT = "/api/v1/scale-wod"
USDC_ASSETS = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}
PRICE_ATOMIC = 50_000
MAX_USDC = Decimal("0.05")


class BuyerError(ValueError):
    """Safe error with financial state, without request bodies or SDK exceptions."""

    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = deepcopy(report)


class _Stop(ValueError):
    pass


def spend_cap(value: str | Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, Decimal)):
        raise ValueError("max_usdc must be an exact decimal string or Decimal")
    try:
        cap = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("max_usdc must be positive, finite and at most 0.05") from None
    if not cap.is_finite() or not 0 < cap <= MAX_USDC:
        raise ValueError("max_usdc must be positive, finite and at most 0.05")
    return cap


def request_url(base_url: str) -> str:
    if (
        type(base_url) is not str
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in base_url)
        or any(c in base_url for c in "\\%?#")
    ):
        raise ValueError("Unsafe service origin")
    parts = urlsplit(base_url)
    if (
        parts.scheme not in ("https", "http") or not parts.hostname
        or parts.username is not None or parts.password is not None
        or parts.path not in ("", "/")
        or parts.port == 0 or parts.netloc.endswith(":")
    ):
        raise ValueError("Service URL must be an origin without credentials, path or query")
    local = parts.hostname.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme == "http" and not local:
        raise ValueError("HTTP is allowed only for localhost/loopback")
    # Accessing port also rejects malformed and out-of-range ports.
    parts.port
    return base_url.rstrip("/") + ENDPOINT


def normalize_request(request: dict) -> dict:
    if type(request) is not dict:
        raise ValueError("Request must be a JSON object")
    return WODScalingRequest.model_validate(request).model_dump(mode="json")


def offline_demo() -> dict:
    return {
        "mode": "SYNTHETIC_ILLUSTRATION",
        "network_used": False,
        "payment_made": False,
        "request": deepcopy(SYNTHETIC_REQUEST),
        "result": deepcopy(SYNTHETIC_RESULT),
        "message": "Offline fixture, not individualized advice. execution_time_ms=0 is a "
                   "placeholder, not measured latency. No wallet, model or payment used.",
    }


def _check_status(response: httpx.Response, *, quote=False):
    status = response.status_code
    if 300 <= status < 400:
        raise _Stop(f"HTTP {status}: all redirects refused; use https://www.hybriqx.com")
    if quote and status == 402:
        return
    if not 200 <= status < 300:
        raise _Stop(f"HTTP {status}: request failed; STOP, no automatic retry")
    if quote:
        raise _Stop("Expected HTTP 402 for empty quote; no workout uploaded or payment authorized")


def validate_challenge(required, url: str, cap: Decimal):
    if required.x402_version != 2 or len(required.accepts) != 1:
        raise _Stop("Require x402 v2 with one exact USDC quote")
    if not required.resource or required.resource.url != url:
        raise _Stop("Quote resource must equal the exact requested endpoint; use canonical "
                    "https://www.hybriqx.com (apex/www are different origins)")
    option = required.accepts[0]
    asset = USDC_ASSETS.get(option.network)
    if option.scheme != "exact" or asset is None or option.asset.lower() != asset.lower():
        raise _Stop("Only exact USDC on Base or Base Sepolia is supported")
    if not re.fullmatch(r"[0-9]+", option.amount) or int(option.amount) != PRICE_ATOMIC:
        raise _Stop("Quote must be exactly 50000 atomic USDC (0.05)")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", option.pay_to) or int(option.pay_to, 16) == 0:
        raise _Stop("Invalid nonzero payment recipient")
    if type(option.max_timeout_seconds) is not int or not 1 <= option.max_timeout_seconds <= 300:
        raise _Stop("Payment timeout must be 1..300 seconds")
    numerator, denominator = cap.as_integer_ratio()
    if PRICE_ATOMIC * denominator > numerator * 1_000_000:
        raise _Stop("Quote exceeds max_usdc; no wallet loaded or signature created")
    return option


def make_sdk(network: str):
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import ExactEvmClientScheme

    key = os.environ.get("X402_CLIENT_PRIVATE_KEY")
    if not key:
        raise _Stop("--pay requires X402_CLIENT_PRIVATE_KEY")
    try:
        account = Account.from_key(key)
    except (ValueError, TypeError):
        raise _Stop("Invalid X402_CLIENT_PRIVATE_KEY") from None
    sdk = x402ClientSync()
    sdk.register(network, ExactEvmClientScheme(EthAccountSigner(account)))
    return sdk


def adapt_workout(
    base_url: str,
    request: dict,
    *,
    pay: bool = False,
    max_usdc: str | Decimal = "0.05",
    transport=None,
) -> dict:
    """Validate locally, inspect an empty quote, optionally authorize one paid retry."""
    report = {
        "mode": "INSPECT_ONLY",
        "authorized_usdc": "0",
        "confirmed_spent_usdc": "0",
        "payment_made": False,
        "settlement_status": "not_authorized",
        "message": "Inspection uploads no workout or notes. Fees buy one advisory adaptation, "
                   "not a coaching-app subscription. This client executes no exercises.",
    }
    try:
        if type(pay) is not bool:
            raise ValueError("pay must be a literal boolean")
        body = normalize_request(request)
        url = request_url(base_url)
        cap = spend_cap(max_usdc)
    except (ValueError, TypeError, OverflowError):
        raise BuyerError("Invalid request, origin, pay flag or exact spend cap; no network used",
                         report) from None
    report.update(mode="PAY" if pay else "INSPECT_ONLY", max_usdc=str(cap))
    try:
        from x402 import x402ClientSync
        from x402.http import x402HTTPClientSync

        decoder = x402HTTPClientSync(x402ClientSync())
        with httpx.Client(
            transport=transport, follow_redirects=False, trust_env=False, timeout=30,
            headers={"X-Client-Name": "hybriqx-workout-assistant",
                     "User-Agent": "hybriqx-workout-assistant/0.1.0"},
        ) as client:
            probe = client.post(url, json={})
            _record_status(report, probe)
            _check_status(probe, quote=True)
            try:
                quote_body = probe.json()
            except ValueError:
                quote_body = {}
            required = decoder.get_payment_required_response(probe.headers.get, quote_body)
            _strict_metadata(probe.headers.get("PAYMENT-REQUIRED"), type(required), version=2)
            option = validate_challenge(required, url, cap)
            report["quote"] = {
                "network": option.network, "asset": option.asset, "amount_atomic": option.amount,
                "pay_to": option.pay_to, "max_timeout_seconds": option.max_timeout_seconds,
            }
            if not pay:
                return report
            sdk = make_sdk(option.network)
            decoder = x402HTTPClientSync(sdk)
            # Reserve before signing; failure may leave an authorization outside this process.
            report.update(authorized_usdc="0.05", settlement_status="unconfirmed")
            headers = decoder.encode_payment_signature_header(sdk.create_payment_payload(
                required.model_copy(update={"accepts": [option]}),
            ))
            response = client.post(url, json=body, headers=headers, timeout=60)
            _record_status(report, response)
            _record_settlement(report, response, decoder, option)
            _check_status(response)
            if report["settlement_status"] != "confirmed_by_sdk_response":
                raise _Stop("Payment unconfirmed; STOP, do not automatically authorize again")
            # Never attach raw response bodies to errors: they can echo private notes.
            result = WODScalingResponse.model_validate(response.json())
            report["result"] = result.model_dump(mode="json")
            report["message"] = (
                "Private advisory result: do not publish this JSON. Settlement is reported "
                "by the SDK response, not independently verified on-chain. No medical safety "
                "or exercise efficacy guarantee; no exercises executed."
            )
            return report
    except _Stop as exc:
        raise BuyerError(str(exc), report) from None
    except (ValueError, TypeError, RuntimeError, httpx.HTTPError, PaymentError):
        raise BuyerError("Transport, SDK or response validation failed; STOP, no automatic retry",
                         report) from None


def _record_status(report, response):
    report["http_status"] = response.status_code
    if response.status_code == 429:
        retry = response.headers.get("Retry-After")
        report["retry_after"] = retry if retry and re.fullmatch(r"\d{1,10}", retry) else (
            "present; consult operator before retry" if retry else "not supplied"
        )


def _strict_metadata(header, schema, *, version=None):
    # The SDK handles wire decoding; additionally forbid its default type coercions.
    raw = base64.b64decode(header, validate=True)
    schema.model_validate_json(raw, strict=True)
    if version is not None and json.loads(raw).get("x402Version") != version:
        raise _Stop("Payment quote must explicitly declare x402Version 2")


def _record_settlement(report, response, decoder, option):
    if not (
        response.headers.get("PAYMENT-RESPONSE") or response.headers.get("X-PAYMENT-RESPONSE")
    ):
        return
    settlement = decoder.get_payment_settle_response(response.headers.get)
    _strict_metadata(
        response.headers.get("PAYMENT-RESPONSE") or response.headers.get("X-PAYMENT-RESPONSE"),
        type(settlement),
    )
    if (
        settlement.network != option.network
        or (settlement.amount is not None and settlement.amount != option.amount)
        or not re.fullmatch(r"0x[0-9a-fA-F]{64}", settlement.transaction or "")
        or int(settlement.transaction, 16) == 0
    ):
        raise _Stop("Settlement metadata mismatch; payment unconfirmed; STOP")
    if settlement.success is not True:
        report["settlement_status"] = "failed"
        raise _Stop("SDK reports settlement failure; STOP, no automatic retry")
    report.update(
        confirmed_spent_usdc="0.05", payment_made=True,
        settlement_status="confirmed_by_sdk_response", transaction=settlement.transaction,
    )


run_workout = adapt_workout


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid CLI arguments; use --help")


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--request", type=Path, help="local private request JSON; required live")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--pay", action="store_true", help="permit one paid retry up to 0.05 USDC")
    parser.add_argument("--max-usdc", default="0.05")
    try:
        args = parser.parse_args(argv)
        if args.demo:
            if args.request or args.pay or args.base_url != DEFAULT_BASE_URL or args.max_usdc != "0.05":
                raise ValueError("Demo cannot be combined with live arguments")
            result = offline_demo()
        else:
            if not args.request:
                raise ValueError("Live mode requires --request; use --demo offline")
            request = json.loads(args.request.read_text(encoding="utf-8"))
            result = adapt_workout(args.base_url, request, pay=args.pay, max_usdc=args.max_usdc)
    except BuyerError as exc:
        print(json.dumps({"error": str(exc), "report": exc.report}, allow_nan=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "Invalid CLI arguments or unreadable request; no automatic retry"}),
              file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0

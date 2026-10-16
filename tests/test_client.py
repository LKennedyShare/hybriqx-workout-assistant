from copy import deepcopy
import base64
from decimal import Decimal, localcontext
import json
from pathlib import Path

import httpx
import pytest
from x402.http import encode_payment_required_header, encode_payment_response_header
from x402.schemas import PaymentPayload, PaymentRequired, SettleResponse

from hybriqx_workout_assistant import client as buyer
from hybriqx_workout_assistant.contracts import WODScalingRequest, WODScalingResponse
from hybriqx_workout_assistant.fixtures import SYNTHETIC_RESULT

BASE = buyer.DEFAULT_BASE_URL
NETWORK = "eip155:84532"


def required(**changes):
    option = {
        "scheme": "exact", "network": NETWORK, "asset": buyer.USDC_ASSETS[NETWORK],
        "amount": "50000", "payTo": "0x" + "1" * 40, "maxTimeoutSeconds": 60,
        "extra": {"name": "USDC", "version": "2"},
    }
    option.update(changes)
    return PaymentRequired.model_validate({
        "x402Version": 2, "resource": {"url": BASE + buyer.ENDPOINT}, "accepts": [option],
    })


def challenge(quote=None, *, body=None):
    quote = quote or required()
    return httpx.Response(402, json={} if body is None else body, headers={
        "PAYMENT-REQUIRED": encode_payment_required_header(quote),
    })


def settled(*, status=200, result=None, **changes):
    metadata = {
        "success": True, "network": NETWORK, "transaction": "0x" + "a" * 64,
        "amount": "50000",
    }
    metadata.update(changes)
    response = SettleResponse.model_validate(metadata)
    return httpx.Response(status, json=deepcopy(SYNTHETIC_RESULT) if result is None else result,
                          headers={"PAYMENT-RESPONSE": encode_payment_response_header(response)})


class FakeSDK:
    def __init__(self):
        self.signed = []

    def create_payment_payload(self, quote):
        self.signed.append(quote)
        return PaymentPayload.model_validate({
            "x402Version": 2, "accepted": quote.accepts[0], "resource": quote.resource,
            "payload": {"signature": "SYNTHETIC-NOT-A-SIGNATURE"},
        })


@pytest.fixture
def sdk(monkeypatch):
    sdk = FakeSDK()
    monkeypatch.setattr(buyer, "make_sdk", lambda network: sdk)
    return sdk


def flow(*, quote=None, response=None):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            assert json.loads(request.content) == {}
            assert "PAYMENT-SIGNATURE" not in request.headers
            return challenge(quote)
        assert len(calls) == 2
        assert "PAYMENT-SIGNATURE" in request.headers
        return response if response is not None else settled()

    return calls, httpx.MockTransport(handle)


def forbidden(*args, **kwargs):
    pytest.fail("Wallet/signing/network must not be reached")


def test_inspection_empty_body_and_private_report(monkeypatch, request_body):
    monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "NEVER-READ-KEY")
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    calls, transport = flow()
    result = buyer.adapt_workout(BASE, request_body, transport=transport)
    assert len(calls) == 1
    assert result["mode"] == "INSPECT_ONLY"
    assert result["authorized_usdc"] == "0"
    assert result["payment_made"] is False
    rendered = json.dumps(result)
    assert "PRIVATE-HEALTH-NOTES" not in rendered
    assert request_body["original_wod"] not in rendered
    assert "NEVER-READ-KEY" not in rendered


def test_paid_single_retry_normalized_body_and_sdk_settlement(request_body, sdk):
    request_body["available_equipment"] = [" rowing machine "]
    calls, transport = flow()
    result = buyer.run_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 2 and len(sdk.signed) == 1
    assert json.loads(calls[1].content) == buyer.normalize_request(request_body)
    assert request_body["available_equipment"] == [" rowing machine "]
    assert result["authorized_usdc"] == "0.05"
    assert result["confirmed_spent_usdc"] == "0.05"
    assert result["settlement_status"] == "confirmed_by_sdk_response"
    assert result["result"] == SYNTHETIC_RESULT


@pytest.mark.parametrize("network", list(buyer.USDC_ASSETS))
def test_supported_networks(network, request_body):
    calls, transport = flow(quote=required(network=network, asset=buyer.USDC_ASSETS[network]))
    result = buyer.run_workout(BASE, request_body, transport=transport)
    assert result["quote"]["network"] == network and len(calls) == 1


@pytest.mark.parametrize("changes", [
    {"network": "eip155:1"}, {"asset": "0x" + "2" * 40}, {"scheme": "upto"},
    {"amount": "49999"}, {"amount": "50001"}, {"amount": "0"}, {"amount": "-1"},
    {"amount": "5e4"}, {"amount": "50000.0"},
    {"payTo": "0x" + "0" * 40}, {"payTo": "not-an-address"},
    {"maxTimeoutSeconds": 0}, {"maxTimeoutSeconds": 301},
])
def test_invalid_quote_before_wallet(changes, request_body, monkeypatch):
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    calls, transport = flow(quote=required(**changes))
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 1 and error.value.report["authorized_usdc"] == "0"


@pytest.mark.parametrize("mutate", [
    lambda q: q.model_copy(update={"x402_version": 1}),
    lambda q: q.model_copy(update={"accepts": []}),
    lambda q: q.model_copy(update={"accepts": q.accepts * 2}),
    lambda q: q.model_copy(update={"resource": None}),
    lambda q: q.model_copy(update={"resource": q.resource.model_copy(update={
        "url": "https://hybriqx.com" + buyer.ENDPOINT})}),
    lambda q: q.model_copy(update={"resource": q.resource.model_copy(update={
        "url": BASE + "/v1/wod/scale"})}),
    lambda q: q.model_copy(update={"resource": q.resource.model_copy(update={
        "url": BASE + buyer.ENDPOINT + "?notes=private"})}),
])
def test_ambiguous_and_crossorigin_quote_refused(mutate, request_body, monkeypatch):
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    calls, transport = flow(quote=mutate(required()))
    with pytest.raises(buyer.BuyerError):
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 1


@pytest.mark.parametrize("url", [
    "http://www.hybriqx.com", "https://user:pass@www.hybriqx.com",
    "https://www.hybriqx.com?x=1", "https://www.hybriqx.com?",
    "https://www.hybriqx.com/#x", "https://www.hybriqx.com/#",
    "https://www.hybriqx.com/other", "https://www.hybriqx.com:0",
    "https://www.hybriqx.com:65536", "https://www.hybriqx.com:bad",
    "https://www.hybriqx.com:", "https://www.hybriqx.com\\evil",
    "https://www.hybriqx.com%2fevil", "https://www.hybriqx.com\n",
    "//www.hybriqx.com", None,
])
def test_invalid_origin_no_http(url, request_body):
    with pytest.raises(buyer.BuyerError, match="Invalid"):
        buyer.adapt_workout(url, request_body, transport=httpx.MockTransport(forbidden))


@pytest.mark.parametrize("url", [
    BASE, BASE + "/", "http://localhost:8080", "http://127.0.0.1:80",
    "http://[::1]:8080", "https://example.org:443",
])
def test_valid_origins(url):
    assert buyer.request_url(url) == url.rstrip("/") + buyer.ENDPOINT


@pytest.mark.parametrize("cap", [
    True, False, 0.05, 1, 0, None, "0", "-1", "nan", "NaN", "Infinity",
    "-Infinity", "0.050000000000000000000000000001", "bad", Decimal("sNaN"),
])
def test_invalid_cap_no_http(cap, request_body):
    with pytest.raises(buyer.BuyerError, match="Invalid"):
        buyer.adapt_workout(BASE, request_body, max_usdc=cap,
                           transport=httpx.MockTransport(forbidden))


def test_exact_cap_not_rounded_up(request_body, monkeypatch):
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    calls, transport = flow()
    with localcontext() as ctx:
        ctx.prec = 2
        with pytest.raises(buyer.BuyerError, match="exceeds"):
            buyer.adapt_workout(BASE, request_body, pay=True,
                               max_usdc="0.0499999999999999999999999999999", transport=transport)
    assert len(calls) == 1


@pytest.mark.parametrize("pay", [1, 0, "true", "false", None, [], {}])
def test_pay_literal_bool(pay, request_body):
    with pytest.raises(buyer.BuyerError, match="Invalid"):
        buyer.adapt_workout(BASE, request_body, pay=pay,
                           transport=httpx.MockTransport(forbidden))


@pytest.mark.parametrize("changes", [
    {"original_wod": ""}, {"original_wod": "  "}, {"original_wod": "a" * 5001},
    {"original_wod": 1}, {"available_equipment": [" "]}, {"available_equipment": [2]},
    {"available_equipment": ["x"] * 51}, {"available_equipment": None},
    {"target_time_domain_mins": True}, {"target_time_domain_mins": "15"},
    {"target_time_domain_mins": 0}, {"target_time_domain_mins": 241},
    {"target_time_domain_mins": 1.5}, {"athlete_notes": "x" * 1001}, {"athlete_notes": 0},
    {"unknown": "PRIVATE-MUST-NOT-ECHO"},
])
def test_invalid_request_before_http(changes, request_body):
    request_body.update(changes)
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, transport=httpx.MockTransport(forbidden))
    assert "PRIVATE" not in str(error.value) + json.dumps(error.value.report)


def test_contract_defaults_normalization_and_nulls():
    result = WODScalingRequest.model_validate({"original_wod": " x ", "available_equipment": []})
    assert result.original_wod == "x" and result.target_time_domain_mins == 15
    assert result.athlete_notes is None
    assert WODScalingRequest.model_validate({
        "original_wod": "x", "available_equipment": [" rower "],
        "target_time_domain_mins": None,
    }).available_equipment == ["rower"]


@pytest.mark.parametrize("status", [200, 204, 301, 302, 307, 308, 400, 422, 429, 502, 504])
def test_probe_status_no_retry(status, request_body, monkeypatch):
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"echo": "PRIVATE-HEALTH-NOTES"}, headers={
            "Location": BASE + buyer.ENDPOINT, "Retry-After": "60",
        })

    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert error.value.report["http_status"] == status
    if status == 429:
        assert error.value.report["retry_after"] == "60"
    assert "PRIVATE-HEALTH-NOTES" not in json.dumps(error.value.report)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 402, 422, 429, 502, 504])
def test_paid_error_never_retried(status, request_body, sdk):
    calls, transport = flow(response=httpx.Response(status, json={"error": "PRIVATE-HEALTH-NOTES"},
                                                    headers={"Retry-After": "20", "Location": BASE}))
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 2 and len(sdk.signed) == 1
    assert error.value.report["authorized_usdc"] == "0.05"
    assert error.value.report["settlement_status"] == "unconfirmed"
    assert "PRIVATE-HEALTH-NOTES" not in json.dumps(error.value.report)


@pytest.mark.parametrize("metadata", [
    {"network": "eip155:8453"}, {"amount": "1"},
    {"transaction": ""}, {"transaction": "0x" + "0" * 64}, {"transaction": "bad"},
    {"success": False},
])
def test_bad_settlement_stops(metadata, request_body, sdk):
    calls, transport = flow(response=settled(**metadata))
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 2 and error.value.report["payment_made"] is False
    assert error.value.report["authorized_usdc"] == "0.05"


def test_optional_settlement_amount_omitted(request_body, sdk):
    calls, transport = flow(response=settled(amount=None))
    result = buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 2
    assert result["settlement_status"] == "confirmed_by_sdk_response"
    assert result["confirmed_spent_usdc"] == "0.05"
    assert result["result"] == SYNTHETIC_RESULT


def test_missing_settlement_valid_response_stops(request_body, sdk):
    calls, transport = flow(response=httpx.Response(200, json=SYNTHETIC_RESULT))
    with pytest.raises(buyer.BuyerError, match="unconfirmed") as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert error.value.report["settlement_status"] == "unconfirmed"
    assert "result" not in error.value.report and len(calls) == 2


@pytest.mark.parametrize("header", ["not-base64", "e30="])
def test_malformed_settlement_safe(request_body, sdk, header):
    calls, transport = flow(response=httpx.Response(200, json={"PRIVATE": "HEALTH"}, headers={
        "PAYMENT-RESPONSE": header,
    }))
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 2 and "PRIVATE" not in str(error.value) + json.dumps(error.value.report)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_nonfinite_result_rejected_and_settlement_retained(value, request_body, sdk):
    result = {**SYNTHETIC_RESULT, "execution_time_ms": value}
    with pytest.raises(ValueError):
        WODScalingResponse.model_validate(result)
    response = settled()
    response._content = json.dumps(result).encode()
    calls, transport = flow(response=response)
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert error.value.report["confirmed_spent_usdc"] == "0.05"
    assert error.value.report["payment_made"] is True
    assert "result" not in error.value.report and len(calls) == 2


def test_http_error_preserves_reported_settlement(request_body, sdk):
    calls, transport = flow(response=settled(status=502, result={"private": "notes"}))
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert error.value.report["confirmed_spent_usdc"] == "0.05"
    assert error.value.report["http_status"] == 502 and len(calls) == 2


def test_signing_failure_keeps_authorization_without_sensitive_exception(request_body, sdk):
    def fail(quote):
        raise RuntimeError("SENSITIVE-KEY-CONTENT")

    sdk.create_payment_payload = fail
    calls, transport = flow()
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 1
    assert error.value.report["authorized_usdc"] == "0.05"
    assert "SENSITIVE" not in str(error.value)


@pytest.mark.parametrize("field,value", [("maxTimeoutSeconds", True), ("maxTimeoutSeconds", "60")])
def test_sdk_quote_type_coercion_refused(field, value, request_body, monkeypatch):
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    quote = required().model_dump(by_alias=True)
    quote["accepts"][0][field] = value
    header = base64.b64encode(json.dumps(quote).encode()).decode()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(402, json={}, headers={"PAYMENT-REQUIRED": header})

    with pytest.raises(buyer.BuyerError):
        buyer.adapt_workout(BASE, request_body, pay=True, transport=httpx.MockTransport(handler))
    assert len(calls) == 1


def test_sdk_settlement_success_coercion_refused(request_body, sdk):
    metadata = {"success": "true", "network": NETWORK, "amount": "50000",
                "transaction": "0x" + "a" * 64}
    header = base64.b64encode(json.dumps(metadata).encode()).decode()
    calls, transport = flow(response=httpx.Response(200, json=SYNTHETIC_RESULT, headers={
        "PAYMENT-RESPONSE": header,
    }))
    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert error.value.report["settlement_status"] == "unconfirmed" and len(calls) == 2


def test_missing_explicit_x402_version_refused(request_body, monkeypatch):
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    quote = required().model_dump(by_alias=True)
    del quote["x402Version"]
    header = base64.b64encode(json.dumps(quote).encode()).decode()

    def handler(request):
        return httpx.Response(402, json={}, headers={"PAYMENT-REQUIRED": header})

    with pytest.raises(buyer.BuyerError, match="explicitly"):
        buyer.adapt_workout(BASE, request_body, pay=True, transport=httpx.MockTransport(handler))


def test_transport_timeout_never_retried(request_body, sdk):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return challenge()
        raise httpx.ReadTimeout("PRIVATE-RAW-WORKOUT-OR-KEY", request=request)

    with pytest.raises(buyer.BuyerError) as error:
        buyer.adapt_workout(BASE, request_body, pay=True, transport=httpx.MockTransport(handler))
    assert len(calls) == 2
    assert error.value.report["authorized_usdc"] == "0.05"
    assert "PRIVATE" not in str(error.value)


def test_sdk_registers_exact_network_without_signing(monkeypatch):
    import eth_account
    import x402
    import x402.mechanisms.evm
    import x402.mechanisms.evm.exact

    registrations = []
    monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "SYNTHETIC-KEY-NOT-VALID")
    monkeypatch.setattr(eth_account.Account, "from_key", lambda key: object())
    monkeypatch.setattr(x402.mechanisms.evm, "EthAccountSigner", lambda account: object())
    monkeypatch.setattr(x402.mechanisms.evm.exact, "ExactEvmClientScheme", lambda signer: object())

    class Registry:
        def register(self, network, scheme):
            registrations.append(network)

    monkeypatch.setattr(x402, "x402ClientSync", Registry)
    buyer.make_sdk(NETWORK)
    assert registrations == [NETWORK]


def test_no_key_required_inspect_but_paid_requires_key(monkeypatch, request_body):
    monkeypatch.delenv("X402_CLIENT_PRIVATE_KEY", raising=False)
    calls, transport = flow()
    with pytest.raises(buyer.BuyerError, match="requires"):
        buyer.adapt_workout(BASE, request_body, pay=True, transport=transport)
    assert len(calls) == 1


def test_offline_demo_cli_never_uses_network_wallet(monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    assert buyer.main(["--demo"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["mode"] == "SYNTHETIC_ILLUSTRATION"
    assert result["result"]["execution_time_ms"] == 0
    assert result["payment_made"] is False and captured.err == ""


@pytest.mark.parametrize("args", [[], ["--unknown"], ["--pay"], ["--demo", "--pay"],
                                  ["--request", "missing-private-request.json"]])
def test_cli_errors_one_json_stderr(args, capsys):
    assert buyer.main(args) != 0
    captured = capsys.readouterr()
    assert captured.out == "" and isinstance(json.loads(captured.err), dict)


@pytest.fixture
def private_request_path(request_body):
    path = Path(__file__).parent / "_private_request.json"
    path.write_text(json.dumps(request_body), encoding="utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_cli_live_and_error_json(private_request_path, request_body, monkeypatch, capsys):
    path = private_request_path
    calls = []

    def run(base_url, request, **kwargs):
        calls.append((base_url, request, kwargs))
        return {"mode": "INSPECT_ONLY"}

    monkeypatch.setattr(buyer, "adapt_workout", run)
    assert buyer.main(["--request", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "INSPECT_ONLY"}
    assert calls[0][2]["pay"] is False

    def fail(*args, **kwargs):
        raise buyer.BuyerError("STOP", {"authorized_usdc": "0.05"})

    monkeypatch.setattr(buyer, "adapt_workout", fail)
    assert buyer.main(["--request", str(path), "--pay"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["report"]["authorized_usdc"] == "0.05"

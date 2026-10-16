"""Unpaid public diagnostics, standard library only; never sign, pay or retry.

PyPI 404 is pending by default; --require-pypi makes it a failure.
Reports contain only fixed check names/statuses. contract_mismatch means inspect
the public schema/configuration; http_error means check public availability;
network_error/timeout means retry the workflow later, not the paid operation.
"""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

LABEL = "scout-distribution-monitor"
ORIGIN = "https://www.hybriqx.com"
MCP = ORIGIN + "/mcp/"
REPOSITORY = "https://github.com/LKennedyShare/hybriqx-workout-assistant"
PYPI = "https://pypi.org/pypi/hybriqx-workout-assistant/json"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
MAX_BODY = 2_000_000
TIMEOUT = 15
PROTOCOL = "2025-03-26"
GET_URLS = {ORIGIN + path for path in (
    "/health", "/agents", "/buyers.txt", "/openapi.json",
    "/.well-known/agent-card.json", "/.well-known/mcp/server-card.json",
)} | {PYPI}
POST_URLS = {MCP, ORIGIN + "/api/v1/scale-wod"}
FAILURES = {"contract_mismatch", "http_error", "network_error", "timeout",
            "redirect_refused", "body_too_large", "invalid_response", "blocked"}


class CheckError(Exception):
    """Only fixed status codes, never remote text."""


def need(condition, status="contract_mismatch"):
    if not condition:
        raise CheckError(status)


def guard(method, url):
    need((method == "GET" and url in GET_URLS)
         or (method == "POST" and url in POST_URLS))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckError("redirect_refused")


def transport(method, url, payload=None):
    guard(method, url)
    request = urllib.request.Request(
        url, method=method,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"User-Agent": LABEL, "X-Client-Name": LABEL,
                 "Accept": "application/json, text/event-stream",
                 "Content-Type": "application/json", "MCP-Protocol-Version": PROTOCOL},
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        need(not 300 <= response.code < 400, "redirect_refused")
        body = response.read(MAX_BODY + 1)
        need(len(body) <= MAX_BODY, "body_too_large")
        return response.code, {k.lower(): v for k, v in response.headers.items()}, body


def rpc_body(body, headers):
    if headers.get("content-type", "").split(";")[0].strip() == "text/event-stream":
        events = [
            json.loads("\n".join(line[5:].lstrip() for line in block.splitlines()
                                 if line.startswith("data:")))
            for block in body.decode("utf-8").replace("\r\n", "\n").split("\n\n")
            if any(line.startswith("data:") for line in block.splitlines())
        ]
        need(len(events) == 1)
        return events[0]
    return json.loads(body)


def objects(value):
    """Walk JSON and MCP text content locally, without dereferencing links."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except ValueError:
            return
        yield from objects(parsed)


def payment(value, resource):
    envelopes = [obj for obj in objects(value) if "accepts" in obj]
    need(bool(envelopes))
    for envelope in envelopes:
        need(envelope["x402Version"] == 2)
        need(envelope["resource"]["url"] == resource)
        options = envelope["accepts"]
        need(isinstance(options, list) and len(options) == 1)
        option = options[0]
        need(option["scheme"] == "exact" and option["network"] == "eip155:8453")
        need(option["asset"].lower() == USDC and option["amount"] == "50000")
        need(bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", option["payTo"])))
        need(int(option["payTo"][2:], 16) != 0)
        need(type(option["maxTimeoutSeconds"]) is int
             and option["maxTimeoutSeconds"] > 0)


def tool_map(tools):
    need(isinstance(tools, list) and bool(tools))
    result = {}
    for tool in tools:
        name, schema = tool["name"], tool["inputSchema"]
        need(isinstance(name, str) and name not in result)
        need(schema["type"] == "object" and isinstance(schema["properties"], dict))
        need(isinstance(schema.get("required", []), list))
        need(set(schema.get("required", [])) <= schema["properties"].keys())
        result[name] = tool
    return result


def check_request(schema):
    need(schema["type"] == "object")
    need(set(schema["required"]) == {"original_wod", "available_equipment"})
    props = schema["properties"]
    need(props["original_wod"]["type"] == "string")
    need(props["available_equipment"]["type"] == "array")
    need(props["available_equipment"]["items"]["type"] == "string")
    need({"target_time_domain_mins", "athlete_notes"} <= props.keys())


def check_openapi(doc):
    need(doc["openapi"].startswith("3."))
    models = doc["components"]["schemas"]

    def resolve(schema):
        if "$ref" in schema:
            prefix = "#/components/schemas/"
            need(schema["$ref"].startswith(prefix))
            return models[schema["$ref"][len(prefix):]]
        return schema

    for path in ("/api/v1/scale-wod", "/v1/wod/scale"):
        operation = doc["paths"][path]["post"]
        check_request(resolve(operation["requestBody"]["content"]["application/json"]["schema"]))
        response = resolve(operation["responses"]["200"]["content"]["application/json"]["schema"])
        need(response["type"] == "object")
        expected = {"scaled_wod": "string", "movement_substitutions": "array",
                    "target_stimulus": "string", "execution_time_ms": "number"}
        need(set(response["required"]) == set(expected))
        for name, kind in expected.items():
            need(response["properties"][name]["type"] == kind)
        need(response["properties"]["movement_substitutions"]["items"]["type"] == "string")


def monitor(require_pypi=False, send=None):
    send = transport if send is None else send
    checks = []
    ready = {"handshake": False, "schema": False, "payment": False}
    request_id = 0

    def run(name, check):
        try:
            status = check() or "ok"
        except CheckError as exc:
            status = str(exc) if str(exc) in FAILURES else "contract_mismatch"
        except (socket.timeout, TimeoutError):
            status = "timeout"
        except (OSError, urllib.error.URLError):
            status = "network_error"
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError, binascii.Error):
            status = "invalid_response"
        checks.append({"check": name, "status": status})

    def request(url, method="GET", payload=None, expected=200):
        guard(method, url)
        status, headers, body = send(method, url, payload)
        need(not 300 <= status < 400, "redirect_refused")
        need(len(body) <= MAX_BODY, "body_too_large")
        need(status == expected, "http_error")
        return headers, body

    def document(url):
        return json.loads(request(url)[1])

    def rpc(method, params=None):
        nonlocal request_id
        request_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers, body = request(MCP, "POST", payload)
        response = rpc_body(body, headers)
        need(response["jsonrpc"] == "2.0" and response["id"] == request_id
             and "error" not in response)
        return response["result"]

    def health():
        need(document(ORIGIN + "/health")["status"] == "ok")

    def links(path):
        need(REPOSITORY in request(ORIGIN + path)[1].decode("utf-8"))

    def agent_card():
        card = document(ORIGIN + "/.well-known/agent-card.json")
        need(card["descriptor_type"] == "custom-service-discovery")
        need(card["a2a_supported"] is False)
        need(set(card["protocols"]) == {"REST", "x402", "MCP"})
        need(not ({"preferredTransport", "supportedInterfaces", "skills"} & card.keys()))
        need(card["links"]["mcp"] == MCP)
        need(card["capabilities"]["workout_adaptation"]["mcp_tool"] == "scale_workout")
        need(card["capabilities"]["service_info"] == {"mcp_tool": "service_info", "price": "free"})

    def initialize():
        result = rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": LABEL, "version": "1.0"}})
        need(result["protocolVersion"] == PROTOCOL)
        need(isinstance(result["serverInfo"]["name"], str))
        need(isinstance(result["capabilities"]["tools"], dict))
        request(MCP, "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                expected=202)
        ready["handshake"] = True

    def mcp_schema():
        need(ready["handshake"], "blocked")
        card = document(ORIGIN + "/.well-known/mcp/server-card.json")
        need(card["descriptor_type"] == "custom-mcp-server-metadata")
        need(card["endpoint"] == MCP and card["transport"] == "streamable-http")
        need(card["stateless"] is True)
        advertised, actual = tool_map(card["tools"]), tool_map(rpc("tools/list")["tools"])
        need(set(advertised) == set(actual) == {"service_info", "scale_workout"})
        for name in actual:
            need(advertised[name]["inputSchema"] == actual[name]["inputSchema"])
            need(advertised[name].get("outputSchema") == actual[name].get("outputSchema"))
        check_request(actual["scale_workout"]["inputSchema"])
        need(advertised["service_info"]["payment"] == {"required": False, "price": "free"})
        need(advertised["service_info"]["available"] is True)
        need(advertised["scale_workout"]["payment"]["required"] is True)
        ready["schema"] = True

    def service_info():
        need(ready["schema"], "blocked")
        result = rpc("tools/call", {"name": "service_info", "arguments": {}})
        need(result.get("isError") is not True)
        info = next((obj for obj in objects(result) if "wod_scaling_enabled" in obj), None)
        need(info is not None)
        need(info["wod_scaling_enabled"] is True and info["payments_enabled"] is True)
        need(info["payment_required"] is True and info["wod_scaling_demo"] is False)
        need(info["wod_scaling_endpoint"] == "/v1/wod/scale")
        need(info["wod_scaling_price"] == "$0.05" and info["network"] == "eip155:8453")
        ready["payment"] = True

    def rest_payment():
        headers, _ = request(ORIGIN + "/api/v1/scale-wod", "POST", {}, 402)
        payment(json.loads(base64.b64decode(headers["payment-required"], validate=True)),
                ORIGIN + "/api/v1/scale-wod")

    def mcp_payment():
        need(ready["payment"], "blocked")
        result = rpc("tools/call", {"name": "scale_workout", "arguments": {
            "original_wod": "SYNTHETIC monitor: 1 air squat",
            "available_equipment": [],
        }})
        need(result.get("isError") is True)
        payment(result, MCP)

    def pypi():
        status, _, body = send("GET", PYPI, None)
        need(not 300 <= status < 400, "redirect_refused")
        need(len(body) <= MAX_BODY, "body_too_large")
        if status == 404 and not require_pypi:
            return "pending"
        need(status == 200, "http_error")
        info = json.loads(body)["info"]
        need(info["name"] == "hybriqx-workout-assistant")
        need(info["project_urls"]["Repository"] == REPOSITORY)
        need((info.get("license_expression") or info.get("license")) == "MIT")

    run("health", health)
    run("agents_repository_link", lambda: links("/agents"))
    run("buyers_repository_link", lambda: links("/buyers.txt"))
    run("openapi", lambda: check_openapi(document(ORIGIN + "/openapi.json")))
    run("custom_descriptor_not_a2a", agent_card)
    run("mcp_initialize", initialize)
    run("mcp_schema", mcp_schema)
    run("service_info_free_availability", service_info)
    run("rest_unpaid_challenge", rest_payment)
    run("mcp_unpaid_challenge", mcp_payment)
    run("pypi", pypi)
    return {"diagnostic": LABEL, "checks": checks,
            "status": "failed" if any(c["status"] in FAILURES for c in checks) else "ok"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-pypi", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = monitor(args.require_pypi)
    serialized = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        summary = "| Check | Status |\n| --- | --- |\n"
        summary += "".join(f"| {c['check']} | {c['status']} |\n" for c in report["checks"])
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("## Unpaid distribution diagnostics\n\n" + summary)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())

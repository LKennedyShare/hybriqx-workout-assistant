"""All public HTTP contracts are mocked; no service, payment or model is called."""

import ast
import base64
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket
import sys
import tomllib
import urllib.error
import uuid
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


discovery = load_script("check_discovery")
release = load_script("check_release")


@pytest.fixture
def scratch_path():
    path = ROOT / ".pytest_cache" / ("automation-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def request_schema():
    return {
        "type": "object", "required": ["original_wod", "available_equipment"],
        "properties": {
            "original_wod": {"type": "string"},
            "available_equipment": {"type": "array", "items": {"type": "string"}},
            "target_time_domain_mins": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "athlete_notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }


def terms(resource):
    return {
        "x402Version": 2, "resource": {"url": resource},
        "accepts": [{
            "scheme": "exact", "network": "eip155:8453", "asset": discovery.USDC,
            "amount": "50000", "payTo": "0x" + "1" * 40, "maxTimeoutSeconds": 300,
        }],
    }


class Backend:
    """Shapes mirror agent_docs.py, schemas.py and FastMCP's public contracts."""

    def __init__(self, sse=False):
        self.calls = []
        self.sse = sse
        self.pypi_status = 404
        self.payment = terms(discovery.MCP)
        self.rest_payment = terms(discovery.ORIGIN + "/api/v1/scale-wod")
        self.info = {
            "name": "Hybriqx Workout Adaptation", "query_endpoint": "/v1/query",
            "wod_scaling_endpoint": "/v1/wod/scale", "wod_scaling_demo": False,
            "wod_scaling_enabled": True, "wod_scaling_price": "$0.05",
            "payment_required": True, "payments_enabled": True, "network": "eip155:8453",
        }
        self.tools = [
            {"name": "service_info", "inputSchema": {"type": "object", "properties": {}},
             "outputSchema": {"type": "object", "properties": {}}},
            {"name": "scale_workout", "inputSchema": request_schema()},
        ]
        advertised = deepcopy(self.tools)
        advertised[0].update(available=True, payment={"required": False, "price": "free"})
        advertised[1].update(available=True, payment={"required": True, "price": "$0.05"})
        response = {
            "type": "object",
            "required": ["scaled_wod", "movement_substitutions", "target_stimulus", "execution_time_ms"],
            "properties": {
                "scaled_wod": {"type": "string"},
                "movement_substitutions": {"type": "array", "items": {"type": "string"}},
                "target_stimulus": {"type": "string"}, "execution_time_ms": {"type": "number"},
            },
        }
        operation = {
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/WODScalingRequest"}}}},
            "responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/WODScalingResponse"}}}}},
        }
        self.docs = {
            "/health": {"status": "ok", "timestamp": "PRIVATE-HEALTH-DATA"},
            "/agents": discovery.REPOSITORY, "/buyers.txt": discovery.REPOSITORY,
            "/openapi.json": {
                "openapi": "3.1.0", "paths": {
                    path: {"post": deepcopy(operation)}
                    for path in ("/api/v1/scale-wod", "/v1/wod/scale")},
                "components": {"schemas": {
                    "WODScalingRequest": request_schema(), "WODScalingResponse": response}},
            },
            "/.well-known/agent-card.json": {
                "descriptor_type": "custom-service-discovery", "a2a_supported": False,
                "protocols": ["REST", "x402", "MCP"], "links": {"mcp": discovery.MCP},
                "capabilities": {"workout_adaptation": {"mcp_tool": "scale_workout"},
                                 "service_info": {"mcp_tool": "service_info", "price": "free"}},
            },
            "/.well-known/mcp/server-card.json": {
                "descriptor_type": "custom-mcp-server-metadata", "endpoint": discovery.MCP,
                "transport": "streamable-http", "stateless": True, "tools": advertised,
            },
        }
        self.pypi_info = {"name": "hybriqx-workout-assistant",
                          "project_urls": {"Repository": discovery.REPOSITORY},
                          "license_expression": "MIT"}

    def __call__(self, method, url, payload=None):
        discovery.guard(method, url)
        self.calls.append((method, url, deepcopy(payload)))
        if url == discovery.PYPI:
            return self.pypi_status, {}, json.dumps({"info": self.pypi_info}).encode()
        if method == "GET":
            value = self.docs[url.removeprefix(discovery.ORIGIN)]
            return 200, {}, (value.encode() if isinstance(value, str)
                             else json.dumps(value).encode())
        if url.endswith("/api/v1/scale-wod"):
            assert payload == {}
            return 402, {"payment-required": base64.b64encode(
                json.dumps(self.rest_payment).encode()).decode()}, b"PRIVATE-REST-BODY"
        assert url == discovery.MCP
        rpc_method = payload["method"]
        if rpc_method == "notifications/initialized":
            assert "id" not in payload
            return 202, {}, b""
        if rpc_method == "initialize":
            assert payload["params"]["clientInfo"]["name"] == discovery.LABEL
            result = {"protocolVersion": discovery.PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Hybriqx Workout Adaptation", "version": "1"}}
        elif rpc_method == "tools/list":
            result = {"tools": self.tools}
        else:
            assert rpc_method == "tools/call"
            params = payload["params"]
            assert set(params) == {"name", "arguments"}  # No payment payload or _meta.
            if params["name"] == "service_info":
                assert params["arguments"] == {}
                result = {"structuredContent": self.info, "content": [], "isError": False}
            else:
                assert params["name"] == "scale_workout"
                assert params["arguments"] == {
                    "original_wod": "SYNTHETIC monitor: 1 air squat", "available_equipment": []}
                result = {"isError": True, "structuredContent": self.payment, "content": [
                    {"type": "text", "text": json.dumps(self.payment)}]}
        body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
        if self.sse:
            return 200, {"content-type": "text/event-stream; charset=utf-8"}, (
                ": keepalive\r\nevent: message\r\ndata: " + body + "\r\n\r\n").encode()
        return 200, {"content-type": "application/json"}, body.encode()


def statuses(report):
    return {item["check"]: item["status"] for item in report["checks"]}


@pytest.mark.parametrize("sse", [False, True])
def test_pending_pypi_and_unpaid_only(sse):
    backend = Backend(sse)
    report = discovery.monitor(send=backend)
    assert report["status"] == "ok"
    assert statuses(report)["pypi"] == "pending"
    assert len(report["checks"]) == 11
    assert set(statuses(report).values()) == {"ok", "pending"}
    assert "PRIVATE" not in json.dumps(report)
    posts = [(url, payload) for method, url, payload in backend.calls if method == "POST"]
    assert len(posts) == 6
    assert all(url in discovery.POST_URLS for url, _ in posts)
    assert not any("/a2a" in url or "/v1/query" in url or "/execute" in url
                   or "strava" in url or "profile" in url for _, url, _ in backend.calls)


@pytest.mark.parametrize("present,required,expected", [
    (False, False, "pending"), (False, True, "http_error"),
    (True, False, "ok"), (True, True, "ok"),
])
def test_pypi_policy(present, required, expected):
    backend = Backend()
    backend.pypi_status = 200 if present else 404
    report = discovery.monitor(required, backend)
    assert statuses(report)["pypi"] == expected
    assert report["status"] == ("failed" if expected == "http_error" else "ok")


@pytest.mark.parametrize("field,value", [
    ("name", "other"), ("license_expression", "Proprietary"),
    ("project_urls", {"Repository": "https://example.com/other"}),
])
def test_pypi_identity(field, value):
    backend = Backend()
    backend.pypi_status = 200
    backend.pypi_info[field] = value
    assert statuses(discovery.monitor(send=backend))["pypi"] == "contract_mismatch"


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("field,value", [
    ("amount", "5000"), ("amount", 50000), ("network", "eip155:1"),
    ("asset", "0x" + "2" * 40), ("scheme", "upto"), ("payTo", "0x" + "0" * 40),
    ("maxTimeoutSeconds", 0),
])
def test_payment_terms_are_exact(surface, field, value):
    backend = Backend()
    challenge = backend.rest_payment if surface == "rest" else backend.payment
    challenge["accepts"][0][field] = value
    assert statuses(discovery.monitor(send=backend))[
        surface + "_unpaid_challenge"] == "contract_mismatch"


@pytest.mark.parametrize("url", ["mcp://hybriqx/scale_workout",
                                discovery.ORIGIN + "/mcp", "https://evil.example/mcp/"])
def test_canonical_mcp_resource(url):
    backend = Backend()
    backend.payment["resource"]["url"] = url
    assert statuses(discovery.monitor(send=backend))["mcp_unpaid_challenge"] == "contract_mismatch"
    assert not any(call[1] == url for call in backend.calls if url != discovery.MCP)


@pytest.mark.parametrize("mutation", ["a2a", "alias", "response", "tools", "output", "links"])
def test_public_contract_drift(mutation):
    backend = Backend()
    check = "openapi"
    if mutation == "a2a":
        backend.docs["/.well-known/agent-card.json"]["a2a_supported"] = True
        check = "custom_descriptor_not_a2a"
    elif mutation == "alias":
        del backend.docs["/openapi.json"]["paths"]["/v1/wod/scale"]
    elif mutation == "response":
        backend.docs["/openapi.json"]["components"]["schemas"][
            "WODScalingResponse"]["properties"]["scaled_wod"]["type"] = "integer"
    elif mutation == "tools":
        backend.tools[1]["inputSchema"]["required"] = []
        check = "mcp_schema"
    elif mutation == "output":
        backend.tools[0]["outputSchema"]["properties"]["wrong"] = {"type": "string"}
        check = "mcp_schema"
    else:
        backend.docs["/agents"] = "No public repository yet"
        check = "agents_repository_link"
    report = discovery.monitor(send=backend)
    assert report["status"] == "failed"
    assert statuses(report)[check] in discovery.FAILURES


@pytest.mark.parametrize("field,value", [
    ("payments_enabled", False), ("payment_required", False),
    ("wod_scaling_enabled", False), ("wod_scaling_demo", True),
    ("wod_scaling_price", "$0.01"), ("network", "eip155:1"),
])
def test_unavailable_service_blocks_paid_tool_probe(field, value):
    backend = Backend()
    backend.info[field] = value
    report = discovery.monitor(send=backend)
    assert statuses(report)["service_info_free_availability"] == "contract_mismatch"
    assert statuses(report)["mcp_unpaid_challenge"] == "blocked"
    assert not any(payload and payload.get("params", {}).get("name") == "scale_workout"
                   for _, _, payload in backend.calls)


@pytest.mark.parametrize("mutation", ["id", "error", "protocol", "notification"])
def test_handshake_failure_blocks_subsequent_mcp_calls(mutation):
    backend = Backend()

    def send(method, url, payload=None):
        status, headers, body = backend(method, url, payload)
        if payload and payload.get("method") == "initialize":
            value = json.loads(body)
            if mutation == "id":
                value["id"] += 1
            elif mutation == "error":
                value["error"] = {"message": "PRIVATE"}
            elif mutation == "protocol":
                value["result"]["protocolVersion"] = "unsupported"
            body = json.dumps(value).encode()
        if payload and payload.get("method") == "notifications/initialized" and mutation == "notification":
            status = 500
        return status, headers, body

    report = discovery.monitor(send=send)
    assert report["status"] == "failed"
    assert statuses(report)["mcp_schema"] == "blocked"
    assert statuses(report)["mcp_unpaid_challenge"] == "blocked"
    assert not any(payload and payload.get("method") in ("tools/call", "tools/list")
                   for _, _, payload in backend.calls)
    assert "PRIVATE" not in json.dumps(report)


def test_mcp_schema_drift_blocks_tool_call():
    backend = Backend()
    backend.tools[1]["inputSchema"]["required"] = []
    report = discovery.monitor(send=backend)
    assert statuses(report)["mcp_schema"] == "contract_mismatch"
    assert not any(payload and payload.get("method") == "tools/call"
                   for _, _, payload in backend.calls)


@pytest.mark.parametrize("body", [b"data: {}\n\ndata: {}\n\n", b": ping\n\n",
                                  b"data: PRIVATE\n\n"])
def test_sse_requires_one_valid_json_response(body):
    with pytest.raises((ValueError, discovery.CheckError)):
        discovery.rpc_body(body, {"content-type": "text/event-stream"})


@pytest.mark.parametrize("problem,expected", [
    ("timeout", "timeout"), ("network", "network_error"), ("redirect", "redirect_refused"),
    ("oversize", "body_too_large"), ("json", "invalid_response"), ("http", "http_error"),
])
def test_sanitized_failures(problem, expected):
    backend = Backend()

    def send(method, url, payload=None):
        if url.endswith("/health"):
            if problem == "timeout":
                raise socket.timeout("PRIVATE")
            if problem == "network":
                raise urllib.error.URLError("PRIVATE")
            return {
                "redirect": (302, {"location": "https://evil.example/PRIVATE"}, b"PRIVATE"),
                "oversize": (200, {}, b"X" * (discovery.MAX_BODY + 1)),
                "json": (200, {}, b"PRIVATE"),
                "http": (503, {}, b"PRIVATE"),
            }[problem]
        return backend(method, url, payload)

    report = discovery.monitor(send=send)
    assert statuses(report)["health"] == expected
    assert report["status"] == "failed"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("method,url", [
    ("GET", "https://www.hybriqx.com.evil.example/health"),
    ("GET", "https://www.hybriqx.com:443/health"),
    ("GET", discovery.ORIGIN + "/health?token=secret"),
    ("GET", "http://www.hybriqx.com/health"),
    ("POST", discovery.ORIGIN + "/v1/wod/scale"),
    ("GET", discovery.ORIGIN + "/api/me"),
    ("GET", discovery.ORIGIN + "/health#fragment"),
    ("GET", "https://user@www.hybriqx.com/health"),
    ("POST", discovery.PYPI),
])
def test_url_guard(method, url, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Forbidden URL must never reach HTTP")
    monkeypatch.setattr(discovery.urllib.request, "build_opener", forbidden)
    with pytest.raises(discovery.CheckError):
        discovery.transport(method, url)


def test_transport_bounds_headers_and_no_payment(monkeypatch):
    captured = {}

    class Response:
        code = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == discovery.MAX_BODY + 1
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            captured.update(request=request, timeout=timeout)
            return Response()

    def build(handler):
        assert isinstance(handler, discovery.NoRedirect)
        return Opener()

    monkeypatch.setattr(discovery.urllib.request, "build_opener", build)
    assert discovery.transport("GET", discovery.ORIGIN + "/health")[0] == 200
    assert captured["timeout"] == 15
    headers = {k.lower(): v for k, v in captured["request"].header_items()}
    assert headers["x-client-name"] == discovery.LABEL
    assert not ({"authorization", "payment-signature", "x-payment", "cookie"} & headers.keys())
    with pytest.raises(discovery.CheckError, match="redirect_refused"):
        discovery.NoRedirect().redirect_request(None, None, 302, "PRIVATE", {},
                                                 "https://evil.example")


def test_cli_output_summary_and_exit_codes(monkeypatch, scratch_path, capsys):
    backend = Backend()
    monkeypatch.setattr(discovery, "transport", backend)
    output = scratch_path / "discovery.json"
    summary = scratch_path / "summary.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert discovery.main(["--output", str(output)]) == 0
    assert statuses(json.loads(output.read_text()))["pypi"] == "pending"
    assert "| pypi | pending |" in summary.read_text()
    assert discovery.main(["--require-pypi"]) == 1
    assert "PRIVATE" not in capsys.readouterr().out + summary.read_text()


def test_monitor_imports_are_standard_library_only():
    tree = ast.parse((ROOT / "scripts" / "check_discovery.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module]
        else:
            continue
        assert all(name.split(".")[0] in sys.stdlib_module_names for name in names)


@pytest.mark.parametrize("field,value", [
    ("name", "wrong"), ("license", "wrong"),
    ("scripts", {"wrong": "wrong:main"}), ("urls", {"Repository": "https://wrong.example"}),
])
def test_release_identity(field, value):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    release.validate(project, release.REPOSITORY, "v" + project["version"])
    project[field] = value
    with pytest.raises(ValueError):
        release.validate(project, release.REPOSITORY, "v" + project["version"])


@pytest.mark.parametrize("repo,tag", [
    ("other/hybriqx-workout-assistant", "v0.1.0"),
    (release.REPOSITORY, "0.1.0"), (release.REPOSITORY, "v0.2.0"),
])
def test_release_rejects_fork_and_wrong_tag(repo, tag):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    with pytest.raises(ValueError):
        release.validate(project, repo, tag)


@pytest.mark.parametrize("mutation", [None, "action", "draft", "tag", "ref", "event", "package"])
def test_release_event(monkeypatch, scratch_path, mutation):
    event = {"action": "published", "release": {"draft": False, "tag_name": "v0.1.0"}}
    if mutation == "action":
        event["action"] = "created"
    elif mutation == "draft":
        event["release"]["draft"] = True
    elif mutation == "tag":
        event["release"]["tag_name"] = "v0.2.0"
    path = scratch_path / "event.json"
    path.write_text(json.dumps(event))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push" if mutation == "event" else "release")
    monkeypatch.setenv("GITHUB_REPOSITORY", release.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main" if mutation == "ref" else "refs/tags/v0.1.0")
    monkeypatch.setattr(sys, "argv", ["check_release", "--tag", "v0.1.0"])
    monkeypatch.chdir(ROOT)
    if mutation == "package":
        original = release.tomllib.loads

        def wrong_package(text):
            data = original(text)
            data["tool"]["setuptools"]["packages"]["find"]["include"] = ["wrong*"]
            return data

        monkeypatch.setattr(release.tomllib, "loads", wrong_package)
    if mutation:
        with pytest.raises(ValueError):
            release.main()
    else:
        release.main()


def test_workflows_release_permissions_and_offline_validation():
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    build, publisher = publish.split("\n  publish:\n")
    assert "id-token:" not in build
    assert "id-token: write" in publisher and "environment: pypi" in publisher
    assert "actions/checkout" not in publisher and "pip install" not in publisher
    assert "secrets." not in publish and "password:" not in publish
    assert publish.count("github.repository == 'LKennedyShare/hybriqx-workout-assistant'") == 2
    assert "types: [published]" in publish and "needs: build" in publisher
    assert "python -m build" in build and "twine check --strict" in build
    assert 'cd "$GITHUB_WORKSPACE/../wheel-smoke"' in build
    tests = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
    for workflow in (tests, build):
        assert "--index-url https://pypi.org/simple" in workflow
        assert "--framework langchain" in workflow and "--framework openai" in workflow
        assert "python -m pytest -q" in workflow
    nightly = (ROOT / ".github" / "workflows" / "discovery.yml").read_text()
    assert "'53 3 * * *'" in nightly and "workflow_dispatch:" in nightly
    assert "require_pypi:" in nightly and "default: false" in nightly
    assert "pip install" not in nightly and "id-token:" not in nightly
    assert "if: always()" in nightly and "actions/upload-artifact" in nightly

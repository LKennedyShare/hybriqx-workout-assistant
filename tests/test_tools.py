import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
import json
from threading import Barrier, get_ident

import pytest

from hybriqx_workout_assistant import client
from hybriqx_workout_assistant.tools import (
    HybriqxWorkoutTools, normalized_body, request_digest,
)


def openai_context(name, arguments):
    from agents.tool_context import ToolContext

    return ToolContext(context=None, tool_name=name, tool_call_id="offline-test",
                       tool_arguments=arguments)


@pytest.fixture
def text(request_body):
    return json.dumps(request_body)


def test_defaults_no_paid_tool():
    factory = HybriqxWorkoutTools()
    assert [item[0] for item in factory._methods()] == ["offline_demo", "inspect_workout"]
    assert factory.paid_attempts == 0 and factory.reserved_usdc == 0
    with pytest.raises(client.BuyerError, match="disabled"):
        factory.adapt_workout_paid('{"original_wod":"x","available_equipment":[]}')


@pytest.mark.parametrize("answer", [1, "yes", [True], {"approved": True}, False, None])
def test_policy_literal_true_required(answer, text, monkeypatch):
    monkeypatch.setattr(client, "adapt_workout", lambda *args, **kwargs: pytest.fail("not approved"))
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: answer)
    with pytest.raises(client.BuyerError, match="denied"):
        factory.adapt_workout_paid(text)
    assert factory.paid_attempts == 0 and factory.reserved_usdc == 0


def test_validation_before_approval_and_digest_normalization(text, request_body):
    called = []
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: called.append(approval) or True)
    with pytest.raises(client.BuyerError, match="Invalid"):
        factory.adapt_workout_paid('{"original_wod":"private","available_equipment":[""]}')
    assert not called
    body = {**request_body, "original_wod": " " + request_body["original_wod"] + " "}
    assert request_digest(json.dumps(body)) == request_digest(text)
    body["athlete_notes"] = "different health data"
    assert request_digest(json.dumps(body)) != request_digest(text)


def test_frozen_policy_cannot_change_sent_body(text, request_body, monkeypatch):
    seen = []
    factory = None

    def policy(approval):
        assert factory.reserved_usdc == 0  # Callback does not run under the lock.
        assert approval.canonical_sha256 == request_digest(text)
        with pytest.raises(TypeError):
            approval.request["athlete_notes"] = "changed"
        with pytest.raises(TypeError):
            approval.request["available_equipment"][0] = "changed"
        with pytest.raises(FrozenInstanceError):
            approval.max_usdc = Decimal("1")
        with pytest.raises(FrozenInstanceError):
            approval.canonical_body = b"changed"
        return True

    def run(base_url, body, **kwargs):
        assert factory.reserved_usdc == Decimal("0.05")
        assert factory.paid_attempts == 1
        seen.append((body, kwargs))
        return {"success": True}

    monkeypatch.setattr(client, "adapt_workout", run)
    factory = HybriqxWorkoutTools(approval_policy=policy)
    assert factory.adapt_workout_paid(text) == {"success": True}
    assert seen[0][0] == normalized_body(text)[0]
    assert seen[0][0]["athlete_notes"] == request_body["athlete_notes"]
    assert seen[0][1]["pay"] is True
    with pytest.raises(client.BuyerError):
        factory.adapt_workout_paid(text)
    assert len(seen) == 1


def test_concurrent_budget_reserves_once(text, monkeypatch):
    barrier = Barrier(2)
    calls = []

    def policy(approval):
        barrier.wait(timeout=10)
        return True

    def run(*args, **kwargs):
        calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(client, "adapt_workout", run)
    factory = HybriqxWorkoutTools(approval_policy=policy, max_paid_attempts=50)

    def invoke():
        try:
            return factory.adapt_workout_paid(text)
        except client.BuyerError:
            return {"denied": True}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: invoke(), range(2)))
    assert sum(item == {"ok": True} for item in results) == 1
    assert calls == [1] and factory.paid_attempts == 1
    assert factory.reserved_usdc == Decimal("0.05")


def test_cumulative_budget_exact_under_low_precision(text, monkeypatch):
    monkeypatch.setattr(client, "adapt_workout", lambda *args, **kwargs: {"ok": True})
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: True,
                                 max_usdc="0.025000000000000000000000000001",
                                 max_paid_attempts=10)
    with localcontext() as context:
        context.prec = 2
        factory.adapt_workout_paid(text)
        with pytest.raises(client.BuyerError, match="limit"):
            factory.adapt_workout_paid(text)
    assert factory.paid_attempts == 1


@pytest.mark.parametrize("error", [
    client.BuyerError("PRIVATE-ERROR", {"authorized_usdc": "0.05", "payment_made": True}),
    RuntimeError("SECRET-PRIVATE-KEY"),
])
def test_failure_retains_full_session_reservation(text, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(client, "adapt_workout", fail)
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: True)
    with pytest.raises(client.BuyerError) as caught:
        factory.adapt_workout_paid(text)
    assert factory.reserved_usdc == Decimal("0.05") and factory.paid_attempts == 1
    assert caught.value.report["reserved_usdc"] == "0.05"
    assert "PRIVATE" not in str(caught.value)
    if isinstance(error, client.BuyerError):
        assert caught.value.report["payment_made"] is True
    with pytest.raises(client.BuyerError, match="limit"):
        factory.adapt_workout_paid(text)


def test_policy_reentrant_refused_without_deadlock(text, monkeypatch):
    monkeypatch.setattr(client, "adapt_workout", lambda *args, **kwargs: {"ok": True})
    factory = None

    def policy(approval):
        with pytest.raises(client.BuyerError, match="Reentrant"):
            factory.adapt_workout_paid(text)
        assert factory.paid_attempts == 0
        return True

    factory = HybriqxWorkoutTools(approval_policy=policy)
    assert factory.adapt_workout_paid(text) == {"ok": True}


def test_policy_exception_not_leaked(text):
    def policy(approval):
        raise RuntimeError("PRIVATE-NOTES")

    factory = HybriqxWorkoutTools(approval_policy=policy)
    with pytest.raises(client.BuyerError) as error:
        factory.adapt_workout_paid(text)
    assert "PRIVATE-NOTES" not in str(error.value)
    assert factory.reserved_usdc == 0


def test_async_policy_rejected(text):
    async def policy(approval):
        return True

    with pytest.raises(ValueError, match="synchronous"):
        HybriqxWorkoutTools(approval_policy=policy)
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: policy(approval))
    with pytest.raises(client.BuyerError, match="synchronously"):
        factory.adapt_workout_paid(text)
    assert factory.paid_attempts == 0


@pytest.mark.parametrize("kwargs", [
    {"max_usdc": True}, {"max_usdc": 0.05}, {"max_usdc": "NaN"}, {"max_usdc": "0.06"},
    {"max_paid_attempts": True}, {"max_paid_attempts": 0}, {"approval_policy": "approved"},
])
def test_bad_constructor(kwargs):
    with pytest.raises(ValueError):
        HybriqxWorkoutTools(**kwargs)


def test_inspection_forces_false_and_no_policy(text, monkeypatch):
    seen = []
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: pytest.fail("no policy for inspect"))

    def run(base_url, body, **kwargs):
        seen.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(client, "adapt_workout", run)
    assert factory.inspect_workout(text) == {"ok": True}
    assert seen == [{"pay": False, "max_usdc": Decimal("0.05")}]
    assert factory.paid_attempts == 0


def test_real_langchain_tools_offline_and_unknown_rejection(text, monkeypatch):
    pytest.importorskip("langchain_core")
    factory = HybriqxWorkoutTools()
    tools = {tool.name: tool for tool in factory.langchain_tools()}
    assert set(tools) == {"offline_demo", "inspect_workout"}
    assert tools["offline_demo"].invoke({})["mode"] == "SYNTHETIC_ILLUSTRATION"
    with pytest.raises(ValueError):
        tools["offline_demo"].invoke({"pay": True})
    with pytest.raises(ValueError):
        tools["inspect_workout"].invoke({"unsigned_request_json": text, "pay": True})
    with pytest.raises(ValueError):
        tools["inspect_workout"].invoke({"unsigned_request_json": {}})
    main_thread = get_ident()
    threads = []

    def run(*args, **kwargs):
        threads.append(get_ident())
        return {"ok": True}

    monkeypatch.setattr(client, "adapt_workout", run)
    result = asyncio.run(tools["inspect_workout"].ainvoke({"unsigned_request_json": text}))
    assert result == {"ok": True} and threads[0] != main_thread


def test_real_openai_tools_offline_and_unknown_rejection(text, monkeypatch):
    pytest.importorskip("agents")
    factory = HybriqxWorkoutTools()
    tools = {tool.name: tool for tool in factory.openai_tools()}
    assert set(tools) == {"offline_demo", "inspect_workout"}
    assert tools["inspect_workout"].strict_json_schema is True
    assert tools["inspect_workout"].params_json_schema["additionalProperties"] is False

    async def invoke():
        demo = await tools["offline_demo"].on_invoke_tool(openai_context("offline_demo", "{}"), "{}")
        assert demo["mode"] == "SYNTHETIC_ILLUSTRATION"
        for name, arguments in [
            ("offline_demo", '{"pay":true}'),
            ("inspect_workout", json.dumps({"unsigned_request_json": text, "approval": True})),
            ("inspect_workout", '{"unsigned_request_json":2}'),
        ]:
            with pytest.raises(ValueError):
                await tools[name].on_invoke_tool(openai_context(name, arguments), arguments)
        arguments = json.dumps({"unsigned_request_json": text})
        return await tools["inspect_workout"].on_invoke_tool(
            openai_context("inspect_workout", arguments), arguments,
        )

    main_thread = get_ident()
    threads = []

    def run(*args, **kwargs):
        threads.append(get_ident())
        return {"ok": True}

    monkeypatch.setattr(client, "adapt_workout", run)
    assert asyncio.run(invoke()) == {"ok": True}
    assert threads[0] != main_thread


@pytest.mark.parametrize("framework", ["langchain", "openai"])
def test_real_paid_tools_policy_and_error_propagation(framework, text, monkeypatch):
    pytest.importorskip("langchain_core" if framework == "langchain" else "agents")
    factory = HybriqxWorkoutTools(approval_policy=lambda approval: True)
    tools = factory.langchain_tools() if framework == "langchain" else factory.openai_tools()
    paid = next(tool for tool in tools if tool.name == "adapt_workout_paid")
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise client.BuyerError("STOP", {"authorized_usdc": "0.05"})

    monkeypatch.setattr(client, "adapt_workout", fail)

    async def invoke():
        if framework == "langchain":
            return await paid.ainvoke({"unsigned_request_json": text})
        arguments = json.dumps({"unsigned_request_json": text})
        return await paid.on_invoke_tool(openai_context(paid.name, arguments), arguments)

    with pytest.raises(client.BuyerError, match="reservation retained"):
        asyncio.run(invoke())
    assert len(calls) == 1 and calls[0]["pay"] is True
    assert factory.reserved_usdc == Decimal("0.05")

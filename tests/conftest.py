from copy import deepcopy
import inspect
import socket

import httpx
import pytest

from hybriqx_workout_assistant.fixtures import SYNTHETIC_REQUEST


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    original_send = httpx.Client.send
    original_connect = socket.socket.connect

    def send(client, *args, **kwargs):
        if not isinstance(client._transport, httpx.MockTransport):
            pytest.fail("Tests permit httpx.MockTransport only")
        return original_send(client, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Real sockets are forbidden")

    def connect(sock, address):
        # Windows asyncio creates its internal wakeup socketpair via loopback.
        caller = inspect.currentframe().f_back
        fallback = getattr(socket, "_fallback_socketpair", None)
        if fallback is not None and caller.f_code is fallback.__code__:
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def request_body():
    body = deepcopy(SYNTHETIC_REQUEST)
    body["athlete_notes"] = "PRIVATE-HEALTH-NOTES"
    return body

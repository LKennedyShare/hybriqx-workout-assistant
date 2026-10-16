"""Optional real framework tools with operator-owned approval and session limits."""

import asyncio
from dataclasses import dataclass
from decimal import Decimal, localcontext
from hashlib import sha256
import inspect
import json
import httpx
from threading import Lock, local
from types import MappingProxyType
from typing import Callable, Mapping

from pydantic import BaseModel, ConfigDict, StrictStr

from . import client


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ApprovalRequest:
    """Normalized immutable body and digest, including any private athlete notes."""

    request: Mapping
    canonical_body: bytes
    canonical_sha256: str
    max_usdc: Decimal


def normalized_body(unsigned_request_json: str) -> tuple[dict, bytes]:
    try:
        if type(unsigned_request_json) is not str:
            raise ValueError
        body = client.normalize_request(json.loads(unsigned_request_json))
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8")
        return body, canonical
    except (ValueError, TypeError, OverflowError):
        raise client.BuyerError("Invalid workout request JSON; no network used", {}) from None


def request_digest(unsigned_request_json: str) -> str:
    """Compute an approval digest locally, only from a trusted operator context."""
    return sha256(normalized_body(unsigned_request_json)[1]).hexdigest()


class _NoArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _RequestArguments(_NoArguments):
    unsigned_request_json: StrictStr


class HybriqxWorkoutTools:
    """Reuse one instance per session; this is not a durable wallet-wide budget."""

    def __init__(
        self,
        *,
        base_url: str = client.DEFAULT_BASE_URL,
        approval_policy: Callable[[ApprovalRequest], bool] | None = None,
        max_usdc: str | Decimal = "0.05",
        max_paid_attempts: int = 1,
    ):
        client.request_url(base_url)
        if approval_policy is not None and (
            not callable(approval_policy)
            or inspect.iscoroutinefunction(approval_policy)
            or inspect.iscoroutinefunction(getattr(approval_policy, "__call__", None))
        ):
            raise ValueError("approval_policy must be a trusted synchronous callback")
        if type(max_paid_attempts) is not int or max_paid_attempts < 1:
            raise ValueError("max_paid_attempts must be a positive integer")
        self._base_url = base_url
        self._approval_policy = approval_policy
        self._cap = client.spend_cap(max_usdc)
        self._max_paid_attempts = max_paid_attempts
        self._lock = Lock()
        self._local = local()
        self._reserved = Decimal("0")
        self._attempts = 0

    @property
    def reserved_usdc(self):
        with self._lock:
            return self._reserved

    @property
    def paid_attempts(self):
        """Reserved attempts, not successful payments."""
        with self._lock:
            return self._attempts

    def offline_demo(self) -> dict:
        """Return a synthetic offline fixture without model, wallet or network access."""
        return client.offline_demo()

    def inspect_workout(self, unsigned_request_json: str) -> dict:
        """Validate locally and inspect empty quotes only; never upload workout or notes."""
        body, _ = normalized_body(unsigned_request_json)
        return client.adapt_workout(self._base_url, body, pay=False, max_usdc=self._cap)

    def adapt_workout_paid(self, unsigned_request_json: str) -> dict:
        """Request one advisory adaptation only if independent operator policy approves."""
        body, canonical = normalized_body(unsigned_request_json)
        if self._approval_policy is None:
            raise client.BuyerError("Paid tool is disabled", {})
        if getattr(self._local, "active", False):
            raise client.BuyerError("Reentrant paid invocation refused", {})
        self._local.active = True
        try:
            with self._lock:
                if self._attempts >= self._max_paid_attempts or self._reserved >= client.MAX_USDC:
                    raise client.BuyerError("Session paid-attempt or cumulative 0.05 USDC limit exhausted", {
                        "reserved_usdc": str(self._reserved), "paid_attempts": self._attempts,
                    })
            approval = ApprovalRequest(
                _freeze(body), canonical, sha256(canonical).hexdigest(), self._cap,
            )
            # No lock while calling external policy: it can safely inspect counters.
            try:
                approved = self._approval_policy(approval)
            except (RuntimeError, ValueError, TypeError):
                raise client.BuyerError("Operator policy failed; no authorization attempted", {}) from None
            if inspect.isawaitable(approved):
                if inspect.iscoroutine(approved):
                    approved.close()
                raise client.BuyerError("Operator policy must return synchronously", {})
            if approved is not True:
                raise client.BuyerError("Operator approval denied", {})
            with self._lock:
                with localcontext() as context:
                    context.prec = max(28, len(self._cap.as_tuple().digits)
                                       + len(str(self._attempts + 1)) + 2)
                    reserved = self._cap * (self._attempts + 1)
                if self._attempts >= self._max_paid_attempts or reserved > client.MAX_USDC:
                    raise client.BuyerError("Session paid-attempt or cumulative 0.05 USDC limit exhausted", {
                        "reserved_usdc": str(self._reserved), "paid_attempts": self._attempts,
                    })
                self._reserved = reserved
                self._attempts += 1
            try:
                return client.adapt_workout(self._base_url, body, pay=True, max_usdc=self._cap)
            except client.BuyerError as exc:
                report = {**exc.report, "reserved_usdc": str(self.reserved_usdc),
                          "paid_attempts": self.paid_attempts}
                raise client.BuyerError("Paid attempt failed; reservation retained; STOP", report) from None
            except (RuntimeError, ValueError, TypeError, httpx.HTTPError):
                raise client.BuyerError("Paid attempt failed; reservation retained; STOP", {
                    "reserved_usdc": str(self.reserved_usdc), "paid_attempts": self.paid_attempts,
                    "settlement_status": "unconfirmed",
                }) from None
        finally:
            self._local.active = False

    def _methods(self):
        methods = [
            ("offline_demo", self.offline_demo, _NoArguments),
            ("inspect_workout", self.inspect_workout, _RequestArguments),
        ]
        if self._approval_policy is not None:
            methods.append(("adapt_workout_paid", self.adapt_workout_paid, _RequestArguments))
        return methods

    def langchain_tools(self) -> list:
        """Build real StructuredTools without creating a model or configuring tracing."""
        from langchain_core.tools import StructuredTool

        class StrictStructuredTool(StructuredTool):
            def _to_args_and_kwargs(self, tool_input, tool_call_id):
                # LangChain skips validation for empty schemas; enforce it here too.
                self.args_schema.model_validate(tool_input, strict=True)
                return super()._to_args_and_kwargs(tool_input, tool_call_id)

        result = []
        for name, method, schema in self._methods():
            async def offload(_method=method, **kwargs):
                return await asyncio.to_thread(_method, **kwargs)

            result.append(StrictStructuredTool.from_function(
                func=method, coroutine=offload, name=name, description=method.__doc__,
                args_schema=schema,
            ))
        return result

    def openai_tools(self) -> list:
        """Build real Agents FunctionTools without starting Runner or a model."""
        from agents import function_tool

        def wrap(name, method, schema):
            if schema is _NoArguments:
                async def invoke() -> dict:
                    return await asyncio.to_thread(method)
            else:
                async def invoke(unsigned_request_json: str) -> dict:
                    return await asyncio.to_thread(method, unsigned_request_json)
            tool = function_tool(
                invoke, name_override=name, description_override=method.__doc__,
                failure_error_function=None, strict_mode=True,
            )
            original = tool.on_invoke_tool

            async def strict_invoke(context, arguments):
                schema.model_validate_json(arguments, strict=True)
                return await original(context, arguments)

            tool.on_invoke_tool = strict_invoke
            return tool

        return [wrap(name, method, schema) for name, method, schema in self._methods()]

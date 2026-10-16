HYBRIQX Workout Assistant
=========================

A standalone, client-only Python buyer for HYBRIQX advisory workout adaptation.
No server, exercise execution, model runner, coaching-app subscription or bundled
backend engine. This is not medical advice or a guarantee of exercise safety,
efficacy or latency. Consult an appropriately qualified professional when needed.

Install (Python 3.12+)
------------------------

Install the published MIT package from PyPI::

    python -m pip install hybriqx-workout-assistant
    python -m pip install "hybriqx-workout-assistant[langchain,openai]"

Or install from the public repository::

    python -m pip install "git+https://github.com/LKennedyShare/hybriqx-workout-assistant.git"

From a checkout, with optional actual framework integrations and tests::

    python -m venv .venv
    .venv\Scripts\python -m pip install -e ".[test,langchain,openai,release]"
    .venv\Scripts\python -m pytest -q

The base package does not import either framework. Optional extras are
``langchain`` (langchain-core >=1.6,<2) and ``openai`` (openai-agents >=0.24,<1).
They provide real tools, not prompt templates or a hosted model.
If your package mirror has not indexed openai-agents 0.24 yet, install its
official upstream release tag first (never disable TLS verification)::

    python -m pip install "https://github.com/openai/openai-agents-python/archive/refs/tags/v0.24.0.zip"
    python -m pip install -e ".[test,langchain,openai,release]"

Offline first
-------------

::

    hybriqx-workout-assistant --demo
    python -m hybriqx_workout_assistant --demo
    python examples\standalone.py
    python examples\framework_demo.py --framework langchain
    python examples\framework_demo.py --framework openai

All of these are offline, use the labelled ``SYNTHETIC_ILLUSTRATION`` fixture,
and need no wallet or model API key. The fixture's rowing machine matches its
rowing example; it is not personalized advice. ``execution_time_ms=0`` is a
placeholder, not measured performance. Framework demos invoke tools locally:
no AgentRunner, Agents Runner, model requests or tracing setup.

Inspection and explicit payment
-------------------------------

Prepare a private local JSON object using the public request schema::

    {
      "original_wod": "SYNTHETIC_ILLUSTRATION: 3 rounds of 200 m rowing and 10 air squats.",
      "available_equipment": ["rowing machine"],
      "target_time_domain_mins": 15,
      "athlete_notes": null
    }

``original_wod`` is a nonblank string of 1..5000 characters.
``available_equipment`` is required, up to 50 nonblank strings; an empty list is
allowed. Strings are trimmed. ``target_time_domain_mins`` is an integer 1..240
or null, default 15. ``athlete_notes`` is a string of at most 1000 characters
or null, default null. Unknown fields and coercions are rejected.

Inspect payment terms without uploading the workout::

    hybriqx-workout-assistant --request examples\synthetic_request.json

The request is validated locally first. Inspection POSTs only ``{}``, even when
a wallet is configured. It never signs, uploads your workout/notes, or pays.
An ungated endpoint returning success to this empty probe is treated as an
error, not as a workout result.

Only ``--pay`` permits one paid request with the validated normalized body::

    # PowerShell: obtain the value privately; do not paste a key into shared logs.
    $secure = Read-Host "Dedicated x402 wallet private key" -AsSecureString
    $env:X402_CLIENT_PRIVATE_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
    try {
        hybriqx-workout-assistant --request examples\synthetic_request.json --pay --max-usdc 0.05
    } finally {
        Remove-Item Env:X402_CLIENT_PRIVATE_KEY -ErrorAction SilentlyContinue
        Remove-Variable secure -ErrorAction SilentlyContinue
    }

Use a dedicated low-balance wallet funded appropriately for the quoted network.
Never commit its key. The client reads only ``X402_CLIENT_PRIVATE_KEY`` and only
after explicit payment opt-in and quote validation. The x402 fee buys a single
advisory adaptation; it is not a HYBRIQX app subscription and executes no exercise.
Like the verification fee in an invoice or car-import buyer, a service fee does
not execute the underlying real-world activity.

Service contract and payment guardrails
-----------------------------------------

Canonical origin: ``https://www.hybriqx.com``.
The client uses ``POST /api/v1/scale-wod``; the service alias is ``/v1/wod/scale``.
The service's MCP endpoint is ``https://www.hybriqx.com/mcp/``; this package uses
REST, not MCP. ``--base-url`` can override only the origin. HTTPS is mandatory
except for localhost/loopback HTTP. Credentials, queries, fragments, unsafe ports
and every redirect (even same-origin) are refused. Apex and www are different
origins: a quote advertising www against an apex request is rejected safely;
use the canonical www origin rather than relying on a redirect.

The official x402 SDK decodes the v2 PAYMENT-REQUIRED header, including when the
402 body is ``{}``. Exactly one ``exact`` quote is required, priced at precisely
50000 atomic USDC (0.05). Only these networks/tokens are accepted:

* Base, ``eip155:8453``: ``0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913``
* Base Sepolia, ``eip155:84532``: ``0x036CbD53842c5426634e7929541eC2318f3dCF7e``

The resource must exactly match the requested endpoint, payTo must be a nonzero
address, and authorization timeout must be 1..300 seconds. The cap defaults to
0.05 and must be a positive finite exact string/Decimal <=0.05; Python bools and
floats are rejected. The quote is checked against it before wallet loading or
signing, using integer-ratio arithmetic, not floating-point rounding.
The SDK registers only the validated network, never a wildcard.

Each invocation makes one initial empty quote and at most one paid POST. No
fresh-quote loop, hidden retries or automatic payment after 400/422/429/502/504,
redirects or a repeated 402. Numeric Retry-After is retained in error reports.
The authorization counter is reserved before signing and is not cleared on
failure. A successful SDK settlement response must match network, any reported
amount and a nonzero transaction hash. The SDK permits an omitted amount; in
that case reported spending uses the authorized quote. Missing/malformed/mismatched PAYMENT-RESPONSE means
unconfirmed payment: STOP and reconcile manually; do not automatically pay again.
``confirmed_by_sdk_response`` means what the SDK response reports, not independent
chain verification. Settlement state is retained if result validation or HTTP
processing subsequently fails.

The result has ``scaled_wod``, ``movement_substitutions`` (list of strings),
``target_stimulus`` and finite nonnegative ``execution_time_ms``. No medical
safety validation or live performance claim is made.

Python API and privacy
----------------------

::

    import json
    from hybriqx_workout_assistant import adapt_workout, BuyerError

    with open("private_request.json", encoding="utf-8") as handle:
        request = json.load(handle)
    try:
        report = adapt_workout("https://www.hybriqx.com", request)
        # Inspection only. Explicit operator opt-in:
        # report = adapt_workout("https://www.hybriqx.com", request,
        #                        pay=True, max_usdc="0.05")
    except BuyerError as error:
        report = error.report  # Safe financial state; no raw request/SDK error.

``run_workout`` is an alias with the same signature. Both accept a test-only
``transport=httpx.MockTransport(...)`` parameter. ``pay`` must be a literal bool.
Invalid input makes no HTTP requests. Success CLI output is exactly one JSON
object on stdout; errors are one JSON object on stderr with nonzero status.
``--help`` provides ordinary CLI help. Error reports omit raw workouts, notes,
wallets and server/SDK error bodies. Successful result JSON intentionally contains
private workout content for the same caller: do not publish it, send it to public
telemetry, or capture it in shared logs. Inspection reports do not echo input.

Notes may contain health data. Obtain informed consent before sharing workout
or notes with HYBRIQX. If a model is involved, separately assess and consent to
the chosen model provider's data handling, tool inputs/outputs, logs and tracing.
This package cannot enforce a host application's telemetry/tracing policy.

Agent tools: independent approval, never model consent
--------------------------------------------------------

``HybriqxWorkoutTools`` exposes ``offline_demo`` and ``inspect_workout`` by
default. A paid ``adapt_workout_paid`` tool exists only when a trusted constructor
``approval_policy`` is supplied. Model arguments are only
``unsigned_request_json: str``; no model-supplied approval, origin, budget,
wallet or pay flag is accepted. Runtime schemas reject extra fields.

::

    import json
    from hybriqx_workout_assistant.tools import HybriqxWorkoutTools, request_digest

    # Independently reviewed exact request, loaded in trusted operator context.
    with open("operator_reviewed_request.json", encoding="utf-8") as handle:
        reviewed_json = json.dumps(json.load(handle))
    allowlist = frozenset({request_digest(reviewed_json)})

    def operator_policy(approval):
        return approval.canonical_sha256 in allowlist

    factory = HybriqxWorkoutTools(approval_policy=operator_policy, max_usdc="0.05")
    langchain_tools = factory.langchain_tools()  # optional extra
    # Or: agents_tools = factory.openai_tools()

Never derive the allowlist from an unreviewed model request, model claims of
consent, or a user statement quoted by a model. This example authorizes only the
exact normalized request, including notes. The callback receives a frozen
``ApprovalRequest``: deep-frozen ``request``, immutable ``canonical_body`` bytes,
``canonical_sha256`` and ``max_usdc``. Only literal ``True`` approves, not truthy
values. Validation occurs before policy invocation; mutation cannot change the
body sent. Policy executes outside the budget lock; reentrant paid invocation is
refused. Approval is not a claim that the workout is medically safe.

Reuse the factory across calls. It atomically reserves the full cap before the
paid workflow, allows one paid attempt by default, and enforces a cumulative
in-memory session limit of 0.05 USDC even under concurrent calls. Reservations
are never refunded on failures or unknown outcomes. Counters represent attempts,
not settled transactions. Recreating the factory, restarting the process or
using multiple processes bypasses an in-memory limit: production requires an
external durable budget/approval ledger. There is no automatic retry.

LangChain uses real StructuredTool objects with strict Pydantic schemas and
async thread offload. OpenAI Agents uses real strict function_tool objects,
``failure_error_function=None``, runtime unknown-argument rejection, and
``asyncio.to_thread``. No frameworks are imported until requested.

Development
-----------

Tests use only mock transport; actual framework tools are invoked locally.
No service, model, wallet signing, chain settlement or exercise efficacy is
tested live::

    python -m pytest -q
    python -m build
    python -m twine check dist\*

MIT licensed, copyright 2026 LKennedyShare.

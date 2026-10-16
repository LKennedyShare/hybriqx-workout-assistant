"""Operator-side digest allowlist example; run locally, never calls the service."""

import json

from hybriqx_workout_assistant.fixtures import SYNTHETIC_REQUEST
from hybriqx_workout_assistant.tools import HybriqxWorkoutTools, request_digest

# In production load an independently reviewed request from a trusted operator store,
# after informed consent, not from the model or its conversational claims.
operator_reviewed_json = json.dumps(SYNTHETIC_REQUEST)
approved_digests = frozenset({request_digest(operator_reviewed_json)})


def policy(approval):
    return approval.canonical_sha256 in approved_digests


tools = HybriqxWorkoutTools(approval_policy=policy)
if __name__ == "__main__":
    # Demonstrate configuration without invoking the paid method.
    print(json.dumps(tools.offline_demo()))

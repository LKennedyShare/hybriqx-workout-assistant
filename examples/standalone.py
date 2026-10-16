"""Offline Python example; deliberately never opts in to payment."""

import json

from hybriqx_workout_assistant import offline_demo

print(json.dumps(offline_demo()))

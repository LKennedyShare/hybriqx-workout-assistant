"""Validate immutable release identity before building or granting OIDC access."""

import argparse
import json
import os
from pathlib import Path
import tomllib

REPOSITORY = "LKennedyShare/hybriqx-workout-assistant"
PACKAGE = "hybriqx_workout_assistant"
CLI = "hybriqx-workout-assistant"
ENTRYPOINT = PACKAGE + ".client:main"


def validate(project, repository, tag):
    if repository != REPOSITORY:
        raise ValueError("Unexpected repository")
    if project["name"] != CLI:
        raise ValueError("Unexpected distribution")
    if project["scripts"] != {CLI: ENTRYPOINT}:
        raise ValueError("Unexpected CLI identity")
    if project["urls"]["Repository"] != "https://github.com/" + REPOSITORY:
        raise ValueError("Unexpected repository metadata")
    if project["license"] != "MIT":
        raise ValueError("Unexpected license")
    if tag != "v" + project["version"]:
        raise ValueError("Release tag must exactly match v<project.version>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_EVENT_NAME") != "release":
        raise ValueError("Only release.published is allowed")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    release = event["release"]
    if (event["action"] != "published" or release["draft"]
            or release["tag_name"] != args.tag
            or os.environ.get("GITHUB_REF") != "refs/tags/" + args.tag):
        raise ValueError("Invalid published release event")
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    validate(metadata["project"], os.environ["GITHUB_REPOSITORY"], args.tag)
    if metadata["tool"]["setuptools"]["packages"]["find"]["include"] != [PACKAGE + "*"]:
        raise ValueError("Unexpected package identity")
    print("Release identity validated")


if __name__ == "__main__":
    main()

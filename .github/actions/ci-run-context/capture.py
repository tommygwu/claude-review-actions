"""Write a validated allowlisted event artifact from the caller environment."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from run_context import extract


def main():
    if len(sys.argv) != 2:
        raise ValueError("run-context-output-argument-required")
    try:
        with Path(os.environ["GITHUB_EVENT_PATH"]).open() as stream:
            event = json.load(stream)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("run-context-event-unreadable") from None
    result = extract(event, os.environ, datetime.now(timezone.utc).isoformat())
    output = Path(sys.argv[1])
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, sort_keys=True))
    except OSError:
        raise ValueError("run-context-output-unwritable") from None


if __name__ == "__main__":
    try:
        main()
    except KeyError:
        sys.exit("run-context-environment-missing")
    except (ValueError, TypeError, RecursionError) as error:
        message = str(error)
        sys.exit(message if message.startswith("run-context-") else "run-context-input-invalid")

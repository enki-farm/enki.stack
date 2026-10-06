#!/usr/bin/env python3
"""Send one native Laya request to a running KServe custom predictor."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_URL = "https://ai.zer0.garden/laya"
DEFAULT_PAYLOAD = {
    "state": {"text": "I was charged twice for the same invoice."},
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "Which department should handle this request?",
            "criteria": {
                "billing": "invoices, payments, refunds",
                "technical": "bugs, outages, system errors",
                "other": "everything else",
            },
        },
        "refund_requested": {
            "type": "noul",
            "instructions": "Does the user explicitly request a refund?",
        },
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Predictor URL")
    parser.add_argument("--payload", help="JSON file containing a native Laya request")
    parser.add_argument("--timeout", type=float, default=120, help="HTTP timeout in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = DEFAULT_PAYLOAD
    if args.payload:
        with open(args.payload, encoding="utf-8") as payload_file:
            payload = json.load(payload_file)

    request = Request(
        args.url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=args.timeout) as response:
            body = response.read().decode("utf-8")
            print(json.dumps(json.loads(body), indent=2))
    except HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1
    except URLError as error:
        print(f"request failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Call Orbit's authenticated, sandbox-only behavior demo seeder."""

import argparse
import json
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Orbit base URL, for example https://staging.orbitguest.com")
    parser.add_argument("--api-key", required=True, help="Sandbox restaurant Orbit API key")
    args = parser.parse_args()
    request = urllib.request.Request(args.url.rstrip("/") + "/v1/demo/behavior/seed", b"{}", {"Authorization": f"Bearer {args.api_key}", "Content-Type": "application/json", "User-Agent": "OrbitGuest-DemoSeeder/1.0"}, method="POST")
    with urllib.request.urlopen(request, timeout=180) as response:
        print(json.dumps(json.load(response), indent=2))


if __name__ == "__main__":
    main()

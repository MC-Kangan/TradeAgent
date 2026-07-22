"""Render a Compose override only for a validated private/VPN bind literal."""

from __future__ import annotations

import argparse

from trade_research.http import validate_bind_host


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    try:
        address = validate_bind_host(arguments.address)
    except ValueError as error:
        parser.error(str(error))
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    compose_address = f"[{address}]" if ":" in address else address
    print(
        "services:\n"
        "  research-api:\n"
        "    ports:\n"
        f'      - "{compose_address}:{arguments.port}:8000"'
    )


if __name__ == "__main__":
    main()

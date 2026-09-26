"""Run one tenant of the mock bank: `python -m mock_bank --tenant pinecrest`."""

from __future__ import annotations

import argparse

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .tenants import TENANTS

DEFAULT_PORTS = {"pinecrest": 8401, "lakeside": 8402}


def main() -> None:
    parser = argparse.ArgumentParser(description="AcmeCore mock core-banking app (synthetic data)")
    parser.add_argument("--tenant", required=True, choices=sorted(TENANTS))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    load_dotenv(".env")
    port = args.port or DEFAULT_PORTS[args.tenant]
    uvicorn.run(create_app(args.tenant), host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()

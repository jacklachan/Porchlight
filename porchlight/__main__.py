"""`python -m porchlight` / `porchlight`: run the server."""

from __future__ import annotations

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(prog="porchlight", description="Care check-ins from the front door.")
    parser.add_argument("--demo", action="store_true", help="use the bundled local stand-in instead of Ring")
    parser.add_argument("--host", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default 8000)")
    parser.add_argument("--env-file", default=".env", help="path to a .env file (default .env)")
    args = parser.parse_args()

    from .config import Settings, load_dotenv

    load_dotenv(args.env_file)
    if args.demo:
        os.environ["PORCHLIGHT_DEMO"] = "1"
    if args.host:
        os.environ["PORCHLIGHT_HOST"] = args.host
    if args.port:
        os.environ["PORCHLIGHT_PORT"] = str(args.port)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings.from_env()

    import uvicorn

    from .api import create_app

    mode = "local stand-in (not Ring)" if "/sim" in settings.ring_api_base else settings.ring_api_base
    print(f"Porchlight  http://{settings.host}:{settings.port}   Ring: {mode}   vision: {settings.vision_provider}")
    print(f"MCP server  http://{settings.host}:{settings.port}/mcp   Alexa+ simulation: /alexa")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()

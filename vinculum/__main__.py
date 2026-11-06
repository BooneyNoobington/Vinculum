import argparse
import hashlib
import re
import secrets
from pathlib import Path

import uvicorn

from .app import create_app
from .config import ConfigError


def main() -> None:
    parser = argparse.ArgumentParser(prog="vinculum")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("token", help="Generate a bearer token and its SHA-256 digest")
    serve = commands.add_parser("serve", help="Start the API")
    serve.add_argument("--config", type=Path, default=Path("config.json"))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--root-path", default="", help="Public URL prefix stripped by the proxy")
    args = parser.parse_args()

    if args.command == "token":
        token = secrets.token_urlsafe(32)
        print(f"Bearer token  {token}")
        print(f"SHA-256       {hashlib.sha256(token.encode('ascii')).hexdigest()}")
        return

    if args.root_path and not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", args.root_path):
        parser.error("--root-path must be a prefix such as /vinculum")
    try:
        app = create_app(args.config)
    except ConfigError as exc:
        parser.exit(1, f"vinculum: {exc}\n")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        root_path=args.root_path,
        workers=1,
        forwarded_allow_ips="127.0.0.1,::1",
        access_log=False,
    )


if __name__ == "__main__":
    main()

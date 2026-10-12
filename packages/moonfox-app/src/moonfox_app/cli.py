"""Explicit optional server entry point. No imports/startup of web frameworks for headless users."""

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Moonfox provider-managed service and WebUI")
    parser.add_argument(
        "--home", type=Path, required=True, help="Explicit private application data directory"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--origin", action="append", default=[], help="Additional exact browser Origin"
    )
    parser.add_argument("--static-dir", type=Path, help="Built packages/web-ui/dist (optional)")
    parser.add_argument("--config", type=Path, help="Initial non-secret JSON settings")
    parser.add_argument(
        "--demo", action="store_true", help="Enable explicitly offline demonstration provider"
    )
    args = parser.parse_args()
    token = os.environ.get("MOONFOX_ADMIN_TOKEN", "")
    if len(token) < 24:
        parser.error("Set MOONFOX_ADMIN_TOKEN to a random token of at least 24 characters")
    try:
        import uvicorn

        from moonfox_ai.api import Model
        from moonfox_app.bootstrap import create_application
        from moonfox_app.config.loader import load_settings
        from moonfox_app.security import Principal, TokenAuth
        from moonfox_app.server.app import create_server
        from moonfox_app.services.demo import demo_stream
        from moonfox_app.services.types import ModelRegistration
    except ImportError:
        parser.error("Server dependencies missing: install moonfox-app[server]")
    origins = [f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}", *args.origin]
    hub = create_application(
        home=args.home,
        models={
            "demo": ModelRegistration(
                Model("demo", "demo", "faux", max_tokens=2048), demo_stream, "离线演示 · 非真实模型"
            )
        }
        if args.demo
        else {},
        defaults=load_settings(args.config) if args.config else None,
    )
    app = create_server(
        hub,
        TokenAuth({token: Principal("owner", True)}),
        allowed_origins=origins,
        static_dir=args.static_dir,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

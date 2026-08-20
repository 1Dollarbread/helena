"""`helena-server` entry point — runs the FastAPI app under uvicorn."""

from __future__ import annotations

import argparse

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="helena-server",
        description="Run the H.E.L.E.N.A model server (FastAPI over Ollama).",
    )
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code changes.")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    uvicorn.run(
        "helena_server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()

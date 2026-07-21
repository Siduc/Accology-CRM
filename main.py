"""
Compatibility entry point.

Prefer:  python run.py
Render:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from app.env_bootstrap import bootstrap_environment  # noqa: E402

bootstrap_environment()

import uvicorn  # noqa: E402

from app.config import HOST, IS_PRODUCTION, PORT  # noqa: E402
from app.main import app  # noqa: F401, E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=int(os.environ.get("PORT", PORT)),
        reload=not IS_PRODUCTION,
    )

"""Start the Accountant CRM server (local or PaaS)."""

import os
from pathlib import Path

# Absolute-path dotenv BEFORE any app.config / database imports
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

# Package bootstrap (logging + .env again, idempotent)
from app.env_bootstrap import bootstrap_environment  # noqa: E402

bootstrap_environment()

import uvicorn  # noqa: E402

from app.config import HOST, IS_PRODUCTION, PORT  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=int(os.environ.get("PORT", PORT)),
        reload=not IS_PRODUCTION,
    )

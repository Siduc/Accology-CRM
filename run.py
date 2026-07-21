"""Start the Accountant CRM server (local or PaaS)."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Absolute project .env before any app imports (cwd-independent)
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

import uvicorn

from app.config import HOST, IS_PRODUCTION, PORT

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=int(os.environ.get("PORT", PORT)),
        reload=not IS_PRODUCTION,
    )

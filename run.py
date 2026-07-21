"""Start the Accountant CRM server (local or PaaS)."""

import os

import uvicorn

from app.config import HOST, PORT, IS_PRODUCTION

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=int(os.environ.get("PORT", PORT)),
        reload=not IS_PRODUCTION,
    )

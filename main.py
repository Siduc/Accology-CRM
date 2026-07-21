"""
Compatibility entry point.

Prefer:  python run.py
Render:  uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""

import os

import uvicorn

from app.main import app  # noqa: F401
from app.config import HOST, PORT, IS_PRODUCTION

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=int(os.environ.get("PORT", PORT)),
        reload=not IS_PRODUCTION,
    )

"""Create Current / year folders and AGENTS.md for every live CRM client.

Uses the local OneDrive tree (PRACTICE_FILES_ROOT). Lost clients are skipped.

  python scripts/ensure_client_packs.py
  python scripts/ensure_client_packs.py --no-move
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.services.client_playbook import ensure_live_client_packs, practice_files_root


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-move",
        action="store_true",
        help="Create folders and AGENTS.md only; do not relocate year-tagged papers.",
    )
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        print(f"Root: {practice_files_root()}", flush=True)
        res = ensure_live_client_packs(db, move_prior_years=not args.no_move)
    finally:
        db.close()
    print(
        f"Clients {res['clients']}  ok {res['ok']}  failed {res['failed']}  "
        f"papers filed {res['moved']}",
        flush=True,
    )
    for err in res.get("errors") or []:
        print(f"  {err}", flush=True)
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

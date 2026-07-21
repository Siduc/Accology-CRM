"""Restore database rows from a backup.json upload."""

import json

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.restore import parse_backup_json, restore_from_backup
from app.templating import render

router = APIRouter(tags=["restore"])


@router.get("/restore", response_class=HTMLResponse)
async def restore_page(request: Request):
    return render(request, "restore.html", {"result": None, "error": None})


@router.post("/restore", response_class=HTMLResponse)
async def restore_post(
    request: Request,
    backup_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if not backup_file.filename:
        return render(
            request,
            "restore.html",
            {"result": None, "error": "Please choose a backup.json file."},
            status_code=400,
        )

    name = (backup_file.filename or "").lower()
    if not name.endswith(".json"):
        return render(
            request,
            "restore.html",
            {
                "result": None,
                "error": "File must be a .json backup (e.g. backup.json).",
            },
            status_code=400,
        )

    try:
        raw = await backup_file.read()
        if not raw:
            raise ValueError("Uploaded file is empty.")
        data = parse_backup_json(raw)
        result = restore_from_backup(db, data)
    except json.JSONDecodeError as exc:
        db.rollback()
        return render(
            request,
            "restore.html",
            {"result": None, "error": f"Invalid JSON: {exc}"},
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return render(
            request,
            "restore.html",
            {"result": None, "error": str(exc)},
            status_code=400,
        )

    return render(
        request,
        "restore.html",
        {"result": result.as_dict(), "error": None},
    )

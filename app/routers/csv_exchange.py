"""CSV export / reimport for key Accologise lists."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.csv_exchange import DATASETS
from app.services.import_csv import excel_bytes_to_csv_text
from app.templating import render

router = APIRouter(tags=["csv-exchange"])


async def _read_text(csv_file: UploadFile | None, csv_data: str) -> str:
    if csv_file and csv_file.filename:
        content = await csv_file.read()
        name = (csv_file.filename or "").lower()
        if name.endswith((".xlsx", ".xlsm")):
            return excel_bytes_to_csv_text(content)
        if name.endswith(".xls"):
            raise ValueError(
                "Old .xls not supported — save as .xlsx or CSV UTF-8."
            )
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
    return csv_data or ""


@router.get("/export/{dataset}")
async def export_dataset(
    dataset: str,
    request: Request,
    db: Session = Depends(get_db),
):
    meta = DATASETS.get(dataset)
    if not meta:
        return RedirectResponse("/import/csv", status_code=303)
    params = dict(request.query_params)
    try:
        body = meta["export"](db, **params)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/import/csv?error={quote(str(exc)[:200])}", status_code=303
        )
    filename = meta.get("filename") or f"{dataset}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/import/csv", response_class=HTMLResponse)
async def csv_exchange_hub(
    request: Request,
    dataset: str = Query(""),
    msg: str = Query(""),
    error: str = Query(""),
):
    return render(
        request,
        "imports/csv_exchange.html",
        {
            "datasets": DATASETS,
            "dataset": dataset if dataset in DATASETS else "clients",
            "msg": msg,
            "error": error,
            "result": None,
        },
    )


@router.post("/import/csv", response_class=HTMLResponse)
async def csv_exchange_import(
    request: Request,
    dataset: str = Form("clients"),
    csv_file: UploadFile = File(None),
    csv_data: str = Form(""),
    db: Session = Depends(get_db),
):
    meta = DATASETS.get(dataset)
    if not meta:
        return render(
            request,
            "imports/csv_exchange.html",
            {
                "datasets": DATASETS,
                "dataset": "clients",
                "msg": "",
                "error": "Unknown dataset.",
                "result": None,
            },
            status_code=400,
        )
    try:
        text = await _read_text(csv_file, csv_data)
    except Exception as exc:  # noqa: BLE001
        return render(
            request,
            "imports/csv_exchange.html",
            {
                "datasets": DATASETS,
                "dataset": dataset,
                "msg": "",
                "error": str(exc),
                "result": None,
            },
            status_code=400,
        )
    if not (text or "").strip():
        return render(
            request,
            "imports/csv_exchange.html",
            {
                "datasets": DATASETS,
                "dataset": dataset,
                "msg": "",
                "error": "No file or pasted CSV provided.",
                "result": None,
            },
            status_code=400,
        )
    result = meta["reimport"](db, text)
    return render(
        request,
        "imports/csv_exchange.html",
        {
            "datasets": DATASETS,
            "dataset": dataset,
            "msg": result.summary(),
            "error": "",
            "result": result,
        },
    )

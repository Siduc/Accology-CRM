from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.import_csv import (
    import_clients,
    import_people,
    import_jobs,
    excel_bytes_to_csv_text,
)
from app.services.data_repair import repair_all_clients
from app.services.prior_import import import_prior_job_analysis, DEFAULT_CSV
from app.templating import render
from app.config import BASE_DIR

router = APIRouter(tags=["imports"])


async def _read_upload_or_paste(csv_file: UploadFile | None, csv_data: str) -> str:
    if csv_file and csv_file.filename:
        content = await csv_file.read()
        name = (csv_file.filename or "").lower()
        if name.endswith((".xlsx", ".xlsm")):
            return excel_bytes_to_csv_text(content)
        if name.endswith(".xls"):
            raise ValueError(
                "Old .xls format is not supported. In Excel use File → Save As → "
                "Excel Workbook (.xlsx) or CSV UTF-8, then upload again."
            )
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
    return csv_data or ""


@router.get("/import", response_class=HTMLResponse)
async def import_hub(request: Request):
    return render(
        request,
        "imports/hub.html",
        {"result": None, "prior_csv_exists": DEFAULT_CSV.exists()},
    )


@router.get("/import-prior-jobs", response_class=HTMLResponse)
async def import_prior_page(request: Request):
    return render(
        request,
        "imports/prior.html",
        {
            "result": None,
            "default_path": str(DEFAULT_CSV),
            "prior_csv_exists": DEFAULT_CSV.exists(),
        },
    )


@router.post("/import-prior-jobs", response_class=HTMLResponse)
async def import_prior_post(
    request: Request,
    csv_file: UploadFile = File(None),
    use_project_file: str = Form(""),
    db: Session = Depends(get_db),
):
    result = None
    try:
        if use_project_file == "yes" and DEFAULT_CSV.exists():
            result = import_prior_job_analysis(db, path=DEFAULT_CSV)
        else:
            text = await _read_upload_or_paste(csv_file, "")
            if not text.strip():
                return render(
                    request,
                    "imports/prior.html",
                    {
                        "result": {
                            "error": "Upload a CSV or tick ‘Use project PriorJobAnalysis.csv’."
                        },
                        "default_path": str(DEFAULT_CSV),
                        "prior_csv_exists": DEFAULT_CSV.exists(),
                    },
                )
            result = import_prior_job_analysis(db, text=text)
    except Exception as exc:  # noqa: BLE001
        return render(
            request,
            "imports/prior.html",
            {
                "result": {"error": str(exc)},
                "default_path": str(DEFAULT_CSV),
                "prior_csv_exists": DEFAULT_CSV.exists(),
            },
        )
    return render(
        request,
        "imports/prior.html",
        {
            "result": result,
            "default_path": str(DEFAULT_CSV),
            "prior_csv_exists": DEFAULT_CSV.exists(),
        },
    )


_CLIENT_HINT = (
    "Upload Excel (.xlsx) or CSV. Row 1 should be column headers. "
    "Required: company_number (or Company Number / Company No). "
    "Useful columns: company_name, contact_name, email, phone, address, town, "
    "postcode, client_type, status, vat_number, utr, notes. "
    "Existing company numbers are skipped (no duplicates)."
)
_PEOPLE_HINT = (
    "Upload Excel (.xlsx) or CSV. Headers e.g. full_name (or First Name + Surname), "
    "email, phone, role, company_number (to link to a client), notes. "
    "Import clients first so people can link by company number or company name."
)


@router.get("/import-clients", response_class=HTMLResponse)
async def import_clients_page(request: Request):
    return render(
        request,
        "imports/form.html",
        {
            "import_type": "clients",
            "title": "Import Clients",
            "hint": _CLIENT_HINT,
            "result": None,
        },
    )


@router.post("/import-clients", response_class=HTMLResponse)
async def import_clients_post(
    request: Request,
    csv_file: UploadFile = File(None),
    csv_data: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        text = await _read_upload_or_paste(csv_file, csv_data)
    except Exception as exc:  # noqa: BLE001
        return render(
            request,
            "imports/form.html",
            {
                "import_type": "clients",
                "title": "Import Clients",
                "hint": _CLIENT_HINT,
                "result": {"error": str(exc)},
            },
        )
    if not text.strip():
        return render(
            request,
            "imports/form.html",
            {
                "import_type": "clients",
                "title": "Import Clients",
                "hint": _CLIENT_HINT,
                "result": {"error": "No data provided."},
            },
        )
    result = import_clients(db, text)
    return render(
        request,
        "imports/form.html",
        {
            "import_type": "clients",
            "title": "Import Clients",
            "hint": _CLIENT_HINT,
            "result": result,
        },
    )


@router.get("/import-people", response_class=HTMLResponse)
async def import_people_page(request: Request):
    return render(
        request,
        "imports/form.html",
        {
            "import_type": "people",
            "title": "Import People",
            "hint": _PEOPLE_HINT,
            "result": None,
        },
    )


@router.post("/import-people", response_class=HTMLResponse)
async def import_people_post(
    request: Request,
    csv_file: UploadFile = File(None),
    csv_data: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        text = await _read_upload_or_paste(csv_file, csv_data)
    except Exception as exc:  # noqa: BLE001
        return render(
            request,
            "imports/form.html",
            {
                "import_type": "people",
                "title": "Import People",
                "hint": _PEOPLE_HINT,
                "result": {"error": str(exc)},
            },
        )
    if not text.strip():
        return render(
            request,
            "imports/form.html",
            {
                "import_type": "people",
                "title": "Import People",
                "hint": _PEOPLE_HINT,
                "result": {"error": "No data provided."},
            },
        )
    result = import_people(db, text)
    return render(
        request,
        "imports/form.html",
        {
            "import_type": "people",
            "title": "Import People",
            "hint": _PEOPLE_HINT,
            "result": result,
        },
    )


@router.get("/import-jobs", response_class=HTMLResponse)
async def import_jobs_page(request: Request):
    return render(
        request,
        "imports/form.html",
        {
            "import_type": "jobs",
            "title": "Import Jobs",
            "hint": "Headers: title, type, company_number (or client_id), period_end (YYYY-MM-DD), fee, status, is_recurring, notes",
            "result": None,
        },
    )


@router.post("/import-jobs", response_class=HTMLResponse)
async def import_jobs_post(
    request: Request,
    csv_file: UploadFile = File(None),
    csv_data: str = Form(""),
    db: Session = Depends(get_db),
):
    jobs_hint = (
        "Headers: title, type, company_number, period_end (YYYY-MM-DD), fee, status, notes"
    )
    try:
        text = await _read_upload_or_paste(csv_file, csv_data)
    except Exception as exc:  # noqa: BLE001
        return render(
            request,
            "imports/form.html",
            {
                "import_type": "jobs",
                "title": "Import Jobs",
                "hint": jobs_hint,
                "result": {"error": str(exc)},
            },
        )
    if not text.strip():
        return render(
            request,
            "imports/form.html",
            {
                "import_type": "jobs",
                "title": "Import Jobs",
                "hint": jobs_hint,
                "result": {"error": "No data provided."},
            },
        )
    result = import_jobs(db, text)
    return render(
        request,
        "imports/form.html",
        {
            "import_type": "jobs",
            "title": "Import Jobs",
            "hint": jobs_hint,
            "result": result,
        },
    )


@router.get("/repair-clients", response_class=HTMLResponse)
async def repair_clients_page(request: Request):
    return render(request, "imports/repair.html", {"result": None})


@router.post("/repair-clients", response_class=HTMLResponse)
async def repair_clients_post(request: Request, db: Session = Depends(get_db)):
    result = repair_all_clients(db)
    return render(request, "imports/repair.html", {"result": result})

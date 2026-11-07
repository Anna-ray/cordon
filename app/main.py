"""Cordon API: dynamic assessment intake plus retained demo evaluation routes."""
import json
import logging
import pathlib
import re
import sqlite3
import uuid
import zipfile
import io
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from . import sandbox, cordon
from .documents import extract_document, ExtractionError, OCRRequired

ROOT = pathlib.Path(__file__).parent.parent
CASES = {c["id"]: c for c in json.loads((ROOT / "eval/cases.json").read_text(encoding="utf-8"))}
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "cordon.db"
MAX_FILES = 12
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
ALLOWED_SUFFIXES = {".pdf", ".docx", ".txt", ".csv", ".json", ".md"}
app = FastAPI(title="Cordon", version="0.2.0")
logger = logging.getLogger("cordon")

# Bounded in-process investigation workers. SQLite remains the source of truth.
INVESTIGATION_WORKERS = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="cordon-investigation"
)
INVESTIGATION_CAPACITY = threading.BoundedSemaphore(4)
INVESTIGATION_LOCK = threading.Lock()
ACTIVE_INVESTIGATIONS = {}



def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS assessments (
                id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                objective TEXT NOT NULL DEFAULT '',
                context TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'received',
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS assessment_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                media_type TEXT,
                size_bytes INTEGER NOT NULL,
                FOREIGN KEY(assessment_id) REFERENCES assessments(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS assessment_extractions (
                file_id INTEGER PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                status TEXT NOT NULL,
                extracted_text TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(file_id) REFERENCES assessment_files(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS assessment_runs (
                id TEXT PRIMARY KEY,
                assessment_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(assessment_id) REFERENCES assessments(id)
            )
        """)

        # In-process workers do not survive a service restart.
        stale_runs = con.execute(
            """SELECT id, assessment_id, result_json FROM assessment_runs
               WHERE status IN ('queued', 'running')"""
        ).fetchall()

        for stale in stale_runs:
            try:
                payload = json.loads(stale["result_json"])
                payload.update({
                    "status": "INVESTIGATION_INTERRUPTED",
                    "run_status": "failed",
                    "error": "PROCESS_RESTARTED",
                })
            except (TypeError, ValueError):
                payload = {
                    "investigation_id": stale["id"],
                    "assessment_id": stale["assessment_id"],
                    "status": "INVESTIGATION_INTERRUPTED",
                    "run_status": "failed",
                    "error": "PROCESS_RESTARTED",
                    "assurance_state": "NOT_INDEPENDENTLY_VERIFIED",
                }

            con.execute(
                """UPDATE assessment_runs
                   SET status='failed', result_json=? WHERE id=?""",
                (json.dumps(payload), stale["id"]),
            )
            con.execute(
                """UPDATE assessments SET status='investigation_failed'
                   WHERE id=? AND status='investigating'""",
                (stale["assessment_id"],),
            )


init_db()


def assessment_payload(row):
    files = []
    with db() as con:
        for item in con.execute(
            "SELECT id, original_name, media_type, size_bytes FROM assessment_files WHERE assessment_id=?",
            (row["id"],),
        ):
            extraction = con.execute(
                "SELECT status, sources_json, metadata_json, error, extracted_text "
                "FROM assessment_extractions WHERE file_id=?",
                (item["id"],),
            ).fetchone()

            extraction_info = None
            if extraction:
                sources = json.loads(extraction["sources_json"])
                metadata = json.loads(extraction["metadata_json"])
                extraction_info = {
                    "status": extraction["status"],
                    "source_count": len(sources),
                    "source_refs": [source["ref"] for source in sources],
                    "extracted_chars": len(extraction["extracted_text"]),
                    "metadata": metadata,
                    "error": extraction["error"],
                }

            files.append({
                "name": item["original_name"],
                "media_type": item["media_type"],
                "size_bytes": item["size_bytes"],
                "extraction": extraction_info,
            })
    return {
        "id": row["id"],
        "company": row["company"],
        "objective": row["objective"],
        "context": row["context"],
        "status": row["status"],
        "created_at": row["created_at"],
        "files": files,
    }


@app.get("/api/health")
def health():
    return {
        "sandbox": "docker" if sandbox.docker_ok() else "DEGRADED (no Docker)",
        "product": "user-driven-assessments",
    }


@app.get("/api/cases")
def cases():
    """Retained for regression/demo scenarios; not used as the primary workflow."""
    return [{"id": c["id"], "name": c["name"]} for c in CASES.values()]


@app.post("/api/investigate/{cid}")
def investigate_demo(cid: str):
    if cid not in CASES:
        raise HTTPException(404, "Demo case not found")
    result = cordon.investigate(CASES[cid])
    return result


def safe_filename(name: str) -> str:
    # Keep only a display-safe basename; stored files use random identifiers.
    # Normalize both Windows and Unix path separators before display.
    cleaned = (name or "evidence").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", cleaned).strip(" .")
    return (cleaned or "evidence")[:180]


def validate_file_content(filename: str, content: bytes) -> None:
    """Reject empty files and obvious extension/content mismatches."""
    suffix = pathlib.Path(filename).suffix.lower()

    if not content:
        raise HTTPException(422, f"{filename} is empty")

    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(415, f"{filename} is not a valid PDF")
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                    raise ValueError("Missing DOCX document structure")
        except (zipfile.BadZipFile, ValueError):
            raise HTTPException(415, f"{filename} is not a valid DOCX")
    else:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(415, f"{filename} must contain valid UTF-8 text")

        if suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                raise HTTPException(415, f"{filename} contains invalid JSON")


async def read_limited(upload: UploadFile, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"{safe_filename(upload.filename)} exceeds the per-file limit")
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/assessments")
async def create_assessment(
    company: str = Form(...),
    objective: str = Form(""),
    context: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    company = company.strip()
    objective = objective.strip()
    context = context.strip()
    if not company or len(company) > 180:
        raise HTTPException(422, "Company name is required and must be at most 180 characters")
    if len(objective) > 4000 or len(context) > 12000:
        raise HTTPException(422, "Assessment text exceeds the allowed length")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"Upload at most {MAX_FILES} files")

    aid = uuid.uuid4().hex
    folder = UPLOAD_DIR / aid
    folder.mkdir(parents=True, exist_ok=False)
    saved = []
    total = 0
    try:
        for upload in files:
            if not upload.filename:
                continue
            display_name = safe_filename(upload.filename)
            suffix = pathlib.Path(display_name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise HTTPException(415, f"Unsupported file type: {suffix or 'no extension'}")
            content = await read_limited(upload, MAX_FILE_BYTES)
            validate_file_content(display_name, content)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise HTTPException(413, "Combined upload exceeds 40 MB")
            stored = f"{uuid.uuid4().hex}{suffix}"
            target = folder / stored
            target.write_bytes(content)
            saved.append((display_name, stored, upload.content_type or "application/octet-stream", len(content)))

        now = datetime.now(timezone.utc).isoformat()

        # Extract each stored file independently. A failure in one document
        # must not discard the other evidence or the original assessment.
        extraction_results = []
        for display_name, stored, media_type, size_bytes in saved:
            try:
                result = extract_document(folder / stored, display_name)
                extraction_results.append({
                    "status": "extracted",
                    "text": result["text"],
                    "sources": result["sources"],
                    "metadata": {
                        key: value for key, value in result.items()
                        if key not in ("name", "kind", "text", "sources")
                    },
                    "error": "",
                })
            except OCRRequired as exc:
                extraction_results.append({
                    "status": "ocr_required",
                    "text": "",
                    "sources": [],
                    "metadata": {},
                    "error": str(exc),
                })
            except ExtractionError as exc:
                extraction_results.append({
                    "status": "failed",
                    "text": "",
                    "sources": [],
                    "metadata": {},
                    "error": str(exc),
                })

        # Persist assessment, file records and extraction records atomically.
        with db() as con:
            con.execute(
                "INSERT INTO assessments(id, company, objective, context, status, created_at) VALUES(?,?,?,?,?,?)",
                (aid, company, objective, context, "received", now),
            )
            for item, extraction in zip(saved, extraction_results):
                display_name, stored, media_type, size_bytes = item
                cursor = con.execute(
                    "INSERT INTO assessment_files(assessment_id, original_name, stored_name, media_type, size_bytes) VALUES(?,?,?,?,?)",
                    (aid, display_name, stored, media_type, size_bytes),
                )
                con.execute(
                    """INSERT INTO assessment_extractions
                    (file_id, assessment_id, status, extracted_text, sources_json,
                     metadata_json, error, created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        cursor.lastrowid,
                        aid,
                        extraction["status"],
                        extraction["text"],
                        json.dumps(extraction["sources"], ensure_ascii=False),
                        json.dumps(extraction["metadata"], ensure_ascii=False),
                        extraction["error"],
                        now,
                    ),
                )
        with db() as con:
            row = con.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
        return assessment_payload(row)
    except Exception:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        for upload in files:
            await upload.close()


@app.get("/api/assessments")
def list_assessments():
    with db() as con:
        rows = con.execute("SELECT * FROM assessments ORDER BY created_at DESC LIMIT 100").fetchall()
    return [assessment_payload(row) for row in rows]


@app.get("/api/assessments/{assessment_id}")
def get_assessment(assessment_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Assessment not found")
    return assessment_payload(row)





def _investigation_worker(run_id, assessment_id, assessment, files):
    """Execute and persist one investigation outside the HTTP request."""
    try:
        with db() as con:
            con.execute(
                "UPDATE assessment_runs SET status=? WHERE id=?",
                ("running", run_id),
            )
            con.execute(
                "UPDATE assessments SET status=? WHERE id=?",
                ("investigating", assessment_id),
            )

        from .assessment_engine import investigate_evidence

        result = investigate_evidence(assessment, files)
        result["investigation_id"] = run_id
        result["run_status"] = "completed"

        now = datetime.now(timezone.utc).isoformat()
        with db() as con:
            con.execute(
                """UPDATE assessment_runs
                   SET status=?, result_json=?
                   WHERE id=?""",
                ("completed", json.dumps(result, ensure_ascii=False), run_id),
            )
            con.execute(
                "UPDATE assessments SET status=? WHERE id=?",
                ("review_required", assessment_id),
            )

        try:
            cordon.audit(
                "live_assessment_investigated",
                assessment=assessment_id,
                investigation=run_id,
                claims=result.get("metrics", {}).get("claims_extracted", 0),
            )
        except Exception:
            logger.exception("Investigation audit write failed: run_id=%s", run_id)

    except Exception as exc:
        logger.exception(
            "Assessment investigation failed: assessment_id=%s run_id=%s exception_type=%s",
            assessment_id,
            run_id,
            type(exc).__name__,
        )
        failure = {
            "investigation_id": run_id,
            "assessment_id": assessment_id,
            "company": assessment.get("company", ""),
            "status": "INVESTIGATION_FAILED",
            "run_status": "failed",
            "error": type(exc).__name__,
            "assurance_state": "NOT_INDEPENDENTLY_VERIFIED",
        }
        with db() as con:
            con.execute(
                """UPDATE assessment_runs
                   SET status=?, result_json=?
                   WHERE id=?""",
                ("failed", json.dumps(failure), run_id),
            )
            con.execute(
                "UPDATE assessments SET status=? WHERE id=?",
                ("investigation_failed", assessment_id),
            )
        try:
            cordon.audit(
                "live_assessment_investigation_failed",
                assessment=assessment_id,
                investigation=run_id,
                exception_type=type(exc).__name__,
            )
        except Exception:
            logger.exception("Failure audit write failed: run_id=%s", run_id)

    finally:
        with INVESTIGATION_LOCK:
            ACTIVE_INVESTIGATIONS.pop(assessment_id, None)
        INVESTIGATION_CAPACITY.release()


@app.post("/api/assessments/{assessment_id}/investigate", status_code=202)
def investigate_assessment(assessment_id: str):
    with db() as con:
        assessment_row = con.execute(
            "SELECT * FROM assessments WHERE id=?", (assessment_id,)
        ).fetchone()

        if not assessment_row:
            raise HTTPException(404, "Assessment not found")

        rows = con.execute(
            """SELECT f.original_name, e.status, e.sources_json
               FROM assessment_files f
               JOIN assessment_extractions e ON e.file_id=f.id
               WHERE f.assessment_id=?
               ORDER BY f.id""",
            (assessment_id,),
        ).fetchall()

    if not rows:
        raise HTTPException(422, "This assessment has no uploaded evidence.")

    files = [
        {
            "name": row["original_name"],
            "status": row["status"],
            "sources": json.loads(row["sources_json"]),
        }
        for row in rows
    ]

    if not any(item["status"] == "extracted" for item in files):
        raise HTTPException(
            422,
            "No evidence could be extracted. Scanned PDFs may require OCR."
        )

    assessment = dict(assessment_row)

    with INVESTIGATION_LOCK:
        # Return the existing active run instead of launching a duplicate.
        with db() as con:
            existing = con.execute(
                """SELECT id, result_json FROM assessment_runs
                   WHERE assessment_id=? AND status IN ('queued', 'running')
                   ORDER BY created_at DESC LIMIT 1""",
                (assessment_id,),
            ).fetchone()

        if existing:
            return json.loads(existing["result_json"])

        if not INVESTIGATION_CAPACITY.acquire(blocking=False):
            raise HTTPException(
                503,
                "Investigation capacity is currently full. Please retry shortly."
            )

        run_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        initial = {
            "investigation_id": run_id,
            "assessment_id": assessment_id,
            "company": assessment["company"],
            "status": "INVESTIGATION_QUEUED",
            "run_status": "queued",
            "assurance_state": "NOT_INDEPENDENTLY_VERIFIED",
        }

        try:
            with db() as con:
                con.execute(
                    """INSERT INTO assessment_runs
                       (id, assessment_id, status, result_json, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        assessment_id,
                        "queued",
                        json.dumps(initial),
                        now,
                    ),
                )

            ACTIVE_INVESTIGATIONS[assessment_id] = run_id
            INVESTIGATION_WORKERS.submit(
                _investigation_worker,
                run_id,
                assessment_id,
                assessment,
                files,
            )
        except Exception:
            ACTIVE_INVESTIGATIONS.pop(assessment_id, None)
            try:
                with db() as con:
                    con.execute(
                        """UPDATE assessment_runs
                           SET status='failed', result_json=?
                           WHERE id=? AND status='queued'""",
                        (
                            json.dumps({
                                **initial,
                                "status": "INVESTIGATION_FAILED",
                                "run_status": "failed",
                                "error": "QUEUE_SUBMISSION_FAILED",
                            }),
                            run_id,
                        ),
                    )
                    con.execute(
                        """UPDATE assessments SET status='investigation_failed'
                           WHERE id=? AND status='investigating'""",
                        (assessment_id,),
                    )
            except Exception:
                logger.exception(
                    "Could not persist queue failure: run_id=%s", run_id
                )
            finally:
                INVESTIGATION_CAPACITY.release()

            logger.exception(
                "Could not queue investigation: assessment_id=%s", assessment_id
            )
            raise HTTPException(503, "Could not queue the investigation.")

    return initial


@app.get("/api/assessments/{assessment_id}/investigation")
def get_latest_investigation(assessment_id: str):
    with db() as con:
        exists = con.execute(
            "SELECT id FROM assessments WHERE id=?", (assessment_id,)
        ).fetchone()

        if not exists:
            raise HTTPException(404, "Assessment not found")

        row = con.execute(
            """SELECT result_json FROM assessment_runs
               WHERE assessment_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (assessment_id,),
        ).fetchone()

    if not row:
        raise HTTPException(404, "No investigation has been run yet.")

    return json.loads(row["result_json"])


@app.post("/api/containment")
def containment():
    return sandbox.containment_suite()


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

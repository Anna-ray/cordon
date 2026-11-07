"""Bounded document extraction with source references for Cordon."""
import csv
import io
import json
import pathlib
import zipfile

import pymupdf as fitz
from docx import Document


MAX_EXTRACTED_CHARS = 500_000
MAX_PDF_PAGES = 500
MAX_CSV_ROWS = 10_000
MAX_DOCX_ITEMS = 20_000
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024


class ExtractionError(Exception):
    pass


class OCRRequired(ExtractionError):
    pass


def _check_size(text, name):
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ExtractionError(
            f"{name}: extracted text exceeds the {MAX_EXTRACTED_CHARS}-character limit"
        )


def _result(name, kind, sources, **metadata):
    combined = "\n\n".join(
        f"[{item['ref']}]\n{item['text']}" for item in sources if item["text"].strip()
    )
    _check_size(combined, name)

    if not combined.strip():
        raise ExtractionError(f"{name}: no extractable text found")

    return {
        "name": name,
        "kind": kind,
        "text": combined,
        "sources": sources,
        **metadata,
    }


def extract_document(path, original_name):
    """Extract text from one supported file without executing document content."""
    path = pathlib.Path(path)
    name = pathlib.Path(original_name).name
    suffix = path.suffix.lower()

    if not path.is_file():
        raise ExtractionError(f"{name}: stored file not found")

    if suffix == ".pdf":
        return _extract_pdf(path, name)
    if suffix == ".docx":
        return _extract_docx(path, name)
    if suffix == ".csv":
        return _extract_csv(path, name)
    if suffix == ".json":
        return _extract_json(path, name)
    if suffix in {".txt", ".md"}:
        return _extract_text(path, name, suffix[1:])

    raise ExtractionError(f"{name}: unsupported document type")


def _extract_pdf(path, name):
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise ExtractionError(f"{name}: could not open PDF ({type(exc).__name__})") from exc

    with doc:
        if doc.page_count > MAX_PDF_PAGES:
            raise ExtractionError(f"{name}: PDF exceeds {MAX_PDF_PAGES} pages")

        sources = []
        scanned_pages = []

        for index, page in enumerate(doc):
            text = page.get_text("text").strip()
            if not text:
                scanned_pages.append(index + 1)
                continue

            sources.append({
                "ref": f"{name}#page={index + 1}",
                "text": text,
            })

        if not sources:
            raise OCRRequired(
                f"{name}: no selectable text found; OCR is required for this scanned PDF"
            )

        return _result(
            name,
            "pdf",
            sources,
            page_count=doc.page_count,
            pages_requiring_ocr=scanned_pages,
        )


def _extract_docx(path, name):
    try:
        with zipfile.ZipFile(path) as archive:
            expanded = sum(item.file_size for item in archive.infolist())
            if expanded > MAX_DOCX_UNCOMPRESSED:
                raise ExtractionError(f"{name}: expanded DOCX content exceeds 50 MB")
            if "[Content_Types].xml" not in archive.namelist():
                raise ExtractionError(f"{name}: invalid DOCX structure")

        document = Document(str(path))
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"{name}: could not read DOCX ({type(exc).__name__})") from exc

    sources = []
    count = 0

    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if text:
            sources.append({
                "ref": f"{name}#paragraph={index}",
                "text": text,
            })
        count += 1

    for table_index, table in enumerate(document.tables, start=1):
        for row_index, row in enumerate(table.rows, start=1):
            cells = [cell.text.strip() for cell in row.cells]
            text = " | ".join(value for value in cells if value)
            if text:
                sources.append({
                    "ref": f"{name}#table={table_index},row={row_index}",
                    "text": text,
                })
            count += 1
            if count > MAX_DOCX_ITEMS:
                raise ExtractionError(f"{name}: DOCX contains too many paragraphs/table rows")

    return _result(name, "docx", sources)


def _extract_csv(path, name):
    try:
        content = path.read_text(encoding="utf-8-sig")
        reader = csv.reader(io.StringIO(content))
        rows = []

        for index, row in enumerate(reader, start=1):
            if index > MAX_CSV_ROWS:
                raise ExtractionError(f"{name}: CSV exceeds {MAX_CSV_ROWS} rows")

            cells = [cell.strip() for cell in row]
            text = " | ".join(cells)

            if text.strip():
                rows.append({
                    "ref": f"{name}#row={index}",
                    "text": text,
                    "row_number": index,
                    "cells": cells,
                })

    except UnicodeDecodeError as exc:
        raise ExtractionError(f"{name}: CSV must be UTF-8 encoded") from exc

    return _result(name, "csv", rows, row_count=len(rows))


def _extract_json(path, name):
    try:
        content = path.read_text(encoding="utf-8-sig")
        value = json.loads(content)
        rendered = json.dumps(value, ensure_ascii=False, indent=2)
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"{name}: JSON must be UTF-8 encoded") from exc
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"{name}: invalid JSON ({exc.msg})") from exc

    return _result(
        name,
        "json",
        [{"ref": f"{name}#document", "text": rendered}],
    )


def _extract_text(path, name, kind):
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError(f"{name}: text file must be UTF-8 encoded") from exc

    return _result(
        name,
        kind,
        [{"ref": f"{name}#document", "text": content}],
    )

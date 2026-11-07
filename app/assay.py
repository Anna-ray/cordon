"""Deterministic, scope-aware evidence checks for Cordon Assay."""
import json
from datetime import date, datetime, time, timedelta, timezone

REQUIRED_HEADERS = {"interval_start", "interval_end", "status"}


class AssayError(ValueError):
    pass


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise AssayError("Missing interval timestamp.")

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssayError("Invalid ISO-8601 timestamp.") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssayError("Timestamps must include a timezone.")

    return parsed


def _scope_bounds(scope_start, scope_end):
    if bool(scope_start) != bool(scope_end):
        raise AssayError("Both scope_start and scope_end must be supplied.")

    if not scope_start:
        return None

    try:
        start_date = date.fromisoformat(scope_start)
        end_date = date.fromisoformat(scope_end)
        if start_date.isoformat() != scope_start or end_date.isoformat() != scope_end:
            raise ValueError("Dates must use YYYY-MM-DD.")
        if start_date > end_date:
            raise ValueError("scope_start must not be after scope_end.")

        start = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        end = datetime.combine(end_date + timedelta(days=1), time.min,
                               tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AssayError("Invalid UTC reporting scope; use valid YYYY-MM-DD dates.") from exc

    return start, end


def _find_records(sources):
    candidates = []

    for source in sources:
        cells = source.get("cells")
        if not isinstance(cells, list) or not cells:
            continue
        headers = [str(value).strip().casefold() for value in cells]
        if REQUIRED_HEADERS.issubset(set(headers)):
            candidates.append((source, headers))

    if not candidates:
        return None, {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "No CSV with the required interval columns was found.",
        }

    if len(candidates) != 1:
        return None, {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "Multiple availability datasets require explicit selection.",
        }

    header_source, headers = candidates[0]
    positions = {name: headers.index(name) for name in REQUIRED_HEADERS}
    document = header_source["ref"].rsplit("#row=", 1)[0]
    records = []

    for source in sources:
        if not source.get("ref", "").startswith(document + "#row="):
            continue
        if source.get("row_number") == header_source.get("row_number"):
            continue

        cells = source.get("cells")
        if not isinstance(cells, list) or not any(str(x).strip() for x in cells):
            continue

        try:
            start = _timestamp(cells[positions["interval_start"]])
            end = _timestamp(cells[positions["interval_end"]])
            status = str(cells[positions["status"]]).strip().casefold()
        except (IndexError, TypeError):
            raise AssayError(
                f"Malformed CSV row {source.get('row_number')}."
            )

        if end <= start:
            raise AssayError(
                f"Non-positive interval at CSV row {source.get('row_number')}."
            )
        if status not in ("available", "unavailable"):
            raise AssayError(
                f"Unknown status at CSV row {source.get('row_number')}."
            )

        records.append({
            "start": start,
            "end": end,
            "status": status,
            "source_ref": source["ref"],
        })

    if not records:
        return None, {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "The availability CSV contains no observation intervals.",
        }

    return records, None


def calculate_availability(sources, scope_start=None, scope_end=None):
    """Calculate observed availability, enforcing an optional UTC date scope."""
    scope = _scope_bounds(scope_start, scope_end)
    records, error = _find_records(sources)
    if error:
        return error

    records.sort(key=lambda item: item["start"])

    if scope:
        expected_start, expected_end = scope
        for item in records:
            if item["start"] < expected_start or item["end"] > expected_end:
                raise AssayError(
                    "An availability interval falls outside the requested UTC scope."
                )

    for previous, current in zip(records, records[1:]):
        if current["start"] < previous["end"]:
            raise AssayError("Overlapping availability intervals detected.")
        if current["start"] > previous["end"]:
            raise AssayError("A gap exists between observation intervals.")

    observed_start = records[0]["start"]
    observed_end = records[-1]["end"]

    if scope and (observed_start > expected_start or observed_end < expected_end):
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "The supplied intervals do not cover the complete requested UTC reporting scope.",
            "scope_start": expected_start.isoformat(),
            "scope_end_exclusive": expected_end.isoformat(),
            "observed_start": observed_start.isoformat(),
            "observed_end": observed_end.isoformat(),
            "interval_count": len(records),
            "evidence_refs": [item["source_ref"] for item in records],
            "verification_status": "NOT_CALCULATED",
        }

    total_seconds = sum(
        (item["end"] - item["start"]).total_seconds() for item in records
    )
    available_seconds = sum(
        (item["end"] - item["start"]).total_seconds()
        for item in records if item["status"] == "available"
    )

    if total_seconds <= 0:
        raise AssayError("Observation period is empty.")

    complete_scope = scope is not None
    return {
        "status": "CALCULATED" if complete_scope else "OBSERVED_WINDOW_ONLY",
        "metric": "observed_availability_percentage",
        "availability_percentage": round(
            available_seconds / total_seconds * 100, 6
        ),
        "available_seconds": int(available_seconds),
        "observed_seconds": int(total_seconds),
        "period_start": observed_start.isoformat(),
        "period_end": observed_end.isoformat(),
        "interval_count": len(records),
        "evidence_refs": [item["source_ref"] for item in records],
        "scope_start": scope[0].isoformat() if scope else None,
        "scope_end_exclusive": scope[1].isoformat() if scope else None,
        "coverage": "COMPLETE" if complete_scope else "OBSERVED_WINDOW_ONLY",
        "verification_status": (
            "DETERMINISTICALLY_CALCULATED"
            if complete_scope
            else "DETERMINISTICALLY_CALCULATED_OBSERVED_WINDOW_ONLY"
        ),
    }


def verify_availability_in_sandbox(sources, scope_start=None, scope_end=None):
    """Recalculate availability in Docker and compare scope and result."""
    from . import sandbox

    expected = calculate_availability(sources, scope_start, scope_end)

    # Insufficient evidence cannot become a successful calculation.
    if expected.get("status") == "INSUFFICIENT_EVIDENCE":
        return expected

    header_candidates = []
    for source in sources:
        cells = source.get("cells")
        if not isinstance(cells, list):
            continue
        headers = [str(x).strip().casefold() for x in cells]
        if REQUIRED_HEADERS.issubset(set(headers)):
            header_candidates.append((source, headers))

    if len(header_candidates) != 1:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "Availability dataset selection is ambiguous.",
        }

    header, headers = header_candidates[0]
    positions = {name: headers.index(name) for name in REQUIRED_HEADERS}
    document = header["ref"].rsplit("#row=", 1)[0]

    intervals = []
    for source in sources:
        if not source.get("ref", "").startswith(document + "#row="):
            continue
        if source.get("row_number") == header.get("row_number"):
            continue

        cells = source.get("cells", [])
        if not isinstance(cells, list) or not any(str(x).strip() for x in cells):
            continue

        try:
            intervals.append({
                "start": cells[positions["interval_start"]],
                "end": cells[positions["interval_end"]],
                "status": cells[positions["status"]],
                "source_ref": source["ref"],
            })
        except IndexError as exc:
            raise AssayError("Malformed availability CSV row.") from exc

    scope = _scope_bounds(scope_start, scope_end)
    scope_payload = {
        "start": scope[0].isoformat() if scope else None,
        "end": scope[1].isoformat() if scope else None,
    }

    code = r'''
import json
from datetime import datetime
from pathlib import Path

data = json.loads(Path("/work/data.json").read_text())
records = data["intervals"]
scope = data["scope"]


if not isinstance(records, list) or not records:
    raise ValueError("Empty or invalid observation records")


def parse(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timezone required")
    return result

scope_start = parse(scope["start"]) if scope["start"] else None
scope_end = parse(scope["end"]) if scope["end"] else None

if bool(scope_start) != bool(scope_end):
    raise ValueError("Incomplete scope")

for item in records:
    item["start_dt"] = parse(item["start"])
    item["end_dt"] = parse(item["end"])
    item["status"] = item["status"].strip().casefold()
    if item["end_dt"] <= item["start_dt"]:
        raise ValueError("Invalid interval")
    if item["status"] not in ("available", "unavailable"):
        raise ValueError("Invalid status")
    if scope_start and (
        item["start_dt"] < scope_start or item["end_dt"] > scope_end
    ):
        raise ValueError("Interval outside requested scope")

records.sort(key=lambda x: x["start_dt"])

for previous, current in zip(records, records[1:]):
    if current["start_dt"] < previous["end_dt"]:
        raise ValueError("Overlapping intervals")
    if current["start_dt"] > previous["end_dt"]:
        raise ValueError("Observation gap")

first = records[0]["start_dt"]
last = records[-1]["end_dt"]

if scope_start and (first > scope_start or last < scope_end):
    raise ValueError("Incomplete requested scope")

total = sum(
    (x["end_dt"] - x["start_dt"]).total_seconds() for x in records
)
available = sum(
    (x["end_dt"] - x["start_dt"]).total_seconds()
    for x in records if x["status"] == "available"
)
if total <= 0:
    raise ValueError("Empty observation period")

complete = scope_start is not None
print(json.dumps({
    "status": "CALCULATED" if complete else "OBSERVED_WINDOW_ONLY",
    "available_seconds": int(available),
    "observed_seconds": int(total),
    "availability_percentage": round(available / total * 100, 6),
    "interval_count": len(records),
    "period_start": first.isoformat(),
    "period_end": last.isoformat(),
    "scope_start": scope_start.isoformat() if complete else None,
    "scope_end_exclusive": scope_end.isoformat() if complete else None,
    "coverage": "COMPLETE" if complete else "OBSERVED_WINDOW_ONLY"
}))
'''

    execution = sandbox.run(
        code,
        {"intervals": intervals, "scope": scope_payload},
        timeout=30,
    )

    if not execution.get("ok"):
        return {
            **expected,
            "verification_status": "SANDBOX_EXECUTION_FAILED",
            "sandbox": {
                "mode": execution.get("mode"),
                "exit": execution.get("exit"),
                "stderr": execution.get("stderr", ""),
            },
        }

    try:
        actual = json.loads(execution["stdout"])
    except (ValueError, TypeError):
        return {**expected, "verification_status": "SANDBOX_OUTPUT_INVALID"}

    fields = (
        "status",
        "available_seconds",
        "observed_seconds",
        "availability_percentage",
        "interval_count",
        "period_start",
        "period_end",
        "scope_start",
        "scope_end_exclusive",
        "coverage",
    )

    if not isinstance(actual, dict) or any(
        actual.get(field) != expected.get(field) for field in fields
    ):
        return {
            **expected,
            "verification_status": "SANDBOX_RESULT_MISMATCH",
        }

    return {
        **expected,
        "verification_status": (
            "SANDBOX_EXECUTION_PASSED_AND_RECOMPUTED"
            if expected["status"] == "CALCULATED"
            else "SANDBOX_RECOMPUTED_OBSERVED_WINDOW_ONLY"
        ),
        "sandbox": {
            "mode": execution["mode"],
            "exit": execution["exit"],
            "execution_ms": execution["ms"],
            "result": actual,
        },
    }

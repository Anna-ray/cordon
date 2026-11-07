"""Deterministic evidence checks for Cordon Assay."""
from datetime import datetime


REQUIRED_HEADERS = {
    "interval_start",
    "interval_end",
    "status",
}


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


def calculate_availability(sources):
    """Calculate availability only from complete, contiguous time intervals."""
    candidates = []

    for source in sources:
        cells = source.get("cells")
        if not isinstance(cells, list) or not cells:
            continue

        headers = [str(value).strip().casefold() for value in cells]

        if REQUIRED_HEADERS.issubset(set(headers)):
            candidates.append((source, headers))

    if not candidates:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "No CSV with the required interval columns was found.",
        }

    if len(candidates) != 1:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "Multiple availability datasets require explicit selection.",
        }

    header_source, headers = candidates[0]
    positions = {name: headers.index(name) for name in REQUIRED_HEADERS}
    records = []

    # Each source is one original CSV row. Header row is excluded.
    document = header_source["ref"].rsplit("#row=", 1)[0]

    for source in sources:
        if not source.get("ref", "").startswith(document + "#row="):
            continue

        row_number = source.get("row_number")
        if row_number == header_source.get("row_number"):
            continue

        cells = source.get("cells")
        if not isinstance(cells, list) or not any(str(x).strip() for x in cells):
            continue

        try:
            start = _timestamp(cells[positions["interval_start"]])
            end = _timestamp(cells[positions["interval_end"]])
            status = str(cells[positions["status"]]).strip().casefold()
        except (IndexError, TypeError):
            raise AssayError(f"Malformed CSV row {row_number}.")

        if end <= start:
            raise AssayError(f"Non-positive interval at CSV row {row_number}.")

        if status not in ("available", "unavailable"):
            raise AssayError(f"Unknown status at CSV row {row_number}.")

        records.append({
            "start": start,
            "end": end,
            "status": status,
            "source_ref": source["ref"],
        })

    if not records:
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reason": "The availability CSV contains no observation intervals.",
        }

    records.sort(key=lambda item: item["start"])

    for previous, current in zip(records, records[1:]):
        if current["start"] < previous["end"]:
            raise AssayError("Overlapping availability intervals detected.")
        if current["start"] > previous["end"]:
            raise AssayError("A gap exists between observation intervals.")

    total_seconds = sum(
        (item["end"] - item["start"]).total_seconds()
        for item in records
    )
    available_seconds = sum(
        (item["end"] - item["start"]).total_seconds()
        for item in records if item["status"] == "available"
    )

    if total_seconds <= 0:
        raise AssayError("Observation period is empty.")

    return {
        "status": "CALCULATED",
        "metric": "observed_availability_percentage",
        "availability_percentage": round(
            available_seconds / total_seconds * 100, 6
        ),
        "available_seconds": int(available_seconds),
        "observed_seconds": int(total_seconds),
        "period_start": records[0]["start"].isoformat(),
        "period_end": records[-1]["end"].isoformat(),
        "interval_count": len(records),
        "evidence_refs": [item["source_ref"] for item in records],
        "verification_status": "DETERMINISTICALLY_CALCULATED",
    }


def verify_availability_in_sandbox(sources):
    """Recalculate availability in Docker and compare with the control plane."""
    import json
    from . import sandbox

    expected = calculate_availability(sources)
    if expected.get("status") != "CALCULATED":
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

        intervals.append({
            "start": cells[positions["interval_start"]],
            "end": cells[positions["interval_end"]],
            "status": cells[positions["status"]],
            "source_ref": source["ref"],
        })

    code = r'''
import json
from datetime import datetime
from pathlib import Path

data = json.loads(Path("/work/data.json").read_text())
records = data["intervals"]

def parse(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timezone required")
    return result

for item in records:
    item["start_dt"] = parse(item["start"])
    item["end_dt"] = parse(item["end"])
    item["status"] = item["status"].strip().casefold()
    if item["end_dt"] <= item["start_dt"]:
        raise ValueError("Invalid interval")
    if item["status"] not in ("available", "unavailable"):
        raise ValueError("Invalid status")

records.sort(key=lambda x: x["start_dt"])

for previous, current in zip(records, records[1:]):
    if current["start_dt"] < previous["end_dt"]:
        raise ValueError("Overlapping intervals")
    if current["start_dt"] > previous["end_dt"]:
        raise ValueError("Observation gap")

total = sum((x["end_dt"] - x["start_dt"]).total_seconds() for x in records)
available = sum(
    (x["end_dt"] - x["start_dt"]).total_seconds()
    for x in records if x["status"] == "available"
)

if total <= 0:
    raise ValueError("Empty observation period")

print(json.dumps({
    "status": "CALCULATED",
    "available_seconds": int(available),
    "observed_seconds": int(total),
    "availability_percentage": round(available / total * 100, 6),
    "interval_count": len(records),
    "period_start": records[0]["start_dt"].isoformat(),
    "period_end": records[-1]["end_dt"].isoformat()
}))
'''

    execution = sandbox.run(code, {"intervals": intervals}, timeout=10)

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
        return {
            **expected,
            "verification_status": "SANDBOX_OUTPUT_INVALID",
        }

    fields = (
        "status",
        "available_seconds",
        "observed_seconds",
        "availability_percentage",
        "interval_count",
        "period_start",
        "period_end",
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
        "verification_status": "SANDBOX_EXECUTION_PASSED_AND_RECOMPUTED",
        "sandbox": {
            "mode": execution["mode"],
            "exit": execution["exit"],
            "execution_ms": execution["ms"],
            "result": actual,
        },
    }

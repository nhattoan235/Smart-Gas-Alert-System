#!/usr/bin/env python3
"""Summarize labeled, timestamped gas-detector evaluation traces.

This utility evaluates supplied rows only. A source label is required and
reported in normalized form; it does not convert synthetic or Wokwi evidence
into a physical-sensor claim.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from statistics import median
from typing import TextIO


SOURCE_TYPES = {"synthetic", "wokwi", "integration", "physical"}
STATES = {"WARMUP", "SAFE", "WATCH", "WARNING", "DANGER", "FAULT", "OFFLINE"}
EVALUABLE_STATES = {"SAFE", "WATCH", "WARNING", "DANGER"}
POSITIVE_STATES = {"WATCH", "WARNING", "DANGER"}
REQUIRED_COLUMNS = {
    "source_type", "run_id", "scenario", "device_id", "gas",
    "truth_hazard", "state", "valid",
}
OPTIONAL_COLUMNS = {"sample_time_ms", "truth_episode_id", "hazard_start_ms", "first_alarm_ms"}
GROUP_COLUMNS = ("scenario", "run_id", "device_id", "gas")


class EvaluationInputError(ValueError):
    """Raised for malformed or ambiguous trace input."""


def _parse_bool(value: str | None, field: str, line: int) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise EvaluationInputError(f"line {line}: {field} must be true/false or 1/0")


def _parse_optional_ms(value: str | None, field: str, line: int) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        number = int(text, 10)
    except ValueError as exc:
        raise EvaluationInputError(f"line {line}: {field} must be a non-negative integer") from exc
    if number < 0:
        raise EvaluationInputError(f"line {line}: {field} must be a non-negative integer")
    return number


def read_trace(stream: TextIO) -> tuple[list[dict], set[str]]:
    reader = csv.DictReader(stream)
    columns = reader.fieldnames or []
    if not columns:
        raise EvaluationInputError("CSV header is missing")
    if len(columns) != len(set(columns)):
        raise EvaluationInputError("CSV has duplicate column names")
    missing = sorted(REQUIRED_COLUMNS - set(columns))
    if missing:
        raise EvaluationInputError("missing required columns: " + ", ".join(missing))
    unknown = sorted(set(columns) - REQUIRED_COLUMNS - OPTIONAL_COLUMNS)
    if unknown:
        raise EvaluationInputError("unknown columns: " + ", ".join(unknown))

    rows = []
    sources = set()
    last_timestamp: dict[tuple[str, ...], int] = {}
    for line, raw in enumerate(reader, start=2):
        if None in raw:
            raise EvaluationInputError(f"line {line}: too many CSV fields")
        row = {key: (raw.get(key) or "").strip() for key in columns}
        for field in ("source_type", "run_id", "scenario", "device_id", "gas", "state"):
            if not row.get(field):
                raise EvaluationInputError(f"line {line}: {field} is required")

        source = row["source_type"].lower()
        if source not in SOURCE_TYPES:
            raise EvaluationInputError(
                f"line {line}: source_type must be one of {', '.join(sorted(SOURCE_TYPES))}"
            )
        sources.add(source)
        gas = row["gas"].upper()
        if gas not in {"LPG", "CO"}:
            raise EvaluationInputError(f"line {line}: gas must be LPG or CO")
        state = row["state"].upper()
        if state not in STATES:
            raise EvaluationInputError(f"line {line}: unknown state {row['state']!r}")

        sample_time = _parse_optional_ms(row.get("sample_time_ms"), "sample_time_ms", line)
        hazard_start = _parse_optional_ms(row.get("hazard_start_ms"), "hazard_start_ms", line)
        first_alarm = _parse_optional_ms(row.get("first_alarm_ms"), "first_alarm_ms", line)
        parsed = {
            "source_type": source,
            "run_id": row["run_id"],
            "scenario": row["scenario"],
            "device_id": row["device_id"],
            "gas": gas,
            "truth_hazard": _parse_bool(row.get("truth_hazard"), "truth_hazard", line),
            "state": state,
            "valid": _parse_bool(row.get("valid"), "valid", line),
            "sample_time_ms": sample_time,
            "truth_episode_id": row.get("truth_episode_id", "") or None,
            "hazard_start_ms": hazard_start,
            "first_alarm_ms": first_alarm,
            "_line": line,
        }
        if sample_time is not None:
            key = tuple(parsed[field] for field in GROUP_COLUMNS)
            previous = last_timestamp.get(key)
            if previous is not None and sample_time <= previous:
                raise EvaluationInputError(
                    f"line {line}: sample_time_ms must increase within scenario/run/device/gas"
                )
            last_timestamp[key] = sample_time
        rows.append(parsed)

    if not rows:
        raise EvaluationInputError("CSV contains no data rows")
    if len(sources) != 1:
        raise EvaluationInputError("one evaluation file must use exactly one source_type")
    return rows, set(columns)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _latency_stats(delays: list[int]) -> dict:
    count = len(delays)
    p95_min_episodes = 20
    ordered = sorted(delays)
    return {
        "p50_delay_ms": round(float(median(ordered)), 3) if ordered else None,
        "p95_delay_ms": ordered[math.ceil(0.95 * count) - 1] if count >= p95_min_episodes else None,
        "p95_min_measured_episodes": p95_min_episodes,
        "p95_na_reason": None if count >= p95_min_episodes else
            f"requires at least {p95_min_episodes} measured episodes; found {count}",
    }


def _latency_na(reason: str, episodes: int | None) -> dict:
    return {
        "status": "NA",
        "na_reason": reason,
        "episodes": episodes,
        "measured_delays_ms": [],
        **_latency_stats([]),
    }


def _classification(rows: list[dict], columns: set[str]) -> dict:
    counts = Counter(tp=0, fp=0, fn=0, tn=0)
    excluded = Counter()
    untimestamped_rows = 0
    timestamp_column_present = "sample_time_ms" in columns

    for row in rows:
        if not timestamp_column_present or row["sample_time_ms"] is None:
            untimestamped_rows += 1
            continue
        if not row["valid"]:
            excluded["invalid"] += 1
            continue
        if row["state"] not in EVALUABLE_STATES:
            excluded[row["state"].lower()] += 1
            continue
        truth = row["truth_hazard"]
        alarm = row["state"] in POSITIVE_STATES
        if truth and alarm:
            counts["tp"] += 1
        elif not truth and alarm:
            counts["fp"] += 1
        elif truth and not alarm:
            counts["fn"] += 1
        else:
            counts["tn"] += 1

    evaluated = sum(counts.values())
    if not timestamp_column_present or evaluated == 0:
        status = "NA"
        reason = "sample_time_ms column is required and at least one timestamped, valid, evaluable row is needed"
    else:
        status = "MEASURED"
        reason = None

    tp, fp, fn, tn = (counts[key] for key in ("tp", "fp", "fn", "tn"))
    return {
        "status": status,
        "na_reason": reason,
        "evaluated_samples": evaluated,
        "untimestamped_rows_excluded": untimestamped_rows,
        "excluded_non_evaluable_rows": dict(sorted(excluded.items())),
        "confusion": dict(counts),
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "false_alarm_rate": _ratio(fp, fp + tn),
        "miss_rate": _ratio(fn, tp + fn),
        "rate_denominators": {
            "precision": "TP + FP",
            "recall": "TP + FN",
            "false_alarm_rate": "FP / (FP + TN)",
            "miss_rate": "FN / (TP + FN)",
        },
    }


def _availability_exclusions(rows: list[dict]) -> dict:
    counts = Counter()
    for row in rows:
        if not row["valid"]:
            counts["invalid_rows"] += 1
        if row["state"] == "FAULT":
            counts["fault_rows"] += 1
        elif row["state"] == "WARMUP":
            counts["warmup_rows"] += 1
        elif row["state"] == "OFFLINE":
            counts["offline_rows"] += 1
        if row["sample_time_ms"] is None:
            counts["untimestamped_rows"] += 1
    return {
        "counts": {key: counts[key] for key in (
            "invalid_rows", "fault_rows", "warmup_rows", "offline_rows", "untimestamped_rows"
        )},
        "counts_may_overlap": True,
        "warning": (
            "Invalid, FAULT, WARMUP, OFFLINE, and untimestamped rows are excluded from confusion denominators; "
            "review these counts because excluding sensor-availability failures can make miss rates appear lower."
        ),
    }


def _latency(rows: list[dict], columns: set[str]) -> dict:
    positive = [row for row in rows if row["truth_hazard"]]
    if "truth_episode_id" not in columns:
        return _latency_na("truth_episode_id column is required", None)

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    missing_episode_id_rows = 0
    for row in positive:
        episode_id = row["truth_episode_id"]
        if not episode_id:
            missing_episode_id_rows += 1
            continue
        key = tuple(row[field] for field in GROUP_COLUMNS) + (episode_id,)
        grouped[key].append(row)

    if not grouped:
        result = _latency_na("no positive truth rows have a truth_episode_id", 0)
        result["missing_episode_id_rows"] = missing_episode_id_rows
        return result

    marker_columns_present = {"hazard_start_ms", "first_alarm_ms"} <= columns
    delays = []
    episode_counts = Counter()
    marker_conflicts = []
    for key, episode_rows in sorted(grouped.items()):
        starts = {row["hazard_start_ms"] for row in episode_rows if row["hazard_start_ms"] is not None}
        alarms = {row["first_alarm_ms"] for row in episode_rows if row["first_alarm_ms"] is not None}
        if len(starts) > 1 or len(alarms) > 1:
            marker_conflicts.append("/".join(map(str, key)))
            continue
        if not marker_columns_present:
            episode_counts["missing_markers"] += 1
            continue
        if not starts:
            episode_counts["missing_hazard_start"] += 1
            continue
        if not alarms:
            episode_counts["missing_first_alarm"] += 1
            continue
        start_ms = next(iter(starts))
        alarm_ms = next(iter(alarms))
        if alarm_ms < start_ms:
            episode_counts["prealert"] += 1
            continue
        episode_counts["measured"] += 1
        delays.append(alarm_ms - start_ms)

    if marker_conflicts:
        raise EvaluationInputError(
            "conflicting hazard/alarm timestamps within episode(s): " + ", ".join(marker_conflicts)
        )

    measured = len(delays)
    total = len(grouped)
    if not marker_columns_present:
        status = "NA"
        reason = "hazard_start_ms and first_alarm_ms columns are required"
    elif measured == 0:
        status = "NA"
        reason = "no episode has both timestamp markers in non-negative order"
    elif episode_counts["missing_hazard_start"] or episode_counts["missing_first_alarm"] or episode_counts["prealert"]:
        status = "PARTIAL"
        reason = None
    else:
        status = "MEASURED"
        reason = None

    return {
        "status": status,
        "na_reason": reason,
        "episodes": total,
        "episodes_with_measured_delay": measured,
        "episode_marker_counts": dict(sorted(episode_counts.items())),
        "missing_episode_id_rows": missing_episode_id_rows,
        "measured_delays_ms": delays,
        "mean_delay_ms": round(sum(delays) / measured, 3) if measured else None,
        "max_delay_ms": max(delays) if measured else None,
        "definition": "first_alarm_ms - hazard_start_ms from explicit per-episode markers; no interpolation",
        **_latency_stats(delays),
    }


def evaluate(rows: list[dict], columns: set[str]) -> dict:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[field] for field in GROUP_COLUMNS)].append(row)
    source = rows[0]["source_type"]
    return {
        "schema_version": 1,
        "source_type": source,
        "source_label_is_user_supplied": True,
        "row_count": len(rows),
        "group_count": len(grouped),
        "scope": "timestamped, labeled relative-signal detector states; not gas concentration or safety performance",
        "alarm_positive_states": sorted(POSITIVE_STATES),
        "classification": _classification(rows, columns),
        "availability_exclusions": _availability_exclusions(rows),
        "episode_latency": _latency(rows, columns),
        "groups": [
            {
                "scenario": key[0], "run_id": key[1], "device_id": key[2], "gas": key[3],
                "classification": _classification(group_rows, columns),
                "episode_latency": _latency(group_rows, columns),
            }
            for key, group_rows in sorted(grouped.items())
        ],
        "interpretation_limits": [
            "Confusion metrics include only rows with sample_time_ms, valid=true, and state SAFE/WATCH/WARNING/DANGER.",
            "FAULT, WARMUP, OFFLINE, invalid, and untimestamped rows are reported or excluded; they are not silently treated as SAFE.",
            "Latency uses explicit hazard_start_ms and first_alarm_ms markers grouped by truth_episode_id; it is NA when markers are absent.",
            "The source_type label is supplied by the dataset author and is not independently verified.",
            "Synthetic, Wokwi, integration, and physical datasets remain separate evidence classes; no class proves calibrated real-gas detection.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="CSV trace path, or '-' to read standard input")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            rows, columns = read_trace(sys.stdin)
        else:
            with Path(args.input).open("r", encoding="utf-8-sig", newline="") as stream:
                rows, columns = read_trace(stream)
        print(json.dumps(evaluate(rows, columns), indent=2, sort_keys=True))
        return 0
    except (OSError, EvaluationInputError) as exc:
        print(f"evaluation input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

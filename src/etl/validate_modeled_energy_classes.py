from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from src.db.session import SessionLocal
from src.etl.energy_class_model import (
    MODEL_CLASS_SCALE_YEAR,
    REFERENCE_PATH,
    ReferenceValue,
    completion_year,
    load_reference_values,
    model_energy_class,
)


ENERGY_CLASSES = "ABCDEFG"
CLASS_INDEX = {energy_class: index for index, energy_class in enumerate(ENERGY_CLASSES)}
SHADOW_QUERY = """
    SELECT DISTINCT ON (building_key)
        building_key,
        energy_certificate_class,
        energy_certificate_class_year,
        completion_date,
        main_purpose,
        heating_method,
        heating_energy_source,
        floor_area,
        total_area,
        gross_floor_area,
        number_of_storeys
    FROM main
    WHERE UPPER(TRIM(energy_certificate_class)) IN ('A', 'B', 'C', 'D', 'E', 'F', 'G')
    ORDER BY building_key;
"""
SOURCE_COUNTS_QUERY = """
    SELECT
        COUNT(*)::bigint AS official_main_rows,
        COUNT(DISTINCT building_key)::bigint AS official_buildings,
        (
            SELECT COUNT(*)
            FROM (
                SELECT building_key
                FROM main
                WHERE UPPER(TRIM(energy_certificate_class)) IN
                    ('A', 'B', 'C', 'D', 'E', 'F', 'G')
                GROUP BY building_key
                HAVING COUNT(DISTINCT ROW(
                    UPPER(TRIM(energy_certificate_class)),
                    energy_certificate_class_year,
                    completion_date,
                    main_purpose,
                    heating_method,
                    heating_energy_source,
                    floor_area,
                    total_area,
                    gross_floor_area,
                    number_of_storeys
                )) > 1
            ) conflicts
        )::bigint AS conflicting_buildings
    FROM main
    WHERE UPPER(TRIM(energy_certificate_class)) IN ('A', 'B', 'C', 'D', 'E', 'F', 'G');
"""


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def _new_metrics() -> dict[str, Any]:
    return {
        "eligible": 0,
        "exact": 0,
        "within_one": 0,
        "modeled_better": 0,
        "modeled_worse": 0,
        "class_delta_sum": 0,
        "confusion": defaultdict(Counter),
    }


def _record_metrics(
    metrics: dict[str, Any], official_class: str, modeled_class: str
) -> int:
    delta = CLASS_INDEX[modeled_class] - CLASS_INDEX[official_class]
    metrics["eligible"] += 1
    metrics["exact"] += delta == 0
    metrics["within_one"] += abs(delta) <= 1
    metrics["modeled_better"] += delta < 0
    metrics["modeled_worse"] += delta > 0
    metrics["class_delta_sum"] += delta
    metrics["confusion"][official_class][modeled_class] += 1
    return delta


def _finalize_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    eligible = int(metrics["eligible"])
    confusion = {
        official: {
            modeled: int(metrics["confusion"][official].get(modeled, 0))
            for modeled in ENERGY_CLASSES
        }
        for official in ENERGY_CLASSES
        if sum(metrics["confusion"][official].values())
    }
    return {
        "eligible": eligible,
        "exact": int(metrics["exact"]),
        "exact_rate": _rate(int(metrics["exact"]), eligible),
        "within_one": int(metrics["within_one"]),
        "within_one_rate": _rate(int(metrics["within_one"]), eligible),
        "modeled_better": int(metrics["modeled_better"]),
        "modeled_worse": int(metrics["modeled_worse"]),
        "mean_class_delta": (
            round(int(metrics["class_delta_sum"]) / eligible, 6)
            if eligible
            else None
        ),
        "confusion": confusion,
    }


def _completion_year_band(year: int) -> str:
    if year <= 1975:
        return "through_1975"
    if year <= 1977:
        return "1976_1977"
    if year <= 1984:
        return "1978_1984"
    if year <= 2002:
        return "1985_2002"
    if year <= 2007:
        return "2003_2007"
    if year <= 2009:
        return "2008_2009"
    if year <= 2011:
        return "2010_2011"
    if year <= 2017:
        return "2012_2017"
    return "2018_or_later"


def _apartment_storey_group(value: Any) -> str:
    storeys = int(value)
    if storeys <= 2:
        return "1_2"
    if storeys <= 5:
        return "3_5"
    if storeys <= 9:
        return "6_9"
    return "10_or_more"


def _segment_keys(record: Mapping[str, Any], modeled: Any) -> dict[str, str]:
    year = completion_year(record.get("completion_date"))
    segments = {
        "purpose": str(record.get("main_purpose") or "missing"),
        "construction_year_band": _completion_year_band(year),
        "heating_source": str(modeled.heating_key),
    }
    if modeled.building_type == "apartment":
        segments["apartment_storey_group"] = _apartment_storey_group(
            record["number_of_storeys"]
        )
    return segments


def validate_records(
    records: Iterable[Mapping[str, Any]],
    references: Mapping[tuple[str, str, str], ReferenceValue],
    *,
    outlier_limit: int = 25,
) -> dict[str, Any]:
    selected = 0
    scale_years: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    overall = _new_metrics()
    segment_metrics: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(_new_metrics)
    )
    outlier_count = 0
    outliers: list[dict[str, Any]] = []

    for record in records:
        selected += 1
        official_class = str(record.get("energy_certificate_class") or "").strip().upper()
        scale_year = record.get("energy_certificate_class_year")
        scale_years[str(scale_year) if scale_year is not None else "missing"] += 1

        if official_class not in CLASS_INDEX:
            exclusions["invalid_official_class"] += 1
            continue
        if scale_year != MODEL_CLASS_SCALE_YEAR:
            exclusions["official_scale_not_2018"] += 1
            continue

        # The official class is intentionally absent from this dictionary. Passing it
        # to model_energy_class would invoke official-first behavior and invalidate
        # the shadow comparison.
        model_input = {
            "completion_date": record.get("completion_date"),
            "main_purpose": record.get("main_purpose"),
            "heating_method": record.get("heating_method"),
            "heating_energy_source": record.get("heating_energy_source"),
            "floor_area": record.get("floor_area"),
            "total_area": record.get("total_area"),
            "gross_floor_area": record.get("gross_floor_area"),
            "number_of_storeys": record.get("number_of_storeys"),
        }
        modeled = model_energy_class(model_input, references)
        if modeled.energy_class is None:
            exclusions[modeled.reason or "model_returned_no_class"] += 1
            continue

        delta = _record_metrics(overall, official_class, modeled.energy_class)
        for dimension, value in _segment_keys(record, modeled).items():
            _record_metrics(
                segment_metrics[dimension][value],
                official_class,
                modeled.energy_class,
            )

        if abs(delta) >= 3:
            outlier_count += 1
            outliers.append(
                {
                    "building_key": str(record.get("building_key")),
                    "official_class": official_class,
                    "modeled_class": modeled.energy_class,
                    "class_delta": delta,
                    "purpose": str(record.get("main_purpose")),
                    "completion_year": completion_year(record.get("completion_date")),
                    "heating_key": modeled.heating_key,
                    "number_of_storeys": record.get("number_of_storeys"),
                }
            )

    outliers.sort(
        key=lambda row: (-abs(row["class_delta"]), row["building_key"])
    )
    finalized_segments = {
        dimension: {
            value: _finalize_metrics(metrics)
            for value, metrics in sorted(values.items())
        }
        for dimension, values in sorted(segment_metrics.items())
    }
    eligible = int(overall["eligible"])
    return {
        "selected_official_buildings": selected,
        "official_class_scale_years": dict(sorted(scale_years.items())),
        "eligible": eligible,
        "excluded": selected - eligible,
        "exclusions": dict(sorted(exclusions.items())),
        "overall": _finalize_metrics(overall),
        "segments": finalized_segments,
        "large_disagreements": {
            "definition": "absolute modeled-minus-official class distance >= 3",
            "count": outlier_count,
            "sample": outliers[:outlier_limit],
        },
    }


def run_validation(*, outlier_limit: int = 25) -> dict[str, Any]:
    references = load_reference_values()
    reference_path = Path(REFERENCE_PATH)
    with SessionLocal() as session:
        source_counts = dict(
            session.execute(text(SOURCE_COUNTS_QUERY)).mappings().one()
        )
        if source_counts["conflicting_buildings"]:
            raise RuntimeError(
                "Shadow validation cannot choose a representative main row: "
                f"{source_counts['conflicting_buildings']} official building keys "
                "have conflicting model inputs"
            )
        result = session.execute(
            text(SHADOW_QUERY).execution_options(stream_results=True, yield_per=5000)
        )
        validation = validate_records(
            result.mappings(), references, outlier_limit=outlier_limit
        )

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": {
            "class_scale_year": MODEL_CLASS_SCALE_YEAR,
            "reference_file": str(reference_path),
            "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            "reference_rows": len(references),
        },
        "source": {
            "table": "public.main",
            "selection": (
                "one DISTINCT building_key per latest official A-G class; "
                "only class-scale year 2018 enters accuracy metrics"
            ),
            **source_counts,
        },
        "validation": validation,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Shadow-validate modeled energy classes against official classes"
    )
    parser.add_argument(
        "--outlier-limit",
        type=int,
        default=25,
        help="Maximum number of large-disagreement examples to include.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of indented JSON.",
    )
    args = parser.parse_args()
    if args.outlier_limit < 0:
        parser.error("--outlier-limit must be non-negative")

    report = run_validation(outlier_limit=args.outlier_limit)
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

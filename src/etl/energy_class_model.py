from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping


MODEL_CLASS_SCALE_YEAR = 2018
REFERENCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "modeled_energy_class_reference.csv"
)

ENERGY_CLASSES = ("A", "B", "C", "D", "E", "F")
BUILDING_TYPES = {"05": "detached", "06": "apartment"}
SUPPORTED_HEATING_KEYS = {
    "detached": {"wood", "oil", "elecb", "delec", "distr", "awhp", "gshp"},
    "apartment": {"oil", "delec", "distr", "awhp", "gshp"},
}


@dataclass(frozen=True)
class ReferenceValue:
    building_type: str
    heating_key: str
    age_group: str
    completion_year_min: int | None
    completion_year_max: int | None
    scenario: str
    heat_kwh_m2_year: float
    electricity_kwh_m2_year: float
    e_value_kwh_m2_year: float
    class_scale_year: int
    source_workbook: str
    source_row_key: str
    source_excel_row: int


@dataclass(frozen=True)
class ModelResult:
    energy_class: str | None = None
    e_value: float | None = None
    rounded_e_value: int | None = None
    reason: str | None = None
    area_source: str | None = None
    class_scale_year: int | None = None
    building_type: str | None = None
    heating_key: str | None = None
    age_group: str | None = None


@dataclass(frozen=True)
class EffectiveEnergyClass:
    energy_class: str | None
    is_modeled: bool | None
    class_scale_year: int | None


def _optional_int(value: str) -> int | None:
    return int(value) if value else None


def load_reference_values(
    path: str | Path = REFERENCE_PATH,
) -> dict[tuple[str, str, str], ReferenceValue]:
    references: dict[tuple[str, str, str], ReferenceValue] = {}

    with Path(path).open(encoding="utf-8", newline="") as source:
        for raw in csv.DictReader(source):
            reference = ReferenceValue(
                building_type=raw["building_type"],
                heating_key=raw["heating_key"],
                age_group=raw["age_group"],
                completion_year_min=_optional_int(raw["completion_year_min"]),
                completion_year_max=_optional_int(raw["completion_year_max"]),
                scenario=raw["scenario"],
                heat_kwh_m2_year=float(raw["heat_kwh_m2_year"]),
                electricity_kwh_m2_year=float(raw["electricity_kwh_m2_year"]),
                e_value_kwh_m2_year=float(raw["e_value_kwh_m2_year"]),
                class_scale_year=int(raw["class_scale_year"]),
                source_workbook=raw["source_workbook"],
                source_row_key=raw["source_row_key"],
                source_excel_row=int(raw["source_excel_row"]),
            )
            key = (
                reference.building_type,
                reference.heating_key,
                reference.age_group,
            )
            if reference.scenario != "default":
                raise ValueError(f"Unsupported reference scenario for {key}: {reference.scenario}")
            if reference.class_scale_year != MODEL_CLASS_SCALE_YEAR:
                raise ValueError(
                    f"Unexpected class scale year for {key}: {reference.class_scale_year}"
                )
            if key in references:
                raise ValueError(f"Duplicate modeled energy class reference: {key}")
            references[key] = reference

    if len(references) != 108:
        raise ValueError(
            f"Expected 108 modeled energy class references, found {len(references)}"
        )
    return references


def completion_year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.year
    if isinstance(value, date):
        return value.year
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    raise ValueError(f"Unsupported completion date: {value!r}")


def age_group_for_year(year: int | None) -> str | None:
    if year is None:
        return None
    if year <= 1975:
        return "agegroup_1"
    if year <= 1977:
        return "agegroup_2"
    if year <= 1984:
        return "agegroup_3"
    if year <= 2002:
        return "agegroup_4"
    if year <= 2007:
        return "agegroup_5"
    if year <= 2009:
        return "agegroup_6"
    if year <= 2011:
        return "agegroup_7"
    if year <= 2017:
        return "agegroup_8"
    return "agegroup_9"


def building_type_for_purpose(main_purpose: Any) -> str | None:
    return BUILDING_TYPES.get(str(main_purpose).strip()) if main_purpose is not None else None


def heating_key_for(
    building_type: str | None,
    heating_method: Any,
    heating_energy_source: Any,
) -> str | None:
    if building_type not in SUPPORTED_HEATING_KEYS:
        return None

    method = str(heating_method).strip() if heating_method is not None else ""
    source = (
        str(heating_energy_source).strip()
        if heating_energy_source is not None
        else ""
    )

    if source == "07":
        key = "wood"
    elif source == "02":
        key = "oil"
    elif source == "04" and method == "01":
        key = "elecb"
    elif source == "04" and method == "03":
        key = "delec"
    elif source == "01":
        key = "distr"
    elif source == "1104":
        key = "awhp"
    elif source in {"09", "1101"}:
        key = "gshp"
    else:
        return None

    return key if key in SUPPORTED_HEATING_KEYS[building_type] else None


def select_detached_area(building: Mapping[str, Any]) -> tuple[float | None, str | None]:
    for field in ("floor_area", "total_area", "gross_floor_area"):
        value = building.get(field)
        try:
            area = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            area = 0.0
        if area > 0:
            return area, field
    return None, None


def class_limits(
    building_type: str,
    *,
    area: float | None = None,
    number_of_storeys: int | None = None,
) -> tuple[float, ...]:
    if building_type == "detached":
        if area is None or area < 50:
            raise ValueError("Detached-house class limits require an area of at least 50 m2")
        if area <= 150:
            return (
                110 - 0.2 * area,
                215 - 0.6 * area,
                252 - 0.6 * area,
                332 - 0.6 * area,
                462 - 0.6 * area,
                532 - 0.6 * area,
            )
        if area <= 600:
            return (
                83 - 0.02 * area,
                131 - 0.04 * area,
                173 - 0.07 * area,
                253 - 0.07 * area,
                383 - 0.07 * area,
                453 - 0.07 * area,
            )
        return (70, 106, 130, 210, 340, 410)

    if building_type == "apartment":
        if number_of_storeys is None or number_of_storeys <= 0:
            raise ValueError("Apartment class limits require a positive storey count")
        if number_of_storeys <= 2:
            return (80, 110, 150, 210, 340, 410)
        return (75, 100, 130, 160, 190, 240)

    raise ValueError(f"Unsupported building type: {building_type}")


def classify_energy_value(e_value: float, limits: tuple[float, ...]) -> tuple[str, int]:
    rounded_e_value = math.ceil(e_value)
    for energy_class, upper_limit in zip(ENERGY_CLASSES, limits, strict=True):
        if rounded_e_value <= upper_limit:
            return energy_class, rounded_e_value
    return "G", rounded_e_value


def model_energy_class(
    building: Mapping[str, Any],
    references: Mapping[tuple[str, str, str], ReferenceValue],
) -> ModelResult:
    if str(building.get("energy_certificate_class") or "").strip():
        return ModelResult()

    building_type = building_type_for_purpose(building.get("main_purpose"))
    if building_type is None:
        return ModelResult(reason="unsupported_building_purpose")

    year = completion_year(building.get("completion_date"))
    age_group = age_group_for_year(year)
    if age_group is None:
        return ModelResult(
            reason="missing_completion_date",
            building_type=building_type,
        )

    heating_key = heating_key_for(
        building_type,
        building.get("heating_method"),
        building.get("heating_energy_source"),
    )
    if heating_key is None:
        return ModelResult(
            reason="unsupported_heating_source",
            building_type=building_type,
            age_group=age_group,
        )

    area = None
    area_source = None
    number_of_storeys = building.get("number_of_storeys")
    if building_type == "detached":
        area, area_source = select_detached_area(building)
        if area is None:
            return ModelResult(
                reason="missing_heated_area",
                building_type=building_type,
                heating_key=heating_key,
                age_group=age_group,
            )
        if area < 50:
            return ModelResult(
                reason="heated_area_below_50_m2",
                area_source=area_source,
                building_type=building_type,
                heating_key=heating_key,
                age_group=age_group,
            )
    elif number_of_storeys is None or int(number_of_storeys) <= 0:
        return ModelResult(
            reason="missing_number_of_storeys",
            building_type=building_type,
            heating_key=heating_key,
            age_group=age_group,
        )

    reference = references.get((building_type, heating_key, age_group))
    if reference is None:
        return ModelResult(
            reason="missing_reference_value",
            area_source=area_source,
            building_type=building_type,
            heating_key=heating_key,
            age_group=age_group,
        )

    limits = class_limits(
        building_type,
        area=area,
        number_of_storeys=(int(number_of_storeys) if number_of_storeys is not None else None),
    )
    energy_class, rounded_e_value = classify_energy_value(
        reference.e_value_kwh_m2_year,
        limits,
    )
    return ModelResult(
        energy_class=energy_class,
        e_value=reference.e_value_kwh_m2_year,
        rounded_e_value=rounded_e_value,
        area_source=area_source,
        class_scale_year=reference.class_scale_year,
        building_type=building_type,
        heating_key=heating_key,
        age_group=age_group,
    )


def effective_energy_class(
    official_class: str | None,
    official_class_scale_year: int | None,
    modeled: ModelResult,
) -> EffectiveEnergyClass:
    if official_class and official_class.strip():
        return EffectiveEnergyClass(
            energy_class=official_class.strip().upper(),
            is_modeled=False,
            class_scale_year=official_class_scale_year,
        )
    if modeled.energy_class:
        return EffectiveEnergyClass(
            energy_class=modeled.energy_class,
            is_modeled=True,
            class_scale_year=modeled.class_scale_year,
        )
    return EffectiveEnergyClass(None, None, None)

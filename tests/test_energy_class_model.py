import unittest
from datetime import date

from src.etl.energy_class_model import (
    MODEL_CLASS_SCALE_YEAR,
    age_group_for_year,
    class_limits,
    classify_energy_value,
    effective_energy_class,
    heating_key_for,
    load_reference_values,
    model_energy_class,
    select_detached_area,
)


class EnergyClassModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.references = load_reference_values()

    def test_reference_asset_contains_all_supported_default_combinations(self) -> None:
        self.assertEqual(len(self.references), 108)
        detached = self.references[("detached", "distr", "agegroup_9")]
        apartment = self.references[("apartment", "gshp", "agegroup_9")]
        self.assertAlmostEqual(detached.e_value_kwh_m2_year, 113.02884617)
        self.assertAlmostEqual(apartment.e_value_kwh_m2_year, 111.886895631877)
        self.assertEqual(detached.class_scale_year, MODEL_CLASS_SCALE_YEAR)

    def test_age_group_boundaries(self) -> None:
        expected = {
            1975: "agegroup_1",
            1976: "agegroup_2",
            1977: "agegroup_2",
            1978: "agegroup_3",
            1984: "agegroup_3",
            1985: "agegroup_4",
            2002: "agegroup_4",
            2003: "agegroup_5",
            2007: "agegroup_5",
            2008: "agegroup_6",
            2009: "agegroup_6",
            2010: "agegroup_7",
            2011: "agegroup_7",
            2012: "agegroup_8",
            2017: "agegroup_8",
            2018: "agegroup_9",
        }
        for year, age_group in expected.items():
            with self.subTest(year=year):
                self.assertEqual(age_group_for_year(year), age_group)

    def test_heating_mapping_is_conservative_by_building_type(self) -> None:
        self.assertEqual(heating_key_for("detached", None, "07"), "wood")
        self.assertEqual(heating_key_for("detached", "01", "04"), "elecb")
        self.assertEqual(heating_key_for("detached", "03", "04"), "delec")
        self.assertEqual(heating_key_for("apartment", "03", "04"), "delec")
        self.assertEqual(heating_key_for("apartment", None, "1101"), "gshp")
        self.assertIsNone(heating_key_for("apartment", None, "07"))
        self.assertIsNone(heating_key_for("apartment", "01", "04"))
        self.assertIsNone(heating_key_for("detached", None, "99"))

    def test_area_proxy_uses_documented_precedence(self) -> None:
        self.assertEqual(
            select_detached_area(
                {"floor_area": None, "total_area": 140, "gross_floor_area": 180}
            ),
            (140.0, "total_area"),
        )
        self.assertEqual(
            select_detached_area(
                {"floor_area": 120, "total_area": 140, "gross_floor_area": 180}
            ),
            (120.0, "floor_area"),
        )

    def test_classification_rounds_up_and_includes_exact_upper_limit(self) -> None:
        apartment_limits = class_limits("apartment", number_of_storeys=2)
        self.assertEqual(classify_energy_value(80, apartment_limits), ("A", 80))
        self.assertEqual(classify_energy_value(80.01, apartment_limits), ("B", 81))
        self.assertEqual(classify_energy_value(110, apartment_limits), ("B", 110))
        self.assertEqual(classify_energy_value(110.01, apartment_limits), ("C", 111))

        detached_limits = class_limits("detached", area=120)
        self.assertEqual(classify_energy_value(85.01, detached_limits), ("A", 86))
        self.assertEqual(classify_energy_value(86.01, detached_limits), ("B", 87))

    def test_models_supported_detached_and_apartment_buildings(self) -> None:
        detached = model_energy_class(
            {
                "main_purpose": "05",
                "completion_date": date(2020, 1, 1),
                "heating_energy_source": "01",
                "floor_area": 120,
            },
            self.references,
        )
        self.assertEqual(detached.energy_class, "B")
        self.assertEqual(detached.rounded_e_value, 114)
        self.assertEqual(detached.area_source, "floor_area")
        self.assertEqual(detached.class_scale_year, MODEL_CLASS_SCALE_YEAR)

        apartment = model_energy_class(
            {
                "main_purpose": "06",
                "completion_date": date(2020, 1, 1),
                "heating_energy_source": "01",
                "number_of_storeys": 3,
            },
            self.references,
        )
        self.assertEqual(apartment.energy_class, "B")
        self.assertEqual(apartment.rounded_e_value, 99)
        self.assertIsNone(apartment.area_source)

    def test_returns_explicit_reasons_when_model_inputs_are_insufficient(self) -> None:
        cases = [
            ({"main_purpose": "01"}, "unsupported_building_purpose"),
            ({"main_purpose": "05"}, "missing_completion_date"),
            (
                {"main_purpose": "05", "completion_date": "2000-01-01"},
                "unsupported_heating_source",
            ),
            (
                {
                    "main_purpose": "05",
                    "completion_date": "2000-01-01",
                    "heating_energy_source": "01",
                },
                "missing_heated_area",
            ),
            (
                {
                    "main_purpose": "05",
                    "completion_date": "2000-01-01",
                    "heating_energy_source": "01",
                    "floor_area": 49,
                },
                "heated_area_below_50_m2",
            ),
            (
                {
                    "main_purpose": "06",
                    "completion_date": "2000-01-01",
                    "heating_energy_source": "01",
                },
                "missing_number_of_storeys",
            ),
        ]
        for building, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    model_energy_class(building, self.references).reason,
                    reason,
                )

    def test_official_class_has_effective_precedence(self) -> None:
        modeled = model_energy_class(
            {
                "main_purpose": "05",
                "completion_date": 2020,
                "heating_energy_source": "01",
                "floor_area": 120,
            },
            self.references,
        )
        official = effective_energy_class("c", 2013, modeled)
        self.assertEqual(official.energy_class, "C")
        self.assertFalse(official.is_modeled)
        self.assertEqual(official.class_scale_year, 2013)

        fallback = effective_energy_class(None, None, modeled)
        self.assertEqual(fallback.energy_class, "B")
        self.assertTrue(fallback.is_modeled)
        self.assertEqual(fallback.class_scale_year, 2018)


if __name__ == "__main__":
    unittest.main()

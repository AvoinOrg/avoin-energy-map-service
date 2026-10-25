import unittest
from datetime import date

from src.etl.energy_class_model import load_reference_values
from src.etl.validate_modeled_energy_classes import validate_records


class ModeledEnergyClassValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.references = load_reference_values()

    def test_shadow_validation_omits_official_class_from_model_inputs(self) -> None:
        base = {
            "energy_certificate_class_year": 2018,
            "completion_date": date(2020, 1, 1),
            "main_purpose": "05",
            "heating_method": None,
            "heating_energy_source": "01",
            "floor_area": 120,
            "total_area": None,
            "gross_floor_area": None,
            "number_of_storeys": None,
        }
        records = [
            {**base, "building_key": "exact", "energy_certificate_class": "B"},
            {**base, "building_key": "within", "energy_certificate_class": "C"},
            {**base, "building_key": "outlier", "energy_certificate_class": "G"},
            {
                **base,
                "building_key": "old-scale",
                "energy_certificate_class": "B",
                "energy_certificate_class_year": 2013,
            },
            {
                **base,
                "building_key": "unsupported-heating",
                "energy_certificate_class": "B",
                "heating_energy_source": "99",
            },
        ]

        report = validate_records(records, self.references)

        self.assertEqual(report["selected_official_buildings"], 5)
        self.assertEqual(report["eligible"], 3)
        self.assertEqual(report["excluded"], 2)
        self.assertEqual(report["exclusions"]["official_scale_not_2018"], 1)
        self.assertEqual(report["exclusions"]["unsupported_heating_source"], 1)
        self.assertEqual(report["overall"]["exact"], 1)
        self.assertEqual(report["overall"]["within_one"], 2)
        self.assertEqual(report["overall"]["modeled_better"], 2)
        self.assertEqual(report["overall"]["confusion"]["G"]["B"], 1)
        self.assertEqual(report["large_disagreements"]["count"], 1)
        self.assertEqual(
            report["segments"]["purpose"]["05"]["eligible"],
            3,
        )

    def test_apartment_storey_segments_are_reported(self) -> None:
        records = [
            {
                "building_key": "apartment",
                "energy_certificate_class": "B",
                "energy_certificate_class_year": 2018,
                "completion_date": date(2020, 1, 1),
                "main_purpose": "06",
                "heating_method": None,
                "heating_energy_source": "01",
                "floor_area": None,
                "total_area": None,
                "gross_floor_area": None,
                "number_of_storeys": 8,
            }
        ]

        report = validate_records(records, self.references)

        self.assertEqual(report["eligible"], 1)
        self.assertEqual(
            report["segments"]["apartment_storey_group"]["6_9"]["eligible"],
            1,
        )


if __name__ == "__main__":
    unittest.main()

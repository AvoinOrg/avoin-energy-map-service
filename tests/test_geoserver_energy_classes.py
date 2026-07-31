import re
import unittest

from src.geoserver.update_energy_certificate_class import (
    ENERGY_CLASS_ATTRIBUTES,
    ensure_certificate_class_sql,
    ensure_energy_class_attributes,
)


class GeoServerEnergyClassTest(unittest.TestCase):
    def test_virtual_table_sql_adds_every_energy_class_property_once(self) -> None:
        original = "SELECT building_key, polygon AS geom FROM main"
        updated = ensure_certificate_class_sql(original, "polygon")
        repeated = ensure_certificate_class_sql(updated, "polygon")

        self.assertEqual(updated, repeated)
        for column in ENERGY_CLASS_ATTRIBUTES:
            self.assertEqual(len(re.findall(rf"\b{re.escape(column)}\b", updated)), 1)
        self.assertLess(updated.index("energy_class_year"), updated.index("polygon AS geom"))

    def test_feature_type_attributes_have_correct_java_bindings(self) -> None:
        feature_type = {
            "attributes": {
                "attribute": [
                    {
                        "name": "energy_certificate_e_value",
                        "binding": "java.lang.Double",
                    },
                    {
                        "name": "geom",
                        "binding": "org.locationtech.jts.geom.MultiPolygon",
                    },
                ]
            }
        }

        ensure_energy_class_attributes(
            feature_type,
            "org.locationtech.jts.geom.MultiPolygon",
        )
        ensure_energy_class_attributes(
            feature_type,
            "org.locationtech.jts.geom.MultiPolygon",
        )

        attrs = feature_type["attributes"]["attribute"]
        by_name = {attribute["name"]: attribute for attribute in attrs}
        self.assertNotIn("energy_certificate_e_value", by_name)
        for column, binding in ENERGY_CLASS_ATTRIBUTES.items():
            self.assertEqual(by_name[column]["binding"], binding)
            self.assertTrue(by_name[column]["nillable"])
            self.assertEqual(
                sum(attribute.get("name") == column for attribute in attrs),
                1,
            )


if __name__ == "__main__":
    unittest.main()

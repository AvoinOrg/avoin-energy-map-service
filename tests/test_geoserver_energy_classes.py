import re
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from src.geoserver import update_energy_certificate_class as geoserver_update
from src.geoserver.ventilation import START, END
from src.geoserver.update_energy_certificate_class import (
    ENERGY_CLASS_ATTRIBUTES,
    check_datastore_jit_off,
    ensure_certificate_class_sql,
    ensure_energy_class_attributes,
)


class GeoServerEnergyClassTest(unittest.TestCase):
    def test_virtual_table_sql_adds_every_energy_class_property_once(self) -> None:
        original = "SELECT building_key, polygon AS geom FROM main"
        updated = ensure_certificate_class_sql(original, "polygon")
        repeated = ensure_certificate_class_sql(updated, "polygon")

        self.assertEqual(updated, repeated)
        # Derived ventilation CASE expressions legitimately reference the raw
        # fields again, but each raw field must still be projected only once.
        raw_projection = updated[:updated.index(START)] + updated[updated.index(END) + len(END):]
        for column in ENERGY_CLASS_ATTRIBUTES:
            self.assertEqual(len(re.findall(rf"\b{re.escape(column)}\b", raw_projection)), 1)
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

    def test_datastore_jit_check_never_rewrites_encrypted_credentials(self) -> None:
        root = ET.fromstring("""
            <dataStore>
              <connectionParameters>
                <entry key="passwd">crypt2:already-encrypted</entry>
              </connectionParameters>
            </dataStore>
        """)
        config = {
            "workspace": "workspace",
            "datastore": "datastore",
        }

        with (
            patch.object(geoserver_update, "get_xml", return_value=root),
            patch.object(geoserver_update, "put_xml") as put_xml,
        ):
            check_datastore_jit_off(config)

        put_xml.assert_not_called()
        password = root.find("./connectionParameters/entry[@key='passwd']")
        self.assertEqual(password.text, "crypt2:already-encrypted")

    def test_cache_clear_is_exact_layer_scoped_and_does_not_guess_zoom_range(self):
        config = {"workspace": "sandbox_energiakartta"}
        layer = "sandbox_energiakartta:energymap_building_polygons"
        with (
            patch.object(geoserver_update, "get_gwc_layer", return_value={}) as get_layer,
            patch.object(geoserver_update, "request", return_value=(200, b"")) as request,
        ):
            geoserver_update.truncate_gwc_layer(config, layer)
        get_layer.assert_called_once_with(config, layer)
        args, kwargs = request.call_args
        self.assertEqual(args, (config, "/gwc/rest/masstruncate"))
        self.assertEqual(kwargs["method"], "POST")
        root = ET.fromstring(kwargs["body"])
        self.assertEqual(root.tag, "truncateLayer")
        self.assertEqual(root.findtext("layerName"), layer)
        self.assertEqual(len(root), 1)


if __name__ == "__main__":
    unittest.main()

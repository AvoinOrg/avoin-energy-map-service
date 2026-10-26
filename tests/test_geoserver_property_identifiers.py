from copy import deepcopy
import unittest

from src.geoserver.publish_property_identifiers import ensure_property_identifier


class PropertyIdentifierPublicationTests(unittest.TestCase):
    def feature_type(self, geometry):
        return {
            "metadata": {"entry": [{"@key": "JDBC_VIRTUAL_TABLE", "virtualTable": {
                "sql": f"SELECT building_key, energy_class, {geometry} AS geom FROM main",
                "name": "existing-layer", "escapeSql": True,
            }}]},
            "attributes": {"attribute": [
                {"name": "energy_class", "binding": "java.lang.String"},
                {"name": "geom", "binding": "unchanged-geometry"},
            ]},
            "unrelated": {"keep": "all existing layer configuration"},
        }

    def test_adds_only_property_identifier_and_is_idempotent_on_both_geometries(self):
        for geometry in ("point", "polygon"):
            original = self.feature_type(geometry)
            before = deepcopy(original)
            result = ensure_property_identifier(original, geometry)
            self.assertEqual(original, before)
            self.assertEqual(result["unrelated"], before["unrelated"])
            self.assertEqual(ensure_property_identifier(result, geometry), result)
            sql = result["metadata"]["entry"][0]["virtualTable"]["sql"]
            self.assertEqual(sql, f"SELECT building_key, energy_class, property_identifier, {geometry} AS geom FROM main")
            self.assertEqual(result["attributes"]["attribute"][-1],
                             before["attributes"]["attribute"][-1])
            self.assertEqual(result["attributes"]["attribute"][-2]["binding"], "java.lang.String")

    def test_rejects_wrong_binding_or_duplicate_attributes(self):
        for attributes in ([{"name": "property_identifier", "binding": "java.lang.Long"}],
                           [{"name": "property_identifier", "binding": "java.lang.String"}] * 2):
            feature_type = self.feature_type("point")
            feature_type["attributes"]["attribute"].extend(attributes)
            with self.assertRaises(RuntimeError):
                ensure_property_identifier(feature_type, "point")

    def test_rejects_an_unrecognized_sql_contract(self):
        feature_type = self.feature_type("point")
        feature_type["metadata"]["entry"][0]["virtualTable"]["sql"] = "SELECT * FROM main"
        with self.assertRaises(RuntimeError):
            ensure_property_identifier(feature_type, "point")

    def test_existing_single_metadata_entry_is_preserved_without_catalog_rewrite(self):
        feature_type = ensure_property_identifier(self.feature_type("point"), "point")
        feature_type["metadata"]["entry"] = feature_type["metadata"]["entry"][0]
        self.assertEqual(ensure_property_identifier(feature_type, "point"), feature_type)


if __name__ == "__main__":
    unittest.main()

from copy import deepcopy
import sqlite3
import unittest

from src.geoserver.publish_ventilation import ensure_ventilation
from src.geoserver.update_energy_certificate_class import (
    ensure_certificate_class_sql, ensure_energy_class_attributes,
)
from src.geoserver.ventilation import (
    END, START, SOURCE_COLUMNS, VENTILATION_ATTRIBUTES, VENTILATION_TYPES,
    ensure_ventilation_sql, ventilation_expressions, verify_code_list,
)


class VentilationMappingTests(unittest.TestCase):
    def project(self, type_id=None, fi=None, sv=None):
        # These standard SQL CASE/NULLIF/TRIM expressions run on PostgreSQL too.
        columns = ", ".join(f"? AS {name}" for name in SOURCE_COLUMNS)
        expressions = ", ".join(f"{sql} AS {name}" for name, sql in ventilation_expressions().items())
        with sqlite3.connect(":memory:") as db:
            return db.execute(f"SELECT {expressions} FROM (SELECT {columns})",
                              (type_id, fi, sv)).fetchone()

    def test_every_official_code_with_and_without_description(self):
        expected = {"0": "gravity", "1": "mechanical", "2": "mechanical",
                    "3": "mechanical", "4": "other", "5": "other", "6": "other",
                    "7": "mechanical", "8": "mechanical"}
        for code, category in expected.items():
            for description in (None, "Unchanged original source description"):
                with self.subTest(code=code, description=description):
                    self.assertEqual(self.project(code, description),
                                     (category, *VENTILATION_TYPES[code][1:]))

    def test_missing_blank_unknown_and_description_only(self):
        for values, expected in (
            ((None, None, None), "no_info"),
            (("  ", " ", ""), "no_info"),
            ((None, "Painovoimainen", None), "other"),
            ((None, None, "Självdrag"), "other"),
            (("99", None, None), "other"),
            (("not-a-code", "Koneellinen", None), "other"),
        ):
            with self.subTest(values=values):
                self.assertEqual(self.project(*values), (expected, None, None))

    def test_zero_is_data_and_description_does_not_override_type(self):
        self.assertEqual(self.project(" 0 ", "Koneellinen")[0], "gravity")
        self.assertEqual(self.project("6", "Koneellinen tulo- ja poisto")[0], "other")
        self.assertNotIn("ilman lämmöntalteenottoa", self.project("1")[1])

    def test_upstream_mapping_drift_is_not_silently_accepted(self):
        rows = [{"id": int(code), "label-fi": labels[1], "label-sv": labels[2], "valid": True}
                for code, labels in VENTILATION_TYPES.items()]
        verify_code_list(rows)
        for changed in (rows[:-1], rows + [rows[0]],
                        rows + [{"id": 9, "label-fi": "New", "label-sv": "New"}]):
            with self.assertRaises(RuntimeError):
                verify_code_list(changed)
        rows[0]["label-fi"] = "Changed"
        with self.assertRaises(RuntimeError):
            verify_code_list(rows)


class VentilationPublicationTests(unittest.TestCase):
    def feature_type(self, geometry):
        return {
            "metadata": {"entry": [{"@key": "JDBC_VIRTUAL_TABLE", "virtualTable": {
                "sql": f"SELECT building_key, property_identifier, {', '.join(SOURCE_COLUMNS)}, {geometry} AS geom FROM main WHERE {geometry} IS NOT NULL",
                "name": "existing-layer", "escapeSql": True,
            }}]},
            "attributes": {"attribute": [
                {"name": "property_identifier", "binding": "java.lang.String"},
                {"name": "geom", "binding": "unchanged-geometry"},
            ]},
            "unrelated": {"keep": "all existing configuration"},
        }

    def test_additive_idempotent_both_geometries_and_metadata_shapes(self):
        for geometry in ("point", "polygon"):
            for single_entry in (False, True):
                original = self.feature_type(geometry)
                if single_entry:
                    original["metadata"]["entry"] = original["metadata"]["entry"][0]
                before = deepcopy(original)
                result = ensure_ventilation(original, geometry)
                self.assertEqual(original, before)
                self.assertEqual(ensure_ventilation(result, geometry), result)
                self.assertEqual(result["unrelated"], before["unrelated"])
                entries = result["metadata"]["entry"]
                entry = entries if single_entry else entries[0]
                sql = entry["virtualTable"]["sql"]
                unmanaged = sql[:sql.index(START)] + sql[sql.index(END) + len(END):]
                old_entries = before["metadata"]["entry"]
                old_entry = old_entries if single_entry else old_entries[0]
                self.assertEqual(unmanaged, old_entry["virtualTable"]["sql"])
                untouched = [a for a in result["attributes"]["attribute"]
                             if a["name"] not in VENTILATION_ATTRIBUTES]
                self.assertEqual(untouched, before["attributes"]["attribute"])

    def test_rejects_unknown_or_ambiguous_contracts(self):
        original = self.feature_type("point")
        sql = original["metadata"]["entry"][0]["virtualTable"]["sql"]
        for bad in ("SELECT * FROM main", sql + START, sql + END,
                    sql + END + START, sql + START + END + START + END,
                    sql.replace("building_key", "ventilation_category"),
                    sql.replace(SOURCE_COLUMNS[1], "unrelated")):
            with self.subTest(sql=bad), self.assertRaises(RuntimeError):
                ensure_ventilation_sql(bad, "point")
        for attrs in ([{"name": "ventilation_category", "binding": "java.lang.Long"}],
                      [{"name": "ventilation_category", "binding": "java.lang.String"}] * 2):
            bad = deepcopy(original)
            bad["attributes"]["attribute"].extend(attrs)
            with self.assertRaises(RuntimeError):
                ensure_ventilation(bad, "point")

    def test_normal_certificate_refresh_retains_projection_and_attributes(self):
        for geometry in ("point", "polygon"):
            sql = ensure_certificate_class_sql(f"SELECT building_key, {geometry} AS geom FROM main", geometry)
            self.assertEqual(ensure_certificate_class_sql(sql, geometry), sql)
            self.assertEqual(ensure_ventilation_sql(sql, geometry), sql)
            feature_type = {"attributes": {"attribute": [{"name": "geom", "binding": "geometry"}]}}
            ensure_energy_class_attributes(feature_type, "geometry")
            before = deepcopy(feature_type)
            ensure_energy_class_attributes(feature_type, "geometry")
            self.assertEqual(feature_type, before)
            names = [a["name"] for a in feature_type["attributes"]["attribute"]]
            self.assertTrue(set(VENTILATION_ATTRIBUTES).issubset(names))


if __name__ == "__main__":
    unittest.main()

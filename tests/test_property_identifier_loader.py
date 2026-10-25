import csv
import gzip
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from sqlalchemy import text

from db.session import SessionLocal
from etl.load_property_identifiers import (
    apply_property_identifiers,
    import_snapshot,
    validated_projection,
)


KEY = "000001ff-50cb-4535-b77d-d90e57d16918"
OTHER_KEY = "000002d2-71e2-40c7-9080-d651e3d43d48"
IDENTIFIER = "09141600110023"


class ProjectionTests(unittest.TestCase):
    def test_identifiers_preserve_leading_zeroes_and_only_project_identity(self):
        self.assertEqual(validated_projection({
            "building_key": KEY,
            "property_identifier": IDENTIFIER,
            "number_of_storeys": "99",
        }), (KEY, IDENTIFIER))

    def test_missing_and_zero_identifiers_are_unavailable(self):
        for value in (None, "", "  ", "00000000000000"):
            self.assertIsNone(validated_projection({
                "building_key": KEY, "property_identifier": value,
            }))

    def test_malformed_populated_identifiers_and_keys_abort(self):
        for value in (9141600110023, True, "091-416-0011-0023", "123", "x" * 14):
            with self.assertRaises(ValueError):
                validated_projection({"building_key": KEY, "property_identifier": value})
        for key in (None, "", "not-a-key", KEY.replace("-", "")):
            with self.assertRaises(ValueError):
                validated_projection({"building_key": key, "property_identifier": IDENTIFIER})

    def test_missing_projection_does_not_mutate_database(self):
        session = MagicMock()
        session.execute.return_value.scalar_one.return_value = False
        self.assertEqual(apply_property_identifiers(session), {
            "updated_buildings": 0, "updated_main": 0,
        })
        self.assertEqual(session.execute.call_count, 1)

    def test_apply_updates_only_property_identifier_by_exact_key(self):
        session = MagicMock()
        session.execute.return_value.scalar_one.return_value = True
        session.execute.return_value.rowcount = 2
        apply_property_identifiers(session)
        sql = [str(call.args[0]) for call in session.execute.call_args_list[1:]]
        self.assertEqual(len(sql), 2)
        for statement in sql:
            self.assertIn("SET property_identifier = p.property_identifier", statement)
            self.assertIn("building_key = p.building_key", statement)
            self.assertNotIn("number_of_storeys", statement)
            self.assertNotIn("energy_class", statement)
            self.assertNotIn("INSERT", statement)


@unittest.skipUnless(os.getenv("RUN_PROPERTY_IDENTIFIER_DB_TESTS") == "1",
                     "requires a disposable PostgreSQL test database")
class ProjectionDatabaseTests(unittest.TestCase):
    def setUp(self):
        # This opt-in suite must only run against a disposable database.
        if os.getenv("PGDATABASE") != "energy_property_identifier_test":
            raise RuntimeError("Refusing destructive fixtures outside the named disposable test database")
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "snapshot.csv.gz"
        with SessionLocal() as session:
            session.execute(text("""
                DROP TABLE IF EXISTS ryhti_property_identifiers;
                DROP TABLE IF EXISTS ryhti_property_identifier_imports;
                DROP TABLE IF EXISTS main;
                DROP TABLE IF EXISTS buildings;
                CREATE TABLE buildings (building_key text PRIMARY KEY,
                    property_identifier text, number_of_storeys integer);
                CREATE TABLE main (building_key text, property_identifier text,
                    untouched text);
                INSERT INTO buildings VALUES (:key, NULL, 7);
                INSERT INTO main VALUES (:key, NULL, 'geometry-and-class-1'),
                    (:key, NULL, 'geometry-and-class-2'),
                    (:other_key, '83705552350006', 'unchanged-unmatched');
            """), {"key": KEY, "other_key": OTHER_KEY})
            session.commit()

    def tearDown(self):
        self.temp.cleanup()

    def write_snapshot(self, rows):
        with gzip.open(self.path, "wt", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(["building_key", "property_identifier", "number_of_storeys"])
            for key, value in rows:
                writer.writerow([key, value, "999"])

    def main_values(self):
        with SessionLocal() as session:
            return session.execute(text(
                "SELECT * FROM main ORDER BY untouched"
            )).all()

    def test_real_copy_apply_and_repeat_are_safe_and_idempotent(self):
        self.write_snapshot([(KEY, IDENTIFIER), (KEY, IDENTIFIER), (OTHER_KEY, "")])
        result = import_snapshot(self.path, min_records=1)
        self.assertEqual(result["source_rows"], 3)
        self.assertEqual(result["unique_keys"], 1)
        self.assertEqual(result["identical_duplicate_rows"], 1)
        self.assertEqual(result["missing_property_identifiers"], 1)
        self.assertEqual(result["updated_main"], 2)
        self.assertEqual(result["updated_buildings"], 1)
        self.assertEqual(self.main_values(), [
            (KEY, IDENTIFIER, "geometry-and-class-1"),
            (KEY, IDENTIFIER, "geometry-and-class-2"),
            (OTHER_KEY, "83705552350006", "unchanged-unmatched"),
        ])
        with SessionLocal() as session:
            self.assertEqual(session.execute(text(
                "SELECT number_of_storeys FROM buildings"
            )).scalar_one(), 7)
        repeated = import_snapshot(self.path, min_records=1)
        self.assertEqual(repeated["updated_main"], 0)
        self.assertEqual(repeated["updated_buildings"], 0)

    def test_conflicting_keys_rollback_before_any_publication(self):
        self.write_snapshot([(KEY, IDENTIFIER), (KEY, "83705552350006")])
        before = self.main_values()
        with self.assertRaisesRegex(RuntimeError, "Conflicting"):
            import_snapshot(self.path, min_records=1)
        self.assertEqual(self.main_values(), before)

    def test_invalid_rows_and_small_snapshot_rollback(self):
        before = self.main_values()
        self.write_snapshot([(KEY, "not-an-id")])
        with self.assertRaises(ValueError):
            import_snapshot(self.path, min_records=1)
        self.write_snapshot([(KEY, IDENTIFIER)])
        with self.assertRaisesRegex(RuntimeError, "small/incomplete"):
            import_snapshot(self.path)
        self.assertEqual(self.main_values(), before)

    def test_dry_run_stages_and_validates_without_publishing(self):
        self.write_snapshot([(KEY, IDENTIFIER)])
        before = self.main_values()
        result = import_snapshot(self.path, min_records=1, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.main_values(), before)
        with SessionLocal() as session:
            self.assertIsNone(session.execute(text(
                "SELECT to_regclass('public.ryhti_property_identifiers')"
            )).scalar_one())

    def test_certificate_property_refresh_never_clears_sparse_staging_values(self):
        from etl.augment_energy_certificates import augment_property_identifiers

        self.write_snapshot([(KEY, IDENTIFIER)])
        import_snapshot(self.path, min_records=1)
        with SessionLocal() as session:
            session.execute(text("""
                UPDATE buildings SET property_identifier = NULL;
                INSERT INTO buildings VALUES (:key, NULL, 8);
            """), {"key": OTHER_KEY})
            session.commit()
        self.assertEqual(augment_property_identifiers(), 0)
        self.assertEqual(self.main_values()[-1][1], "83705552350006")


if __name__ == "__main__":
    unittest.main()

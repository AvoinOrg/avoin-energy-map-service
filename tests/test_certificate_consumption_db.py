import json
import os
import unittest

from sqlalchemy import text

from src.db.session import SessionLocal
from src.etl.augment_certificate_consumption import augment_consumption
from tests.test_certificate_consumption import certificate


@unittest.skipUnless(os.getenv("RUN_CONSUMPTION_DB_TESTS") == "1", "requires disposable PostgreSQL")
class ConsumptionDatabaseTests(unittest.TestCase):
    def setUp(self):
        if os.getenv("PGDATABASE") != "energy_consumption_test":
            raise RuntimeError("Refusing fixtures outside the named disposable test database")
        self.session = SessionLocal()
        self.addCleanup(self.session.close)
        self.addCleanup(self.session.rollback)
        self.session.execute(text("""
            DROP TABLE IF EXISTS main, energy_certificates;
            CREATE TABLE main (building_key text, permanent_building_identifier text,
                address_key text, energy_class text, floor_area double precision);
            CREATE TABLE energy_certificates (building_identifier text, certificate_id text,
                version integer, signed_at timestamp, valid_until timestamp,
                heated_net_area double precision, source_row jsonb);
            INSERT INTO main VALUES ('key1','1036581819','address1','C',0),
                ('key1','1036581819','address2','C',0), ('key2','unmatched','address3','B',100);
        """))
        self.insert(certificate())

    def insert(self, row):
        self.session.execute(text("""
            INSERT INTO energy_certificates VALUES (:building_identifier,:certificate_id,
                :version,:signed_at,:valid_until,:heated_net_area,CAST(:source_row AS jsonb))
        """), {**row, "source_row": json.dumps(row["source_row"])})

    def test_exact_join_multiple_addresses_unchanged_other_fields_and_idempotence(self):
        result = augment_consumption(self.session)
        self.assertEqual(result["updated_main_rows"], 2)
        self.assertEqual(augment_consumption(self.session)["changed_main_rows"], 0)
        rows = self.session.execute(text("SELECT energy_class,floor_area,energy_certificate_consumption FROM main ORDER BY address_key")).all()
        self.assertEqual([(r[0], r[1]) for r in rows], [("C",0),("C",0),("B",100)])
        self.assertEqual(json.loads(rows[0][2])["certificate_id"], "318510")
        self.assertEqual(rows[0][2], rows[1][2])
        self.assertIsNone(rows[2][2])

    def test_newest_missing_values_clear_old_payload_without_older_fallback(self):
        augment_consumption(self.session)
        self.insert(certificate(certificate_id="999999", signed_at="2024-01-01", source_row={}))
        augment_consumption(self.session)
        self.assertIsNone(self.session.execute(text("SELECT energy_certificate_consumption FROM main WHERE address_key='address1'")).scalar())

    def test_selection_matches_existing_tie_break_and_null_date_order(self):
        self.insert(certificate(certificate_id="999999", signed_at=None))
        self.insert(certificate(certificate_id="400000"))
        augment_consumption(self.session)
        payload = self.session.execute(text("SELECT energy_certificate_consumption FROM main WHERE address_key='address1'")).scalar()
        self.assertEqual(json.loads(payload)["certificate_id"], "400000")

    def test_dry_run_changes_neither_schema_nor_data(self):
        result = augment_consumption(self.session, apply=False)
        self.assertEqual(result["changed_main_rows"], 2)
        self.assertIsNone(self.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='main' AND column_name='energy_certificate_consumption'")).scalar())

    def test_stale_removal_and_keep_stale_option(self):
        augment_consumption(self.session)
        self.session.execute(text("DELETE FROM energy_certificates"))
        self.insert(certificate(building_identifier="unmatched"))
        self.assertEqual(augment_consumption(self.session, clear_stale=False)["updated_main_rows"], 1)
        self.assertEqual(augment_consumption(self.session)["cleared_stale_rows"], 2)

    def test_ambiguous_building_keys_and_empty_source_are_rejected(self):
        self.session.execute(text("INSERT INTO main VALUES ('different','1036581819','address4','D',1)"))
        with self.assertRaisesRegex(RuntimeError, "multiple building keys"):
            augment_consumption(self.session)

    def test_empty_source_is_rejected_before_any_schema_change(self):
        self.session.execute(text("DELETE FROM energy_certificates"))
        with self.assertRaisesRegex(RuntimeError, "empty certificate table"):
            augment_consumption(self.session)

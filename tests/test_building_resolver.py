import unittest
from unittest.mock import Mock
from src.building_resolver import resolve_building_summaries, _calendar, _timestamp, geometry_query


class BuildingResolverTests(unittest.TestCase):
    def test_empty_invalid_and_oversized_batch_do_not_query(self):
        session=Mock()
        self.assertEqual(resolve_building_summaries(session,[]),{})
        for keys in (['k'+str(i) for i in range(101)],[''],['x\0'],'a-key'):
            with self.assertRaises(Exception):resolve_building_summaries(session,keys)
        session.execute.assert_not_called()

    def test_strict_source_calendar_and_declared_utc_timestamp(self):
        self.assertEqual(str(_calendar('2024-02-29')),'2024-02-29')
        for value in ('2026-02-30','2026','2026-01-01T25:00:00','2026-01-01junk'):
            self.assertIsNone(_calendar(value))
        self.assertEqual(_timestamp('2026-10-23T12:30:00Z')[1],'known')
        self.assertEqual(_timestamp('2026-10-23T12:30:00')[1],'known')
        self.assertEqual(_timestamp('2026-10-23')[1],'invalid')
        self.assertEqual(_timestamp('2026-02-30T12:30:00Z')[1],'invalid')
        self.assertEqual(_timestamp(None),(None,'missing'))

    def test_summary_geometry_sql_does_not_return_full_polygons(self):
        sql=geometry_query({'main':{'point','polygon'},'buildings':{'point'},'polygons':{'polygon'}},detail=False)
        self.assertIn('filtered AS MATERIALIZED',sql)
        self.assertIn('building_key=ANY(:main_keys)',sql)
        self.assertIn('ST_AsEWKB(g)',sql)
        self.assertIn('NULL::jsonb',sql)
        self.assertNotIn('ctid',sql)

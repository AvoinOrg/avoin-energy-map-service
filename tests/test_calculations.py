import unittest
from src.etl.add_calculations import get_lookup_data, get_unique_column_names

class TestCalculations(unittest.TestCase):
    def test_get_unique_column_names(self):
        cols = get_unique_column_names()
        self.assertIn('oil_default_heat', cols)
        self.assertIn('wood_default_heat', cols)
        self.assertTrue(all(not c.startswith('ab_') and not c.startswith('dh_') for c in cols))

    def test_get_lookup_data(self):
        cols = get_unique_column_names()
        data = get_lookup_data(cols)
        # Should have 2 (purposes) * 9 (age groups) = 18 rows
        self.assertEqual(len(data), 18)

        # Check a specific row from Example 1: purpose 05 (omakotitalot), agegroup_2 (1976-1977)
        # dh_wood_default_heat for agegroup_2 is 373
        row_05_2 = next(r for r in data if r['main_purpose'] == '05' and r['age_group'] == 'agegroup_2')
        self.assertEqual(row_05_2['wood_default_heat'], 373.0)

        # dh_gshp_windows_total for agegroup_2 is 156,0576923
        self.assertAlmostEqual(row_05_2['gshp_windows_total'], 156.0576923)

        # ab_gshp_windows_total for agegroup_5 is 113.4898299
        row_06_5 = next(r for r in data if r['main_purpose'] == '06' and r['age_group'] == 'agegroup_5')
        self.assertAlmostEqual(row_06_5['gshp_windows_total'], 113.4898299)

        # ab_wood_default_heat does not exist. Should be None.
        self.assertIsNone(row_06_5.get('wood_default_heat'))

if __name__ == '__main__':
    unittest.main()

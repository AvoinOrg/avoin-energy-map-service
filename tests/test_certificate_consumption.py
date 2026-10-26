from copy import deepcopy
from datetime import datetime
import json
import math
import unittest
from unittest.mock import patch

from src.etl.certificate_consumption import (
    CARRIERS, SOURCE_PREFIX, consumption_payload, nonnegative_number,
)
from src.geoserver.publish_certificate_consumption import ensure_consumption, clear_consumption_cache
from src.geoserver.update_energy_certificate_class import (
    ensure_certificate_class_sql, ensure_energy_class_attributes,
)


def certificate(**overrides):
    row = {
        "certificate_id": "318510", "building_identifier": "1036581819",
        "version": 2018, "signed_at": datetime(2023, 4, 13, 11, 37, 42),
        "valid_until": datetime(2033, 4, 14), "heated_net_area": 22707.2,
        "source_row": {SOURCE_PREFIX + name: "0" for name in CARRIERS.values()},
    }
    row["source_row"].update({SOURCE_PREFIX + "Sahko": "1196074",
                              SOURCE_PREFIX + "Kaukolampo": "2994881",
                              SOURCE_PREFIX + "Kaukojaahdytys": "197293"})
    row.update(overrides)
    return row


class ConsumptionNormalizationTests(unittest.TestCase):
    def test_reported_building_and_same_certificate_provenance(self):
        payload = json.loads(consumption_payload(certificate()))
        self.assertEqual(payload["total_kwh_year"], 4388248)
        self.assertEqual(payload["heated_net_area_m2"], 22707.2)
        self.assertEqual(payload["certificate_id"], "318510")
        self.assertEqual(payload["basis"], "standardized_calculation")
        self.assertEqual(payload["unit"], "kWh/year")
        self.assertEqual(payload["completeness"], "complete")
        self.assertEqual(payload["components"][2], {"carrier": "district_cooling", "annual_kwh": 197293})
        self.assertEqual(len(payload["components"]), 5)  # real zeroes retained

    def test_decimal_commas_zero_and_invalid_numbers(self):
        for value, expected in [(" 28775,61 ", 28775.61), ("0", 0), (0, 0), ("1.5", 1.5)]:
            self.assertEqual(nonnegative_number(value), expected)
        for value in (None, True, False, "", " ", "NaN", math.nan, math.inf,
                      -1, "-1", "1,000.5", "1e309", {}, [], "1 kWh", "9" * 400):
            with self.subTest(value=value):
                self.assertIsNone(nonnegative_number(value))

    def test_partial_never_sums_missing_or_invalid_carriers_as_zero(self):
        for value in (None, "", "bad", "-1"):
            row = certificate()
            row["source_row"][SOURCE_PREFIX + "Kaukojaahdytys"] = value
            payload = json.loads(consumption_payload(row))
            self.assertEqual(payload["completeness"], "partial")
            self.assertIsNone(payload["total_kwh_year"])
            self.assertNotIn("district_cooling", [x["carrier"] for x in payload["components"]])

    def test_other_carriers_preserve_names_slots_and_completeness(self):
        row = certificate()
        row["source_row"].update({SOURCE_PREFIX + "Muu / 1 / Nimi": "Biokaasu",
                                 SOURCE_PREFIX + "Muu / 1 / Ostoenergia": "123,5"})
        payload = json.loads(consumption_payload(row))
        self.assertEqual(payload["components"][-1], {"carrier": "other", "source_slot": 1,
                                                     "name": "Biokaasu", "annual_kwh": 123.5})
        self.assertEqual(payload["total_kwh_year"], 4388371.5)
        row["source_row"][SOURCE_PREFIX + "Muu / 0 / Nimi"] = "Unknown amount"
        self.assertIsNone(json.loads(consumption_payload(row))["total_kwh_year"])

    def test_no_quantities_or_unsupported_version_do_not_create_content(self):
        for row in (certificate(source_row={}), certificate(source_row=None),
                    certificate(version=2027), certificate(certificate_id=""),
                    certificate(building_identifier=None)):
            self.assertIsNone(consumption_payload(row))

    def test_2013_partial_values_and_missing_area_and_dates(self):
        row = certificate(version=2013, heated_net_area=0, signed_at=None,
                          valid_until="invalid", source_row={SOURCE_PREFIX + "Sahko": "28775,61"})
        payload = json.loads(consumption_payload(row))
        self.assertEqual(payload["components"], [{"carrier": "electricity", "annual_kwh": 28775.61}])
        for field in ("heated_net_area_m2", "signed_at", "valid_until", "total_kwh_year"):
            self.assertIsNone(payload[field])

    def test_output_is_deterministic_and_does_not_change_source_or_use_e_value(self):
        row = certificate()
        before = deepcopy(row)
        encoded = consumption_payload(row)
        row["source_row"]["Tulokset / E-luku"] = "99999"
        self.assertEqual(consumption_payload(row), encoded)
        del row["source_row"]["Tulokset / E-luku"]
        self.assertEqual(row, before)


class ConsumptionPublicationTests(unittest.TestCase):
    def test_only_transient_quota_contention_retries_the_same_layer(self):
        with patch("src.geoserver.publish_certificate_consumption.truncate_gwc_layer") as clear, \
             patch("src.geoserver.publish_certificate_consumption.time.sleep"):
            clear.side_effect = [RuntimeError("could not serialize access due to concurrent update"), None]
            clear_consumption_cache({}, "workspace:buildings")
            self.assertEqual(clear.call_count, 2)
            self.assertTrue(all(call.args == ({}, "workspace:buildings") for call in clear.call_args_list))
            clear.reset_mock()
            clear.side_effect = RuntimeError("an unrelated failure")
            with self.assertRaisesRegex(RuntimeError, "unrelated"):
                clear_consumption_cache({}, "workspace:buildings")
            self.assertEqual(clear.call_count, 1)

    def feature(self, geometry):
        return {"metadata": {"entry": {"@key": "JDBC_VIRTUAL_TABLE", "virtualTable": {
            "sql": f"SELECT building_key, property_identifier, {geometry} AS geom FROM public.main WHERE {geometry} IS NOT NULL",
            "escapeSql": True}}}, "attributes": {"attribute": [
                {"name": "property_identifier", "binding": "java.lang.String"},
                {"name": "geom", "binding": "geometry"}]}, "keep": "unchanged"}

    def test_both_geometries_add_only_one_string_and_preserve_configuration(self):
        for geometry in ("point", "polygon"):
            before = self.feature(geometry)
            saved = deepcopy(before)
            result = ensure_consumption(before, geometry)
            self.assertEqual(before, saved)
            self.assertEqual(ensure_consumption(result, geometry), result)
            result["metadata"]["entry"]["virtualTable"]["sql"] = result["metadata"]["entry"]["virtualTable"]["sql"].replace(", energy_certificate_consumption", "")
            result["attributes"]["attribute"] = [x for x in result["attributes"]["attribute"] if x["name"] != "energy_certificate_consumption"]
            self.assertEqual(result, before)

    def test_ambiguous_sql_or_wrong_types_fail_closed(self):
        before = self.feature("point")
        for sql in ("SELECT * FROM main", "SELECT x, point AS geom FROM unrelated",
                    "SELECT energy_certificate_consumption, point AS geom FROM main"):
            bad = deepcopy(before)
            bad["metadata"]["entry"]["virtualTable"]["sql"] = sql
            with self.assertRaises(RuntimeError):
                ensure_consumption(bad, "point")
        before["attributes"]["attribute"].append({"name": "energy_certificate_consumption", "binding": "java.lang.Double"})
        with self.assertRaises(RuntimeError):
            ensure_consumption(before, "point")

    def test_ordinary_geoserver_refresh_retains_the_contract(self):
        sql = ensure_certificate_class_sql("SELECT building_key, point AS geom FROM main", "point")
        self.assertIn(", energy_certificate_consumption", sql)
        self.assertEqual(ensure_certificate_class_sql(sql, "point"), sql)
        feature = self.feature("point")
        ensure_energy_class_attributes(feature, "geometry")
        self.assertIn({"name": "energy_certificate_consumption", "binding": "java.lang.String",
                       "minOccurs": 0, "maxOccurs": 1, "nillable": True}, feature["attributes"]["attribute"])

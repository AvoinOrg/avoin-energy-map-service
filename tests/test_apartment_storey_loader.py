import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql

from etl.base_loader import BaseLoader
from etl.load_apartment_storeys import (
    APARTMENT_PURPOSE_URI,
    APARTMENT_STOREY_FILTER,
    ApartmentStoreyLoader,
    normalize_storey_count,
)


def feature(
    building_key: str = "building-1",
    storeys: object = 3,
    purpose: str = APARTMENT_PURPOSE_URI,
) -> dict:
    return {
        "properties": {
            "building_key": building_key,
            "number_of_storeys": storeys,
            "main_purpose": purpose,
            "modified_timestamp_utc": "2026-08-19T10:00:00Z",
            "floor_area": 1234,
        }
    }


class ApartmentStoreyLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = ApartmentStoreyLoader(
            base_url="https://example.test/buildings",
            api_page_size=25,
        )

    def test_filter_and_projection_are_narrow_and_authoritative(self) -> None:
        params = self.loader.initial_params()

        self.assertEqual(params["filter"], APARTMENT_STOREY_FILTER)
        self.assertEqual(params["limit"], 25)
        self.assertEqual(
            params["properties"],
            "building_key,number_of_storeys,main_purpose,modified_timestamp_utc",
        )

    def test_maps_only_storey_projection_columns(self) -> None:
        self.assertEqual(
            self.loader.map_feature_to_row(feature()),
            {
                "building_key": "building-1",
                "number_of_storeys": 3,
                "main_purpose": "06",
                "modified_timestamp_utc": "2026-08-19T10:00:00Z",
            },
        )

    def test_rejects_invalid_storeys_and_non_apartments(self) -> None:
        self.assertIsNone(self.loader.map_feature_to_row(feature(storeys=0)))
        self.assertIsNone(self.loader.map_feature_to_row(feature(storeys=2.5)))
        self.assertIsNone(
            self.loader.map_feature_to_row(
                feature(
                    purpose=(
                        "http://uri.suomi.fi/codelist/rytj/"
                        "avoin_rakennusluokitus/code/05"
                    )
                )
            )
        )

    def test_duplicate_identical_keys_are_skipped(self) -> None:
        with patch.object(
            BaseLoader,
            "iterate_features",
            return_value=iter([feature(), feature()]),
        ):
            features = list(self.loader.iterate_features())

        self.assertEqual(len(features), 1)
        self.assertEqual(self.loader.source_rows, 2)
        self.assertEqual(self.loader.duplicate_rows, 1)

    def test_conflicting_duplicate_keys_abort(self) -> None:
        with patch.object(
            BaseLoader,
            "iterate_features",
            return_value=iter([feature(storeys=2), feature(storeys=3)]),
        ):
            with self.assertRaisesRegex(RuntimeError, "Conflicting Ryhti"):
                list(self.loader.iterate_features())

    def test_storey_normalization_accepts_only_positive_integers(self) -> None:
        self.assertEqual(normalize_storey_count(1), 1)
        self.assertEqual(normalize_storey_count(2.0), 2)
        self.assertEqual(normalize_storey_count("3"), 3)
        self.assertIsNone(normalize_storey_count("4.0"))
        self.assertIsNone(normalize_storey_count(True))
        self.assertIsNone(normalize_storey_count(0))
        self.assertIsNone(normalize_storey_count(-1))
        self.assertIsNone(normalize_storey_count(1.5))
        self.assertIsNone(normalize_storey_count("many"))

    def test_projected_upsert_does_not_update_unselected_columns(self) -> None:
        database_session = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = database_session
        context.__exit__.return_value = False

        with patch("etl.base_loader.SessionLocal", return_value=context):
            written = self.loader._upsert_batch_worker([feature()])

        self.assertEqual(written, 1)
        statement = database_session.execute.call_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        self.assertIn(
            "number_of_storeys = excluded.number_of_storeys",
            sql,
        )
        self.assertIn("main_purpose = excluded.main_purpose", sql)
        self.assertNotIn("floor_area = excluded.floor_area", sql)
        self.assertNotIn("point = excluded.point", sql)


if __name__ == "__main__":
    unittest.main()

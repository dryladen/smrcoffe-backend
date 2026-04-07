import json
import tempfile
import unittest
from pathlib import Path

from pipelines.extractor import (
    normalize_google_maps_record,
    normalize_tiktok_record,
    run_extraction,
)


class ExtractorTests(unittest.TestCase):
    def test_google_maps_requires_coordinates(self) -> None:
        record = {
            "source_platform": "google_maps",
            "run_id": "run-1",
            "source_record_id": "gm-1",
            "query": "coffee in Samarinda",
            "raw": {"name": "Cafe A", "address": "Jl. Example, Samarinda"},
        }

        result = normalize_google_maps_record(record)

        self.assertEqual(result["cafes"], [])
        self.assertEqual(result["cafe_photos"], [])
        self.assertEqual(result["cafe_tags"], [])
        self.assertEqual(result["quarantine_cafes"][0]["reason_code"], "missing_required_field")
        self.assertIn("missing required field: latitude", result["quarantine_cafes"][0]["validation_errors"])
        self.assertIn("missing required field: longitude", result["quarantine_cafes"][0]["validation_errors"])

    def test_google_maps_builds_photos_and_hours(self) -> None:
        record = {
            "source_platform": "google_maps",
            "run_id": "run-1",
            "source_record_id": "gm-2",
            "query": "coffee in Samarinda",
            "raw": {
                "name": "Cafe B",
                "address": "Jl. Example, Samarinda",
                "gmaps_url": "https://maps.google.com/@-0.500,117.100,17z",
                "category": "Coffee shop",
                "opening_hours": ["Monday: 8:00 AM-10:00 PM", "Tuesday: 8:00 AM-10:00 PM"],
                "service_options": ["Outdoor seating", "Delivery", "Delivery"],
                "highlights": ["Great coffee", "Great dessert"],
                "popular_for": ["Lunch", "Dinner"],
                "offerings": ["Coffee", "Quick bite"],
                "dining_options": ["Breakfast", "Lunch", "Dinner"],
                "amenities": ["Toilet", "Wi-Fi"],
                "atmosphere": ["Casual", "Cosy"],
                "crowd": ["Family friendly", "Groups"],
                "planning": ["Accepts reservations"],
                "payments": ["Credit cards", "NFC mobile payments"],
                "children": ["Good for kids", "Kids' menu"],
                "parking": ["Paid street parking", "Plenty of parking"],
                "photos": [
                    "https://images.example.com/a.jpg",
                    {"url": "https://images.example.com/b.jpg", "caption": "Bar"},
                ],
            },
        }

        result = normalize_google_maps_record(record)

        self.assertEqual(result["quarantine"], [])
        self.assertEqual(result["cafes"][0]["opening_time"], "08:00")
        self.assertEqual(result["cafes"][0]["closing_time"], "22:00")
        self.assertEqual(result["cafes"][0]["opening_days"], ["Monday", "Tuesday"])
        self.assertEqual(result["cafes"][0]["service_options"], ["Outdoor seating", "Delivery"])
        self.assertEqual(result["cafes"][0]["highlights"], ["Great coffee", "Great dessert"])
        self.assertEqual(result["cafes"][0]["popular_for"], ["Lunch", "Dinner"])
        self.assertEqual(result["cafes"][0]["offerings"], ["Coffee", "Quick bite"])
        self.assertEqual(result["cafes"][0]["dining_options"], ["Breakfast", "Lunch", "Dinner"])
        self.assertEqual(result["cafes"][0]["amenities"], ["Toilet", "Wi-Fi"])
        self.assertEqual(result["cafes"][0]["atmosphere"], ["Casual", "Cosy"])
        self.assertEqual(result["cafes"][0]["crowd"], ["Family friendly", "Groups"])
        self.assertEqual(result["cafes"][0]["planning"], ["Accepts reservations"])
        self.assertEqual(result["cafes"][0]["payments"], ["Credit cards", "NFC mobile payments"])
        self.assertEqual(result["cafes"][0]["children"], ["Good for kids", "Kids' menu"])
        self.assertEqual(result["cafes"][0]["parking"], ["Paid street parking", "Plenty of parking"])
        self.assertEqual(len(result["cafe_photos"]), 2)
        self.assertEqual(result["cafe_photos"][1]["caption"], "Bar")

    def test_tiktok_unmatched_records_are_quarantined(self) -> None:
        record = {
            "source_platform": "tiktok",
            "run_id": "run-1",
            "source_record_id": "tt-1",
            "raw": {
                "caption": "A nice night out",
                "author_handle": "@creator",
                "video_url": "https://www.tiktok.com/@creator/video/123",
            },
        }

        result = normalize_tiktok_record(record, cafe_slugs={"known-cafe-samarinda"})

        self.assertEqual(result["influencer_reviews"], [])
        self.assertEqual(result["cafes"], [])
        self.assertEqual(result["quarantine_influencer_reviews"][0]["reason_code"], "missing_required_field")
        self.assertIn(
            "missing required field: cafe_slug",
            result["quarantine_influencer_reviews"][0]["validation_errors"],
        )

    def test_tiktok_invalid_date_is_quarantined(self) -> None:
        record = {
            "source_platform": "tiktok",
            "run_id": "run-1",
            "source_record_id": "tt-2",
            "raw": {
                "caption": "At Known Cafe Samarinda",
                "author_handle": "@creator",
                "video_url": "https://www.tiktok.com/@creator/video/456",
                "post_date": "definitely-not-a-date",
            },
        }

        result = normalize_tiktok_record(record, cafe_slugs={"known-cafe-samarinda"})

        self.assertEqual(result["influencer_reviews"], [])
        self.assertEqual(result["quarantine_influencer_reviews"][0]["reason_code"], "invalid_timestamp")

    def test_run_extraction_writes_contract_files_and_detects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_google_dir = root / "raw" / "2026-04-05" / "google_maps"
            raw_tiktok_dir = root / "raw" / "2026-04-05" / "tiktok"
            raw_google_dir.mkdir(parents=True)
            raw_tiktok_dir.mkdir(parents=True)

            google_records = [
                {
                    "source_platform": "google_maps",
                    "run_id": "run-1",
                    "source_record_id": "gm-1",
                    "query": "coffee in Samarinda",
                    "raw": {
                        "name": "Cafe C",
                        "address": "Jl. Alpha, Samarinda",
                        "latitude": -0.5,
                        "longitude": 117.1,
                        "photos": ["https://images.example.com/a.jpg"],
                    },
                },
                {
                    "source_platform": "google_maps",
                    "run_id": "run-1",
                    "source_record_id": "gm-2",
                    "query": "coffee in Samarinda",
                    "raw": {
                        "name": "Cafe C",
                        "address": "Jl. Beta, Samarinda",
                        "latitude": -0.51,
                        "longitude": 117.11,
                    },
                },
            ]
            (raw_google_dir / "google_maps_raw.json").write_text(
                json.dumps(google_records),
                encoding="utf-8",
            )

            tiktok_records = [
                {
                    "source_platform": "tiktok",
                    "run_id": "run-1",
                    "source_record_id": "tt-1",
                    "raw": {
                        "caption": "At Cafe C Samarinda #CafeCSamarinda",
                        "author_handle": "@creator",
                        "video_url": "https://www.tiktok.com/@creator/video/789",
                        "post_date": "2026-04-05",
                        "embed_html": "<blockquote>embed</blockquote>",
                    },
                }
            ]
            (raw_tiktok_dir / "tiktok_raw.json").write_text(
                json.dumps(tiktok_records),
                encoding="utf-8",
            )

            (raw_tiktok_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date="2026-04-05",
            )

            output_dir = root / "normalized" / "2026-04-05"
            cafes = json.loads((output_dir / "cafes.json").read_text(encoding="utf-8"))
            photos = json.loads((output_dir / "cafe_photos.json").read_text(encoding="utf-8"))
            reviews = json.loads((output_dir / "influencer_reviews.json").read_text(encoding="utf-8"))
            quarantine_cafes = json.loads((output_dir / "quarantine_cafes.json").read_text(encoding="utf-8"))
            quarantine_reviews = json.loads(
                (output_dir / "quarantine_influencer_reviews.json").read_text(encoding="utf-8")
            )

            self.assertEqual(summary["cafes"], 1)
            self.assertEqual(summary["cafe_photos"], 1)
            self.assertEqual(len(cafes), 1)
            self.assertEqual(len(photos), 1)
            self.assertEqual(reviews[0]["embed_code"], "<blockquote>embed</blockquote>")
            self.assertTrue(any(item["reason_code"] == "dedupe_conflict" for item in quarantine_cafes))
            self.assertTrue(any(item["reason_code"] == "schema_mismatch" for item in quarantine_reviews))


if __name__ == "__main__":
    unittest.main()

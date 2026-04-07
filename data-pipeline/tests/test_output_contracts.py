import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_corpus import load_fixture_payload
from models.schemas import (
    CafePhotoRecord,
    CafeRecord,
    CafeTagRecord,
    InfluencerReviewRecord,
    QuarantineRecord,
)
from pipelines.extractor import run_extraction


FIXTURE_RUN_DATE = "2026-04-05"
EXPECTED_OUTPUT_FILES = (
    "cafes.json",
    "cafe_photos.json",
    "cafe_tags.json",
    "influencer_reviews.json",
    "events.json",
    "quarantine.json",
    "quarantine_cafes.json",
    "quarantine_cafe_photos.json",
    "quarantine_cafe_tags.json",
    "quarantine_influencer_reviews.json",
)
MODEL_BY_FILENAME = {
    "cafes.json": CafeRecord,
    "cafe_photos.json": CafePhotoRecord,
    "cafe_tags.json": CafeTagRecord,
    "influencer_reviews.json": InfluencerReviewRecord,
    "quarantine.json": QuarantineRecord,
    "quarantine_cafes.json": QuarantineRecord,
    "quarantine_cafe_photos.json": QuarantineRecord,
    "quarantine_cafe_tags.json": QuarantineRecord,
    "quarantine_influencer_reviews.json": QuarantineRecord,
}


class OutputContractTests(unittest.TestCase):
    def _load_output_payloads(self, output_dir: Path) -> dict[str, list[dict[str, Any]]]:
        payloads: dict[str, list[dict[str, Any]]] = {}
        for filename in EXPECTED_OUTPUT_FILES:
            filepath = output_dir / filename
            self.assertTrue(filepath.exists(), f"expected output file to exist: {filename}")
            loaded = json.loads(filepath.read_text(encoding="utf-8"))
            self.assertIsInstance(loaded, list, f"expected list payload in {filename}")
            payloads[filename] = loaded
        return payloads

    def _assert_contract_payloads_validate(
        self, payloads: dict[str, list[dict[str, Any]]]
    ) -> None:
        for filename, model in MODEL_BY_FILENAME.items():
            for record in payloads[filename]:
                model(**record)

    def _assert_quarantine_trace_fields(
        self, records: list[dict[str, Any]], *, source_platform: str
    ) -> None:
        self.assertGreater(len(records), 0)
        for record in records:
            self.assertIn("run_id", record)
            self.assertIn("source_platform", record)
            self.assertIn("source_record_id", record)
            self.assertIn("validation_errors", record)
            self.assertEqual(record["source_platform"], source_platform)
            self.assertIsInstance(record["validation_errors"], list)

    def test_run_extraction_happy_path_outputs_validate_against_schema_contracts(
        self,
    ) -> None:
        google_records = deepcopy(
            load_fixture_payload(
                "raw/2026-04-05/google_maps/google_maps_raw_fixture_part1.json"
            )
        )
        google_record = google_records[0]
        google_record["raw"]["photos"] = [
            "https://images.example.com/you-front.jpg",
            {
                "url": "https://images.example.com/you-bar.jpg",
                "caption": "Bar seating",
            },
        ]

        tiktok_records = [
            {
                "source_platform": "tiktok",
                "run_id": "fixture-tiktok-contract-001",
                "source_record_id": "tt-contract-001",
                "raw": {
                    "caption": "At YOU Coffee and Brunch Samarinda #youcoffeeandbrunch #latteart",
                    "author_handle": "@contracttester",
                    "video_url": "https://www.tiktok.com/@contracttester/video/9876543210",
                    "post_date": "2026-04-05",
                    "embed_html": "<blockquote>fixture embed</blockquote>",
                    "hashtags": ["#youcoffeeandbrunch", "#latteart"],
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_google_dir = root / "raw" / FIXTURE_RUN_DATE / "google_maps"
            raw_tiktok_dir = root / "raw" / FIXTURE_RUN_DATE / "tiktok"
            raw_google_dir.mkdir(parents=True)
            raw_tiktok_dir.mkdir(parents=True)

            (raw_google_dir / "google_maps_raw_fixture.json").write_text(
                json.dumps([google_record]),
                encoding="utf-8",
            )
            (raw_tiktok_dir / "tiktok_raw_fixture.json").write_text(
                json.dumps(tiktok_records),
                encoding="utf-8",
            )

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date=FIXTURE_RUN_DATE,
            )

            payloads = self._load_output_payloads(root / "normalized" / FIXTURE_RUN_DATE)

        self._assert_contract_payloads_validate(payloads)
        self.assertEqual(summary["cafes"], 1)
        self.assertEqual(summary["cafe_photos"], 2)
        self.assertGreaterEqual(summary["cafe_tags"], 1)
        self.assertEqual(summary["influencer_reviews"], 1)
        self.assertEqual(payloads["events.json"], [])
        self.assertEqual(payloads["quarantine.json"], [])
        self.assertEqual(len(payloads["cafes.json"]), 1)
        self.assertEqual(len(payloads["cafe_photos.json"]), 2)
        self.assertGreaterEqual(len(payloads["cafe_tags.json"]), 1)
        self.assertEqual(len(payloads["influencer_reviews.json"]), 1)

    def test_run_extraction_missing_raw_date_still_writes_empty_json_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date=FIXTURE_RUN_DATE,
            )

            payloads = self._load_output_payloads(root / "normalized" / FIXTURE_RUN_DATE)

        self._assert_contract_payloads_validate(payloads)
        self.assertEqual(summary["total_raw_records"], 0)
        self.assertEqual(summary["cafes"], 0)
        self.assertEqual(summary["cafe_photos"], 0)
        self.assertEqual(summary["cafe_tags"], 0)
        self.assertEqual(summary["influencer_reviews"], 0)
        self.assertEqual(summary["quarantine"], 0)
        for filename, records in payloads.items():
            self.assertEqual(records, [], f"expected empty list payload in {filename}")

    def test_run_extraction_malformed_raw_json_emits_structurally_valid_quarantine_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_google_dir = root / "raw" / FIXTURE_RUN_DATE / "google_maps"
            raw_google_dir.mkdir(parents=True)
            (raw_google_dir / "google_maps_raw_broken.json").write_text(
                "{not valid json",
                encoding="utf-8",
            )

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date=FIXTURE_RUN_DATE,
            )

            payloads = self._load_output_payloads(root / "normalized" / FIXTURE_RUN_DATE)

        self._assert_contract_payloads_validate(payloads)
        self.assertEqual(summary["total_raw_records"], 0)
        self.assertEqual(summary["cafes"], 0)
        self.assertEqual(summary["cafe_photos"], 0)
        self.assertEqual(summary["cafe_tags"], 0)
        self.assertEqual(summary["influencer_reviews"], 0)
        self.assertEqual(summary["quarantine"], 1)
        self.assertEqual(payloads["cafes.json"], [])
        self.assertEqual(payloads["cafe_photos.json"], [])
        self.assertEqual(payloads["cafe_tags.json"], [])
        self.assertEqual(payloads["influencer_reviews.json"], [])
        self.assertEqual(payloads["events.json"], [])
        self.assertEqual(len(payloads["quarantine.json"]), 1)
        self.assertEqual(len(payloads["quarantine_cafes.json"]), 1)
        self.assertEqual(payloads["quarantine_cafe_photos.json"], [])
        self.assertEqual(payloads["quarantine_cafe_tags.json"], [])
        self.assertEqual(payloads["quarantine_influencer_reviews.json"], [])
        self._assert_quarantine_trace_fields(
            payloads["quarantine.json"], source_platform="google_maps"
        )
        self._assert_quarantine_trace_fields(
            payloads["quarantine_cafes.json"], source_platform="google_maps"
        )
        quarantine_record = payloads["quarantine.json"][0]
        self.assertEqual(quarantine_record["reason_code"], "schema_mismatch")
        self.assertIn("file", quarantine_record["raw_data"])
        self.assertIn("invalid json:", quarantine_record["validation_errors"][0])


if __name__ == "__main__":
    unittest.main()

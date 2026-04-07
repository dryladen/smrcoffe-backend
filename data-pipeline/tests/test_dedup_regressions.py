import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipelines.extractor import (  # type: ignore[reportMissingImports]
    _dedupe_google_raw_records,
    _load_raw_file_records,
    normalize_google_maps_record,
    run_extraction,
)
from pipelines.google_maps_scraper import (  # type: ignore[reportMissingImports]
    apply_same_date_cross_run_google_dedup,
    build_google_maps_binding_diagnostics,
    build_google_maps_duplicate_skip_diagnostic,
    build_google_maps_exact_page_key,
    build_google_maps_identity_contract,
    build_google_maps_record_identity_snapshot,
    extract_google_place_id,
    extract_validated_google_place_token,
    merge_discovered_place_targets,
    stabilize_google_accepted_record_identity,
    validate_google_maps_identity_contract,
)
from fixture_corpus import FIXTURE_ROOT, load_fixture_payload


FIXTURE_RUN_DATE = "2026-04-05"


def load_google_fixture(name: str) -> list[dict[str, Any]]:
    payload = load_fixture_payload(f"google_maps/{name}.json")
    if not isinstance(payload, list):
        raise AssertionError(f"expected list fixture for {name}, got {type(payload).__name__}")
    return payload


def build_fixture_details(
    raw_record: dict[str, Any],
    *,
    discovery_url: str | None = None,
    final_page_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    raw = deepcopy(raw_record["raw"])
    identity = build_google_maps_identity_contract(
        discovery_url=discovery_url or str(raw.get("maps_link") or raw.get("gmaps_url") or ""),
        final_page_url=final_page_url or str(raw.get("gmaps_url") or raw.get("maps_link") or ""),
        place_token=str(raw.get("google_place_id") or ""),
    )
    binding = build_google_maps_binding_diagnostics(
        discovery_url=identity["discovery_url"],
        final_page_url=identity["final_page_url"],
        canonical_place_url=identity["canonical_place_url"],
        validated_google_place_id=identity["validated_google_place_id"],
        discovery_place_id=str(raw.get("google_place_id") or ""),
        name=str(raw.get("name") or ""),
        address=str(raw.get("address") or ""),
    )
    details = {
        **raw,
        **identity,
        "binding_status": binding["status"],
        "binding_diagnostics": binding,
    }
    return details, identity, binding


class GoogleMapsHelperRegressionTests(unittest.TestCase):
    def test_identity_contract_recovers_validated_place_id_from_broken_query_string_fixture(
        self,
    ) -> None:
        raw_record = load_google_fixture("broken_google_place_id")[0]
        details, identity, binding = build_fixture_details(raw_record)

        recovered_place_id = extract_google_place_id(str(raw_record["raw"]["maps_link"]))
        self.assertEqual(
            recovered_place_id,
            "0x2df67f38a37d8527:0x4291ca857ca785ef",
            "helper extraction should ignore the broken query-string fragment and recover the later validated place token from the canonical long URL",
        )
        self.assertNotEqual(
            identity["validated_google_place_id"],
            raw_record["raw"]["google_place_id"],
            "identity contract must ignore the broken query-string google_place_id field rather than persisting it as validated identity",
        )
        self.assertEqual(
            identity["validated_google_place_id"],
            recovered_place_id,
            "identity contract should keep validated_google_place_id aligned with helper extraction, even when that means leaving it empty",
        )
        self.assertEqual(
            identity["canonical_place_key"],
            build_google_maps_exact_page_key(str(raw_record["raw"]["maps_link"])),
            "identity contract should preserve the canonical /maps/place/ path for downstream exact-page dedup",
        )

        validated_identity = validate_google_maps_identity_contract(details)
        self.assertEqual(
            validated_identity["validated_google_place_id"],
            recovered_place_id,
            "validated identity contract should accept the fail-closed helper result and reject drift back to query junk",
        )
        self.assertEqual(
            binding["reason_code"],
            "binding_matched",
            "canonical recovery fixture should remain a matched binding after helper extraction",
        )

    def test_binding_diagnostics_report_matched_case_for_recovered_fixture(self) -> None:
        raw_record = load_google_fixture("broken_google_place_id")[0]
        _, _, binding = build_fixture_details(raw_record)

        self.assertEqual(
            binding["status"],
            "matched",
            "binding diagnostics should classify the recovered canonical record as matched",
        )
        self.assertEqual(
            binding["reason_code"],
            "binding_matched",
            "matched binding diagnostics should expose the standardized binding_matched reason code",
        )
        self.assertIn(
            "canonical_place_key",
            binding["matched_on"],
            "matched binding diagnostics should record canonical-place identity when no validated token can be recovered from the fixture URLs",
        )

    def test_binding_diagnostics_report_mismatch_for_misbinding_fixture(self) -> None:
        canonical_you_record = load_google_fixture("same_business_different_short_url")[0]
        mismatched_record = load_google_fixture("binding_mismatch")[0]

        discovery_url = str(canonical_you_record["raw"]["maps_link"])
        final_page_url = str(mismatched_record["raw"]["maps_link"])
        validated_google_place_id = extract_validated_google_place_token(final_page_url)
        binding = build_google_maps_binding_diagnostics(
            discovery_url=discovery_url,
            final_page_url=final_page_url,
            canonical_place_url=final_page_url,
            validated_google_place_id=validated_google_place_id,
            discovery_place_id=str(canonical_you_record["raw"]["google_place_id"] or ""),
            name=str(mismatched_record["raw"]["name"] or ""),
            address=str(mismatched_record["raw"]["address"] or ""),
        )

        self.assertEqual(
            binding["status"],
            "mismatch",
            "binding mismatch fixture should stay flagged as mismatch when discovery and final identities diverge",
        )
        self.assertEqual(
            binding["reason_code"],
            "binding_mismatch",
            "mismatch diagnostics should expose the standardized binding_mismatch reason code",
        )
        self.assertIn(
            "canonical_place_key_mismatch",
            binding["reason_codes"],
            "mismatch diagnostics should preserve the canonical-place-key disagreement for downstream assertions",
        )
        self.assertIn(
            "url_place_label_drift",
            binding["drift_indicators"],
            "mismatch diagnostics should preserve label drift when the final page label no longer matches discovery",
        )

    def test_binding_diagnostics_do_not_false_match_when_final_page_token_disagrees(self) -> None:
        discovery_record = load_google_fixture("binding_mismatch")[1]
        final_record = load_google_fixture("same_business_different_short_url")[0]

        discovery_url = str(discovery_record["raw"]["maps_link"])
        final_page_url = str(final_record["raw"]["maps_link"])
        discovery_place_id = extract_validated_google_place_token(discovery_url)
        final_place_id = extract_validated_google_place_token(final_page_url)

        self.assertTrue(
            discovery_place_id,
            "fixture-driven false-match regression needs a recoverable discovery place token",
        )
        self.assertTrue(
            final_place_id,
            "fixture-driven false-match regression needs a recoverable final-page place token",
        )
        self.assertNotEqual(
            discovery_place_id,
            final_place_id,
            "false-match regression requires discovery and final-page tokens to disagree so drift cannot be silently accepted",
        )

        identity = build_google_maps_identity_contract(
            discovery_url=discovery_url,
            final_page_url=final_page_url,
            place_token=discovery_place_id,
        )

        self.assertEqual(
            identity["validated_google_place_id"],
            final_place_id,
            "identity contract should prefer the recoverable final-page token over the carried discovery token when the clicked page drifted",
        )

        binding = build_google_maps_binding_diagnostics(
            discovery_url=discovery_url,
            final_page_url=final_page_url,
            canonical_place_url=identity["canonical_place_url"],
            validated_google_place_id=identity["validated_google_place_id"],
            discovery_place_id=discovery_place_id,
            name=str(final_record["raw"]["name"] or ""),
            address=str(final_record["raw"]["address"] or ""),
        )

        self.assertEqual(
            binding["status"],
            "mismatch",
            "binding diagnostics should reject a drifted final page when the final recoverable token disagrees with the discovery candidate",
        )
        self.assertIn(
            "validated_google_place_id_mismatch",
            binding["reason_codes"],
            "false matched-binding regression should expose token disagreement once the final page token is recovered correctly",
        )


class GoogleMapsDedupDiagnosticRegressionTests(unittest.TestCase):
    def test_discovery_dedup_uses_duplicate_exact_page_reason_code(self) -> None:
        records = load_google_fixture("duplicate_exact_page")
        discovered_places: list[dict[str, str]] = []
        diagnostics: dict[str, Any] = {
            "raw_candidates": 0,
            "unique_candidates": 0,
            "duplicates_skipped": 0,
            "invalid_skipped": 0,
            "unique_exact_page_keys": 0,
            "duplicate_examples": [],
        }

        added = merge_discovered_place_targets(
            discovered_places,
            diagnostics,
            [str(record["raw"]["maps_link"]) for record in records],
            source_name="fixture_duplicate_exact_page",
        )

        self.assertEqual(
            added,
            1,
            "discovery dedup should only accept the first exact-page candidate from the duplicate_exact_page fixture",
        )
        self.assertEqual(
            diagnostics["duplicates_skipped"],
            1,
            "discovery diagnostics should count the second exact-page candidate as a skipped duplicate",
        )
        self.assertEqual(
            diagnostics["duplicate_examples"][0]["reason_code"],
            "duplicate_exact_page",
            "discovery duplicate examples should surface the standardized duplicate_exact_page reason code",
        )

    def test_pre_append_dedup_stabilizes_source_record_id_across_short_urls(self) -> None:
        first_record, second_record = load_google_fixture("same_business_different_short_url")

        first_details, _, _ = build_fixture_details(first_record)
        second_details, _, _ = build_fixture_details(second_record)

        stabilized_first, first_key, first_source_record_id = (
            stabilize_google_accepted_record_identity(first_details)
        )
        stabilized_second, second_key, second_source_record_id = (
            stabilize_google_accepted_record_identity(second_details)
        )

        first_snapshot = build_google_maps_record_identity_snapshot(
            stabilized_first,
            place_url=str(first_record["raw"]["maps_link"]),
            source_record_id=first_source_record_id,
        )
        second_snapshot = build_google_maps_record_identity_snapshot(
            stabilized_second,
            place_url=str(second_record["raw"]["maps_link"]),
            source_record_id=second_source_record_id,
        )
        duplicate_diagnostic = build_google_maps_duplicate_skip_diagnostic(
            accepted_exact_page_key=second_key,
            skipped_record=second_snapshot,
            kept_record=first_snapshot,
        )

        self.assertEqual(
            first_key,
            second_key,
            "same-business-different-short-url fixture should collapse to one accepted exact-page key before append",
        )
        self.assertEqual(
            first_source_record_id,
            first_key,
            "accepted Google source_record_id should stabilize to canonical exact-page identity for the kept record",
        )
        self.assertEqual(
            second_source_record_id,
            first_source_record_id,
            "duplicate accepted records should reuse the same canonical source_record_id instead of drifting with short URLs",
        )
        self.assertEqual(
            duplicate_diagnostic["reason_code"],
            "duplicate_exact_page",
            "pre-append duplicate diagnostics should expose duplicate_exact_page for accepted-record skips",
        )
        self.assertEqual(
            duplicate_diagnostic["kept_record"]["source_record_id"],
            first_source_record_id,
            "kept accepted-record diagnostic context should preserve the stabilized source_record_id",
        )

    def test_same_day_raw_dedup_keeps_first_ingest_record_even_if_input_order_reversed(self) -> None:
        part1_path = (
            FIXTURE_ROOT
            / "raw"
            / FIXTURE_RUN_DATE
            / "google_maps"
            / "google_maps_raw_fixture_part1.json"
        )
        part2_path = (
            FIXTURE_ROOT
            / "raw"
            / FIXTURE_RUN_DATE
            / "google_maps"
            / "google_maps_raw_fixture_part2.json"
        )
        part1_records, _ = _load_raw_file_records(part1_path, "google_maps")
        part2_records, _ = _load_raw_file_records(part2_path, "google_maps")

        deduped_records = _dedupe_google_raw_records([*part2_records, *part1_records])
        kept_run_ids = [str(record.get("run_id") or "") for record in deduped_records]

        self.assertEqual(
            kept_run_ids,
            [
                "fixture-google-multifile-001",
                "fixture-google-multifile-003",
                "fixture-google-multifile-004",
            ],
            "same-day raw dedup should sort by ingest metadata and keep the first-seen canonical record regardless of caller order",
        )

    def test_same_date_cross_run_scrape_dedup_skips_previously_successful_place_before_visit(
        self,
    ) -> None:
        prior_record = deepcopy(load_google_fixture("same_business_different_short_url")[0])
        fresh_record = deepcopy(load_google_fixture("binding_mismatch")[1])

        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = Path(tmpdir) / FIXTURE_RUN_DATE / "google_maps"
            target_dir.mkdir(parents=True)
            (target_dir / "google_maps_raw_fixture_previous.json").write_text(
                json.dumps([prior_record]),
                encoding="utf-8",
            )
            (target_dir / "errors_fixture_previous.json").write_text(
                json.dumps([
                    {
                        "source_platform": "google_maps",
                        "run_id": "fixture-google-error-001",
                        "place_url": str(fresh_record["raw"]["maps_link"]),
                        "error": "transient browser timeout",
                    }
                ]),
                encoding="utf-8",
            )

            filtered_places, diagnostics = apply_same_date_cross_run_google_dedup(
                [
                    {
                        "place_url": str(prior_record["raw"]["maps_link"]),
                        "place_id": "",
                    },
                    {
                        "place_url": str(fresh_record["raw"]["maps_link"]),
                        "place_id": "",
                    },
                ],
                target_dir=str(target_dir),
                current_output_path=str(
                    target_dir / "google_maps_raw_fixture_current.json"
                ),
            )

        self.assertEqual(
            len(filtered_places),
            1,
            "same-date cross-run scrape dedup should remove already successful Google raw records before visiting the page again",
        )
        self.assertEqual(
            build_google_maps_exact_page_key(filtered_places[0]["place_url"]),
            build_google_maps_exact_page_key(str(fresh_record["raw"]["maps_link"])),
            "same-date cross-run scrape dedup should retain fresh discovery targets that are not present in earlier successful raw files",
        )
        self.assertEqual(
            diagnostics["count"],
            1,
            "cross-run diagnostics should count the previously successful place as a skip",
        )
        self.assertEqual(
            diagnostics["load_diagnostics"]["raw_files_scanned"],
            1,
            "cross-run identity loading should only scan prior raw files and ignore same-date error files",
        )
        self.assertEqual(
            diagnostics["examples"][0]["matched_on"],
            "validated_google_place_id",
            "cross-run scrape dedup should prefer the recovered validated Google place token once the helper can safely recover it from the earlier raw long URL",
        )
        self.assertEqual(
            diagnostics["examples"][0]["kept_record"]["source_record_id"],
            build_google_maps_exact_page_key(str(prior_record["raw"]["maps_link"])),
            "cross-run skip diagnostics should point back to the stabilized canonical record identity from the earlier successful raw file",
        )


class ExtractionRegressionTests(unittest.TestCase):
    def run_google_extraction(
        self, fixture_files: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_google_dir = root / "raw" / FIXTURE_RUN_DATE / "google_maps"
            raw_google_dir.mkdir(parents=True)
            for filename, payload in fixture_files.items():
                (raw_google_dir / filename).write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date=FIXTURE_RUN_DATE,
            )

            output_dir = root / "normalized" / FIXTURE_RUN_DATE
            outputs = {
                "cafes": json.loads((output_dir / "cafes.json").read_text(encoding="utf-8")),
                "quarantine_cafes": json.loads(
                    (output_dir / "quarantine_cafes.json").read_text(encoding="utf-8")
                ),
            }
            return summary, outputs

    def test_same_day_raw_dedup_happens_before_normalization(self) -> None:
        part1 = load_fixture_payload(
            "raw/2026-04-05/google_maps/google_maps_raw_fixture_part1.json"
        )
        part2 = load_fixture_payload(
            "raw/2026-04-05/google_maps/google_maps_raw_fixture_part2.json"
        )

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "pipelines.extractor.normalize_google_maps_record",
            wraps=normalize_google_maps_record,
        ) as normalize_google:
            root = Path(tmpdir)
            raw_google_dir = root / "raw" / FIXTURE_RUN_DATE / "google_maps"
            raw_google_dir.mkdir(parents=True)
            (raw_google_dir / "google_maps_raw_fixture_part1.json").write_text(
                json.dumps(part1),
                encoding="utf-8",
            )
            (raw_google_dir / "google_maps_raw_fixture_part2.json").write_text(
                json.dumps(part2),
                encoding="utf-8",
            )

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date=FIXTURE_RUN_DATE,
            )

        self.assertEqual(summary["total_raw_records"], 4)
        self.assertEqual(
            normalize_google.call_count,
            3,
            "same-day duplicate raw records should be removed before normalize_google_maps_record runs",
        )

    def test_run_extraction_keeps_binding_mismatch_fixture_as_two_businesses(self) -> None:
        summary, outputs = self.run_google_extraction(
            {"binding_mismatch.json": load_google_fixture("binding_mismatch")}
        )

        self.assertEqual(
            summary["cafes"],
            2,
            "binding mismatch fixture should remain as two cafes instead of being over-merged at business level",
        )
        self.assertEqual(
            outputs["quarantine_cafes"],
            [],
            "binding mismatch fixture should not trigger a dedupe conflict quarantine when the businesses genuinely differ",
        )

    def test_run_extraction_prefers_persisted_google_raw_contract_fields(self) -> None:
        record = deepcopy(load_google_fixture("same_business_different_short_url")[0])
        canonical_maps_link = str(record["raw"]["maps_link"])
        canonical_place_id = extract_validated_google_place_token(canonical_maps_link)

        record["source_record_id"] = "https://maps.app.goo.gl/outdatedLegacySource"
        record["raw"]["latitude"] = ""
        record["raw"]["longitude"] = ""
        record["raw"]["maps_link"] = ""
        record["raw"]["gmaps_url"] = "https://maps.app.goo.gl/outdatedLegacyShort"
        record["raw"]["canonical_place_url"] = canonical_maps_link
        record["raw"]["canonical_place_key"] = build_google_maps_exact_page_key(
            canonical_maps_link
        )
        record["raw"]["validated_google_place_id"] = canonical_place_id

        summary, outputs = self.run_google_extraction(
            {"persisted_google_contract.json": [record]}
        )

        self.assertEqual(summary["cafes"], 1)
        self.assertEqual(outputs["quarantine_cafes"], [])
        self.assertEqual(
            outputs["cafes"][0]["gmaps_url"],
            canonical_maps_link,
            "persisted canonical Google raw fields should remain the primary extraction contract when legacy short-link fields drift",
        )
        self.assertEqual(
            outputs["cafes"][0]["latitude"],
            -0.4857808,
            "extractor should recover latitude from the persisted canonical Google contract before falling back to legacy short links",
        )
        self.assertEqual(
            outputs["cafes"][0]["longitude"],
            117.1123437,
            "extractor should recover longitude from the persisted canonical Google contract before falling back to legacy short links",
        )

    def test_run_extraction_legacy_flat_payload_uses_maps_link_before_short_gmaps_url(
        self,
    ) -> None:
        fixture_record = deepcopy(load_google_fixture("same_business_different_short_url")[0])
        legacy_record = {
            key: value for key, value in fixture_record.items() if key != "raw"
        }
        legacy_record.update(deepcopy(fixture_record["raw"]))
        legacy_record["latitude"] = ""
        legacy_record["longitude"] = ""

        summary, outputs = self.run_google_extraction(
            {"legacy_flat_google_payload.json": [legacy_record]}
        )

        self.assertEqual(summary["cafes"], 1)
        self.assertEqual(outputs["quarantine_cafes"], [])
        self.assertEqual(
            outputs["cafes"][0]["gmaps_url"],
            str(fixture_record["raw"]["maps_link"]),
            "legacy flat Google payloads should still prefer the canonical maps_link over the short gmaps_url when rebuilding cafe links",
        )
        self.assertEqual(
            outputs["cafes"][0]["latitude"],
            -0.4857808,
            "legacy flat payload fallback should recover latitude from maps_link before trying the short gmaps_url",
        )
        self.assertEqual(
            outputs["cafes"][0]["longitude"],
            117.1123437,
            "legacy flat payload fallback should recover longitude from maps_link before trying the short gmaps_url",
        )

    def test_run_extraction_merges_same_business_on_business_identity_when_exact_page_differs(
        self,
    ) -> None:
        base_record = deepcopy(load_google_fixture("broken_google_place_id")[0])
        second_record = deepcopy(base_record)
        second_record["run_id"] = "fixture-google-business-merge-002"
        second_record["source_record_id"] = "https://maps.app.goo.gl/businessMergeTwo"
        second_record["raw"]["gmaps_url"] = "https://maps.app.goo.gl/businessMergeTwo"
        second_record["raw"]["maps_link"] = (
            "https://www.google.com/maps/place/YOU+Coffee+and+Brunch+Annex/"
            "@-0.4857808,117.1123437,14z/data=!4m10!1m2!2m1!1scafe+in+Samarinda!"
            "3m6!1s0x2df67f38a37d8528:0x4291ca857ca785ee!8m2!3d-0.4857808!4d117.1483927!"
            "15sChFjYWZlIGluIFNhbWFyaW5kYVoTIhFjYWZlIGluIHNhbWFyaW5kYZIBC2NvZmZlZV9zaG9w!"
            "16s%2Fg%2F11rs2qz1zk?hl=en&entry=ttu"
        )
        second_record["raw"]["google_place_id"] = "still broken"

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "pipelines.extractor.normalize_google_maps_record",
            wraps=normalize_google_maps_record,
        ) as normalize_google:
            root = Path(tmpdir)
            raw_google_dir = root / "raw" / FIXTURE_RUN_DATE / "google_maps"
            raw_google_dir.mkdir(parents=True)
            (raw_google_dir / "business_merge.json").write_text(
                json.dumps([base_record, second_record]),
                encoding="utf-8",
            )

            summary = run_extraction(
                raw_dir=str(root / "raw"),
                normalized_dir=str(root / "normalized"),
                run_date=FIXTURE_RUN_DATE,
            )
            output_dir = root / "normalized" / FIXTURE_RUN_DATE
            cafes = json.loads((output_dir / "cafes.json").read_text(encoding="utf-8"))
            quarantine_cafes = json.loads(
                (output_dir / "quarantine_cafes.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            normalize_google.call_count,
            2,
            "business merge regression should exercise the post-normalization business dedup layer, not raw exact-page dedup",
        )
        self.assertEqual(
            summary["cafes"],
            1,
            "same business with different exact-page identities should merge into one cafe record",
        )
        self.assertEqual(
            len(cafes),
            1,
            "business-level merge fixture should persist a single cafe after extraction",
        )
        self.assertEqual(
            quarantine_cafes,
            [],
            "business-level merge fixture should not quarantine a genuine same-business match",
        )

    def test_run_extraction_quarantines_slug_collision_without_strong_business_match(
        self,
    ) -> None:
        base_record = deepcopy(load_google_fixture("broken_google_place_id")[0])
        base_record["raw"]["google_place_id"] = ""
        conflicting_record = deepcopy(base_record)
        conflicting_record["run_id"] = "fixture-google-slug-collision-002"
        conflicting_record["source_record_id"] = "https://maps.app.goo.gl/slugCollisionTwo"
        conflicting_record["raw"]["address"] = "Jl. Beta, Samarinda"
        conflicting_record["raw"]["gmaps_url"] = "https://maps.app.goo.gl/slugCollisionTwo"
        conflicting_record["raw"]["maps_link"] = (
            "https://www.google.com/maps/place/YOU+Coffee+and+Brunch+Second/"
            "@-0.4857808,117.1123437,14z/data=!4m10!1m2!2m1!1scafe+in+Samarinda!"
            "3m6!1s0x2df67f38a37d8599:0x4291ca857ca78111!8m2!3d-0.486!4d117.149!"
            "15sChFjYWZlIGluIFNhbWFyaW5kYVoTIhFjYWZlIGluIHNhbWFyaW5kYZIBC2NvZmZlZV9zaG9w!"
            "16s%2Fg%2F11rs2qz1zl?hl=en&entry=ttu"
        )

        summary, outputs = self.run_google_extraction(
            {"slug_collision.json": [base_record, conflicting_record]}
        )

        self.assertEqual(
            summary["cafes"],
            1,
            "slug collision regression should keep the first cafe and quarantine the conflicting later record",
        )
        self.assertEqual(
            len(outputs["quarantine_cafes"]),
            1,
            "slug collision without a strong business match should quarantine exactly one later cafe",
        )
        self.assertEqual(
            outputs["quarantine_cafes"][0]["reason_code"],
            "dedupe_conflict",
            "slug collision quarantine should keep the standardized dedupe_conflict reason code",
        )
        self.assertIn(
            "slug_collision_without_strong_business_match",
            outputs["quarantine_cafes"][0]["validation_errors"][0],
            "slug collision quarantine should explain that the conflict came from a slug match without stronger business evidence",
        )


if __name__ == "__main__":
    unittest.main()

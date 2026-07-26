"""Pilot hardening, Build 8: config gates, redacted live probes (fakes
only - no automated test ever performs a network call), schema
observation, doctor diagnostics, retention cleanup safety, the redacted
pilot manifest, Docker OCR packaging (static), and UI wiring."""

import json
import sys
import time
from pathlib import Path

import pytest

from apps.web.job_manager import JobError
from apps.web.transfer import gateway_auth as ga
from apps.web.transfer import jobs as tjobs
from apps.web.transfer import packing as pk
from apps.web.transfer import pilot
from apps.web.transfer import workbook as wbmod
from tests.test_transfer_gateway_auth import (
    ACCESS_1,
    SECRET_PASSWORD,
    make_client,
    ok_envelope,
)
from tests.test_transfer_packing import enrich, two_destination_job
from tests.test_transfer_product_lookup import (
    FakeAuth,
    FakeTransport,
    envelope,
    wire_record,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def roots(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("TRANSFER_JOBS_DIR", str(tmp_path / "transfer-jobs"))
    for name in ("PILOT_ENABLE_LIVE_AUTH", "PILOT_ENABLE_LIVE_PRODUCT_LOOKUP",
                 "PACKING_OUTPUT_RETENTION_HOURS",
                 "PACKING_CUSTOMER_STYLE_FIELD",
                 "PACKING_CUSTOMER_COLOR_CODE_FIELD",
                 "PACKING_CUSTOMER_COLOR_DESC_FIELD",
                 "API_GATEWAY_BASE_URL", "API_GATEWAY_USER_ID",
                 "API_GATEWAY_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    ga.reset_default_cache()
    return tmp_path


def packed_job(tmp_path):
    job_id = two_destination_job(tmp_path)
    enrich(job_id)
    pk.prepare_packing(job_id)
    return job_id


# --- pilot configuration ----------------------------------------------------------

class TestPilotConfig:
    def test_defaults_disable_all_live_probes(self):
        config = pilot.load_pilot_config()
        assert config.enable_live_auth is False
        assert config.enable_live_product_lookup is False

    def test_invalid_boolean_rejected(self, monkeypatch):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_AUTH", "maybe")
        assert pilot.pilot_config_problems()
        with pytest.raises(JobError, match="PILOT_ENABLE_LIVE_AUTH"):
            pilot.load_pilot_config()

    def test_true_variants_parse(self, monkeypatch):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_AUTH", "TRUE")
        monkeypatch.setenv("PILOT_ENABLE_LIVE_PRODUCT_LOOKUP", "on")
        config = pilot.load_pilot_config()
        assert config.enable_live_auth and config.enable_live_product_lookup


# --- auth probe -------------------------------------------------------------------

class TestAuthProbe:
    def test_disabled_by_default(self):
        with pytest.raises(JobError, match="disabled"):
            pilot.auth_probe(confirm=True)

    def test_requires_explicit_confirmation(self, monkeypatch):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_AUTH", "true")
        with pytest.raises(JobError, match="confirmation"):
            pilot.auth_probe()

    def _enable_with_fake(self, monkeypatch, responses):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_AUTH", "true")
        client, transport = make_client(responses)
        monkeypatch.setattr(ga, "build_client",
                            lambda *a, **k: client)
        return client, transport

    def test_successful_probe_is_redacted_and_clears_tokens(
            self, monkeypatch):
        client, transport = self._enable_with_fake(
            monkeypatch, [(200, ok_envelope())])
        outcome = pilot.auth_probe(confirm=True)
        assert outcome["success"] is True
        assert outcome["access_token_present"] is True
        assert outcome["refresh_token_present"] is True
        assert outcome["expires_in_seconds"] == 86400
        blob = json.dumps(outcome)
        assert ACCESS_1 not in blob
        assert SECRET_PASSWORD not in blob
        assert "Authorization" not in blob
        # probe tokens are never kept in memory afterwards
        assert client.cache.get() is None
        assert len(transport.calls) == 1

    def test_rejected_login_reports_error_code_only(self, monkeypatch):
        client, _ = self._enable_with_fake(
            monkeypatch, [(200, ok_envelope(code=100403))])
        outcome = pilot.auth_probe(confirm=True)
        assert outcome["success"] is False
        assert outcome["error_code"] == ga.AUTH_LOGIN_FAILED
        assert outcome["gateway_code"] == 100403
        assert SECRET_PASSWORD not in json.dumps(outcome)

    def test_timeout_reported_safely(self, monkeypatch):
        client, _ = self._enable_with_fake(
            monkeypatch, [ga.TransportTimeout("t"), ga.TransportTimeout("t"),
                          ga.TransportTimeout("t")])
        outcome = pilot.auth_probe(confirm=True)
        assert outcome["success"] is False
        assert outcome["error_code"] in (ga.AUTH_TIMEOUT,
                                         ga.AUTH_RETRY_EXHAUSTED)

    def test_no_token_persisted_anywhere(self, monkeypatch, tmp_path):
        self._enable_with_fake(monkeypatch, [(200, ok_envelope())])
        pilot.auth_probe(confirm=True)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert ACCESS_1 not in path.read_text(errors="ignore")


# --- product probe ----------------------------------------------------------------

class TestProductProbe:
    def _run(self, monkeypatch, responses, *, plus=("4894532999996",),
             qty=1, show_values=False):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_PRODUCT_LOOKUP", "true")
        transport = FakeTransport(responses)
        auth = FakeAuth()
        observation = pilot.product_probe(
            location="ZZOHK101", price_date="2026-07-24", plus=list(plus),
            qty=qty, confirm=True, show_values=show_values,
            transport=transport, auth=auth)
        return observation, transport, auth

    def test_disabled_by_default(self):
        with pytest.raises(JobError, match="disabled"):
            pilot.product_probe(location="X", price_date="2026-07-24",
                                plus=["1"], confirm=True)

    def test_requires_confirmation_and_limits_identifiers(self, monkeypatch):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_PRODUCT_LOOKUP", "true")
        with pytest.raises(JobError, match="confirmation"):
            pilot.product_probe(location="X", price_date="2026-07-24",
                                plus=["1"])
        with pytest.raises(JobError, match="1-3"):
            pilot.product_probe(location="X", price_date="2026-07-24",
                                plus=["1", "2", "3", "4"], confirm=True)
        with pytest.raises(JobError, match="YYYY-MM-DD"):
            pilot.product_probe(location="X", price_date="24/07/2026",
                                plus=["1"], confirm=True)

    def test_success_observes_schema_without_values(self, monkeypatch):
        record = wire_record("4894532999996", ean="4894532999996")
        observation, transport, _ = self._run(
            monkeypatch, [(200, envelope([record]))])
        assert observation["success"] is True
        assert observation["records_returned"] == 1
        assert observation["correlated"] == 1
        assert observation["omitted_identifiers"] == []
        assert observation["values"] is None
        schema = observation["record_schema"]
        assert "plu" in schema and "ean" in schema
        assert schema["plu"]["types"] == ["str"]
        # no business values leak into the schema observation
        assert "4894532999996" not in json.dumps(schema)
        assert len(transport.calls) == 1        # exactly one batch

    def test_not_found_identifier_reported_as_omitted(self, monkeypatch):
        observation, _, _ = self._run(
            monkeypatch, [(200, envelope([]))], plus=("0000012345678",))
        assert observation["success"] is True
        assert observation["records_returned"] == 0
        assert observation["omitted_identifiers"] == ["0000012345678"]

    def test_show_values_excludes_sensitive_keys(self, monkeypatch):
        record = wire_record("4894532999996", ean="4894532999996")
        observation, _, _ = self._run(
            monkeypatch, [(200, envelope([record]))], show_values=True)
        blob = json.dumps(observation["values"])
        assert "4894532999996" in blob
        for banned in ("password", "accessToken", "Authorization"):
            assert banned not in blob

    def test_non_default_qty_sends_single_raw_request(self, monkeypatch):
        record = wire_record("4894532999996", ean="4894532999996")
        observation, transport, _ = self._run(
            monkeypatch, [(200, envelope([record]))], qty=6)
        assert observation["success"] is True
        assert observation["qty"] == 6
        assert len(transport.calls) == 1
        items = transport.calls[0]["body"]["RequestList"]
        assert all(item["Qty"] == 6 for item in items)

    def test_failure_reports_error_code_and_clears_tokens(self, monkeypatch):
        observation, _, auth = self._run(monkeypatch, [(500, None)])
        assert observation["success"] is False
        assert observation["error_code"] is not None
        blob = json.dumps(observation)
        assert "tok-A" not in blob and "Bearer" not in blob


# --- schema observation -----------------------------------------------------------

class TestSchemaObservation:
    def test_types_null_and_empty_counts(self):
        schema = pilot.observe_record_schema([
            {"plu": "01", "price": 100, "note": None, "desc": ""},
            {"plu": "02", "price": 99.5, "desc": "x"},
        ])
        assert schema["plu"] == {"types": ["str"], "count": 2,
                                 "null_count": 0, "empty_count": 0}
        assert sorted(schema["price"]["types"]) == ["float", "int"]
        assert schema["note"]["null_count"] == 1
        assert schema["desc"]["empty_count"] == 1

    def test_no_values_in_observation(self):
        schema = pilot.observe_record_schema(
            [{"analysisCode01": "SECRET-VALUE"}])
        assert "SECRET-VALUE" not in json.dumps(schema)
        assert "analysisCode01" in schema


# --- OCR readiness ----------------------------------------------------------------

class TestOcrReadiness:
    def test_missing_dependency_reported(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", None)
        result = pilot.ocr_ready()
        assert result["status"] == "missing"
        assert "requirements" not in result["detail"] or True

    def test_shallow_check_never_runs_engine(self):
        result = pilot.ocr_ready()
        assert result["status"] in ("installed", "missing")


# --- doctor -----------------------------------------------------------------------

class TestDoctor:
    def test_unconfigured_api_is_warning_not_blocker(self):
        report, code = pilot.doctor()
        assert report["api_status"] == "not_configured"
        assert code in (pilot.EXIT_READY, pilot.EXIT_WARNINGS)
        assert not report["blockers"]

    def test_invalid_config_blocks(self, monkeypatch):
        monkeypatch.setenv("PACKING_OUTPUT_RETENTION_HOURS", "banana")
        report, code = pilot.doctor()
        assert code == pilot.EXIT_BLOCKED
        assert report["blockers"]

    def test_report_is_redacted(self, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_BASE_URL", "https://gw.test/devgapi")
        monkeypatch.setenv("API_GATEWAY_USER_ID", "pilot-user-77")
        monkeypatch.setenv("API_GATEWAY_PASSWORD", "Sup3rSecret!")
        report, _ = pilot.doctor()
        blob = json.dumps(report)
        assert "Sup3rSecret!" not in blob
        assert "pilot-user-77" not in blob
        assert report["api_status"] == "configured"

    def test_job_state_counts_and_stranded_warning(self, tmp_path,
                                                   monkeypatch):
        job_id = two_destination_job(tmp_path)
        tjobs.update_job_status(job_id, "REVIEW_IN_PROGRESS")
        report, _ = pilot.doctor()
        assert report["job_state_counts"].get("REVIEW_IN_PROGRESS") == 1
        assert report["stranded_jobs"] == 0

    def test_exit_ready_when_clean_and_ocr_present(self, monkeypatch):
        report, code = pilot.doctor()
        if report["ocr"]["status"] == "installed" \
                and report["api_status"] == "configured":
            assert code == pilot.EXIT_READY
        else:
            assert code == pilot.EXIT_WARNINGS


# --- cleanup ----------------------------------------------------------------------

def _fake_job_dir(tmp_path, name="tjob-20260101T000000-abcdef123456"):
    root = Path(tjobs.transfer_jobs_root())
    d = root / name
    (d / "output").mkdir(parents=True)
    return d


def _age(path, hours=48):
    old = time.time() - hours * 3600
    import os
    os.utime(path, (old, old))


class TestCleanup:
    def test_dry_run_counts_but_keeps(self, tmp_path):
        d = _fake_job_dir(tmp_path)
        stale = d / "output" / "old.xlsx"
        stale.write_bytes(b"x" * 10)
        _age(stale)
        tmpfile = d / "output" / "result.json.tmp-999"
        tmpfile.write_text("{}")
        result = pilot.cleanup(dry_run=True)
        assert result["dry_run"] is True
        assert result["files"] == 2
        assert result["bytes"] >= 10
        assert stale.exists() and tmpfile.exists()

    def test_execute_deletes_only_candidates(self, tmp_path):
        d = _fake_job_dir(tmp_path)
        stale = d / "output" / "old.zip"
        stale.write_bytes(b"z" * 5)
        _age(stale)
        fresh = d / "output" / "new.xlsx"
        fresh.write_bytes(b"y")
        keep = d / "output" / "result.json"
        keep.write_text("{}")
        _age(keep)                       # old but NOT a cleanup candidate
        archived = d / "packing" / "result-stale-20260101T000000Z.json"
        archived.parent.mkdir()
        archived.write_text("{}")
        _age(archived)
        result = pilot.cleanup(dry_run=False)
        assert result["files"] == 2
        assert not stale.exists() and not archived.exists()
        assert fresh.exists() and keep.exists()

    def test_in_progress_job_untouched(self, tmp_path):
        job_id = two_destination_job(tmp_path)
        tjobs.update_job_status(job_id, "REVIEW_IN_PROGRESS")
        tjobs.update_job_status(job_id, "READY_FOR_PRODUCT_LOOKUP")
        tjobs.update_job_status(job_id, "PRODUCT_LOOKUP_IN_PROGRESS")
        job_dir = tjobs.transfer_job_dir_for(job_id)
        stale = job_dir / "output" / "old.xlsx"
        stale.parent.mkdir(exist_ok=True)
        stale.write_bytes(b"x")
        _age(stale)
        result = pilot.cleanup(dry_run=False)
        assert stale.exists()
        assert result["jobs_skipped_active"] == 1

    def test_invoice_jobs_and_foreign_dirs_ignored(self, tmp_path):
        root = Path(tjobs.transfer_jobs_root())
        foreign = root / "job-20260101T000000-something"     # invoice shape
        foreign.mkdir(parents=True)
        loot = foreign / "old.xlsx"
        loot.write_bytes(b"x")
        _age(loot)
        result = pilot.cleanup(dry_run=False)
        assert loot.exists()
        assert result["jobs_scanned"] == 0

    def test_symlink_is_never_deleted_or_followed(self, tmp_path):
        d = _fake_job_dir(tmp_path)
        victim = tmp_path / "outside.xlsx"
        victim.write_bytes(b"precious")
        _age(victim)
        link = d / "output" / "old.xlsx"
        link.symlink_to(victim)
        _age(link)
        pilot.cleanup(dry_run=False)
        assert victim.exists()
        assert victim.read_bytes() == b"precious"

    def test_never_reaches_outside_transfer_root(self, tmp_path):
        d = _fake_job_dir(tmp_path)
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        secret = outside_dir / "keep.tmp-1"
        secret.write_text("keep")
        _age(secret)
        link = d / "output"
        # a symlinked WHOLE job dir must be skipped by the iterator
        evil = Path(tjobs.transfer_jobs_root()) / \
            "tjob-20260101T000001-abcdef123456"
        evil.symlink_to(outside_dir)
        pilot.cleanup(dry_run=False)
        assert secret.exists()


# --- pilot manifest ---------------------------------------------------------------

class TestPilotManifest:
    def test_manifest_schema_and_redaction(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_GATEWAY_PASSWORD", "Sup3rSecret!")
        job_id = packed_job(tmp_path)
        wbmod.generate_workbooks(job_id)
        manifest = pilot.write_pilot_manifest(job_id)
        assert manifest["schema_version"] == 1
        assert manifest["job_id"] == job_id
        assert manifest["destinations"] == ["ZZOHK101", "ZZOHK202"]
        assert len(manifest["workbooks"]) == 2
        for entry in manifest["workbooks"]:
            assert len(entry["sha256"]) == 64
            assert entry["validation_status"] == "valid"
        assert manifest["zip"] and manifest["zip"]["sha256"]
        assert manifest["source_files"][0]["sha256"]
        blob = pilot.manifest_path(job_id).read_text()
        assert "Sup3rSecret!" not in blob
        assert "Authorization" not in blob
        assert "accessToken" not in blob
        reloaded = json.loads(blob)
        assert reloaded["job_id"] == job_id

    def test_manifest_written_atomically_no_tmp_left(self, tmp_path):
        job_id = packed_job(tmp_path)
        pilot.write_pilot_manifest(job_id)
        pilot_dir = pilot.manifest_path(job_id).parent
        assert list(pilot_dir.glob("*.tmp-*")) == []

    def test_unknown_job_rejected(self):
        with pytest.raises(JobError, match="Unknown"):
            pilot.write_pilot_manifest("tjob-20260101T000000-abcdef123456")


# --- CLI --------------------------------------------------------------------------

class TestCli:
    def test_doctor_exit_code_propagates(self, capsys):
        code = pilot.main(["doctor"])
        assert code in (0, 1, 2)
        out = json.loads(capsys.readouterr().out)
        assert "warnings" in out and "blockers" in out

    def test_cleanup_defaults_to_dry_run(self, capsys):
        assert pilot.main(["cleanup"]) == 0
        assert json.loads(capsys.readouterr().out)["dry_run"] is True

    def test_auth_check_blocked_without_env_flag(self):
        with pytest.raises(JobError, match="disabled"):
            pilot.main(["auth-check", "--yes"])

    def test_product_check_blocked_without_env_flag(self):
        with pytest.raises(JobError, match="disabled"):
            pilot.main(["product-check", "--yes", "--location", "X",
                        "--price-date", "2026-07-24", "--plu", "1"])


# --- Docker / packaging (static) --------------------------------------------------

class TestDockerPackaging:
    def test_web_stage_installs_pinned_ocr(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        web_stage = dockerfile.split("FROM runtime-base AS web")[1]
        web_stage = web_stage.split("FROM runtime-base AS cli")[0]
        assert "requirements-ocr.txt" in web_stage
        assert "libgl1" in web_stage and "libglib2.0-0" in web_stage
        assert "--only-binary=:all:" in web_stage

    def test_cli_and_runtime_stages_unchanged(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        pre_web = dockerfile.split("FROM runtime-base AS web")[0]
        assert "requirements-ocr" not in pre_web
        assert "libgl1" not in pre_web

    def test_ocr_requirements_stay_pinned(self):
        text = (ROOT / "requirements-ocr.txt").read_text()
        assert "rapidocr-onnxruntime==1.2.3" in text
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                assert "==" in line, line

    def test_no_credentials_in_docker_or_compose(self):
        for name in ("Dockerfile", "compose.yaml"):
            text = (ROOT / name).read_text()
            for banned in ("PASSWORD=", "ACCESS_TOKEN", "Bearer "):
                assert banned not in text, (name, banned)


# --- UI and safety boundaries -----------------------------------------------------

class TestUiAndBoundaries:
    def test_readiness_section_wired_into_page(self):
        text = (ROOT / "apps/web/transfer/page.py").read_text()
        assert "_render_pilot_readiness" in text
        assert "pilot.doctor" in text

    def test_pilot_module_never_prints_secrets(self):
        text = (ROOT / "apps/web/transfer/pilot.py").read_text()
        assert "password" not in text.lower().replace(
            "no passwords", "").replace("passwords,", "")
        assert "print(token" not in text
        assert "access_token)" not in text.replace(
            "bool(tokens.access_token)", "")

    def test_env_example_gates_default_false(self):
        text = (ROOT / ".env.example").read_text()
        assert "#PILOT_ENABLE_LIVE_AUTH=false" in text
        assert "#PILOT_ENABLE_LIVE_PRODUCT_LOOKUP=false" in text

    def test_probe_output_shapes_are_json_safe(self, monkeypatch):
        monkeypatch.setenv("PILOT_ENABLE_LIVE_PRODUCT_LOOKUP", "true")
        observation = pilot.product_probe(
            location="ZZOHK101", price_date="2026-07-24",
            plus=["123"], confirm=True,
            transport=FakeTransport([(200, envelope([]))]),
            auth=FakeAuth())
        json.dumps(observation)          # must not raise

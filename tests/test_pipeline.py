from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from muvyosubscriberouter.client import MuvyoClient, MuvyoClientError
from muvyosubscriberouter.completion import CompletionDecision
from muvyosubscriberouter.pipeline import (
    PipelineResult,
    RouterPipelineConfig,
    inspect_staging_root_warnings,
    is_path_strictly_within_root,
    resolve_post_process_strm_targets,
    run_muvyo_routing_pipeline,
    validate_router_paths,
)


class _FakePipelineClient:
    def __init__(
        self,
        *,
        api_key: str = "",
        has_results: bool = True,
        staged_items: List[Dict[str, Any]] | None = None,
        browse_cloud_side_effects: List[Any] | None = None,
        organize_ok: bool = True,
        organize_blockers: List[str] | None = None,
        resolved_target_dirs: List[str] | None = None,
        resolved_strm_dirs: List[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.has_results = has_results
        self.organize_ok = organize_ok
        self.organize_blockers = list(organize_blockers or [])
        self.resolved_target_dirs = (
            list(resolved_target_dirs)
            if resolved_target_dirs is not None
            else ["/已整理/series/演示剧集/Season 01"]
        )
        self.resolved_strm_dirs = list(resolved_strm_dirs) if resolved_strm_dirs is not None else []
        self.staged_items = staged_items if staged_items is not None else [
            {"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"},
            {"fn": "Demo.S01E02.mkv", "fid": "v2", "pickcode": "pc_secret_2"},
        ]
        self.browse_cloud_side_effects = (
            list(browse_cloud_side_effects) if browse_cloud_side_effects is not None else None
        )
        self._browse_cloud_index = 0
        self.calls: List[str] = []
        self.ensured_paths: List[str] = []
        self.received_paths: List[str] = []
        self.organize_kwargs: Dict[str, Any] = {}
        self.strm_kwargs: Dict[str, Any] = {}

    def search_resources(self, keyword: str, channels=None) -> Dict[str, Any]:
        self.calls.append("search")
        if not self.has_results:
            return {"ok": True, "items": [], "result_count": 0}
        return {"ok": True, "items": [{"title": "演示剧集 S01 1080p", "share_code": "sc_raw_1"}]}

    def parse_share(self, share_url: str, receive_code: str = "") -> Dict[str, Any]:
        self.calls.append("parse")
        return {"ok": True, "share_code": "sc_raw_1", "receive_code": receive_code}

    def browse_share(self, share_code: str, receive_code: str = "", cid: str = "", limit: int = 1150) -> Dict[str, Any]:
        self.calls.append("browse_share")
        return {
            "ok": True,
            "items": [
                {"name": "Demo.S01E01.mkv", "file_id": "sf1"},
                {"name": "Demo.S01E02.mkv", "file_id": "sf2"},
                {"name": "poster.jpg", "file_id": "meta1"},
            ],
        }

    def ensure_115_folder(self, target_path: str) -> Dict[str, Any]:
        self.calls.append("ensure_folder")
        self.ensured_paths.append(target_path)
        return {"ok": True, "target_path": target_path, "final_folder_id": "staging_fid_9"}

    def receive_share(self, share_code: str, file_ids, target_path: str, receive_code: str = "") -> Dict[str, Any]:
        self.calls.append("receive")
        self.received_paths.append(target_path)
        return {"ok": True, "file_id_count": len(file_ids), "target_path": target_path}

    def browse_cloud_folder(self, folder_id: str = "", path: str = "", limit: int = 1150) -> Dict[str, Any]:
        self.calls.append("browse_cloud")
        if self.browse_cloud_side_effects is not None:
            idx = min(self._browse_cloud_index, len(self.browse_cloud_side_effects) - 1)
            self._browse_cloud_index += 1
            effect = self.browse_cloud_side_effects[idx]
            if isinstance(effect, Exception):
                raise effect
            return dict(effect)
        return {"ok": True, "folder_id": folder_id, "items": list(self.staged_items)}

    def build_redirect_probe_url(self, cloud_path: str, pickcode: str) -> str:
        return f"http://mv3.example.local:7811/redirect?path={cloud_path}&pickcode={pickcode}"

    def organize_transfer(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append("organize_transfer")
        self.organize_kwargs = kwargs
        return {
            "ok": self.organize_ok,
            "status": 200,
            "blockers": self.organize_blockers,
            "resolved_target_dirs": self.resolved_target_dirs,
            "resolved_strm_dirs": self.resolved_strm_dirs,
            "response": {"moved": len(kwargs.get("files", []))},
        }

    def generate_strm(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append("generate_strm")
        self.strm_kwargs = kwargs
        return {"ok": True, "status": 200, "response": {"generated": 2}}


class TestPipeline(unittest.TestCase):
    def test_path_guards_reject_unsafe_roots(self) -> None:
        self.assertEqual(validate_router_paths("/未整理/mp-router", "/已整理", "/strm"), [])
        self.assertIn(
            "cloud_organize_root_must_not_append_category",
            validate_router_paths("/未整理/mp-router", "/已整理/series", "/strm"),
        )
        self.assertIn(
            "mv3_strm_root_must_be_explicit_strm_path",
            validate_router_paths("/未整理/mp-router", "/已整理", "/series"),
        )
        self.assertIn(
            "cloud_organize_root_must_not_be_strm_side",
            validate_router_paths("/未整理/mp-router", "/strm", "/strm"),
        )
        self.assertIn(
            "mv3_strm_root_must_not_be_cloud_media_path",
            validate_router_paths("/未整理/mp-router", "/已整理", "/已整理"),
        )
        self.assertIn(
            "cloud_organize_root_must_not_contain_dot_segments",
            validate_router_paths("/未整理/mp-router", "/已整理/../etc", "/strm"),
        )
        self.assertIn(
            "mv3_strm_root_must_not_contain_dot_segments",
            validate_router_paths("/未整理/mp-router", "/已整理", "/strm/./sub"),
        )

    def test_happy_path_routes_and_organizes(self) -> None:
        client = _FakePipelineClient()
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(trigger_strm_generate=True)

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(client, decision, tmdb_id=3001, config=cfg, run_id="r1", probe_runner=_probe)  # type: ignore[arg-type]
        self.assertEqual(res.status, "routed_ok")
        self.assertIn("organize_transfer", client.calls)
        self.assertIn("generate_strm", client.calls)
        self.assertEqual(client.organize_kwargs["target_dir"], "/已整理")
        self.assertEqual(client.organize_kwargs["strm_dir"], "/strm")
        self.assertEqual(client.organize_kwargs["files"][0]["name"], "Demo.S01E01.mkv")
        self.assertEqual(client.strm_kwargs["source_dir"], "/已整理/series/演示剧集/Season 01")
        self.assertNotIn("pc_secret_1", str(res.to_sanitized_dict()))

    def test_strm_generate_skipped_when_no_scoped_target_dir(self) -> None:
        client = _FakePipelineClient(resolved_target_dirs=[])
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(trigger_strm_generate=True)

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(client, decision, tmdb_id=3004, config=cfg, run_id="r4", probe_runner=_probe)  # type: ignore[arg-type]
        self.assertEqual(res.status, "routed_ok")
        self.assertNotIn("generate_strm", client.calls)
        self.assertIn("strm_generate_skipped_missing_scoped_target_dir", res.diagnostics.get("warnings", []))

    def test_organize_skipped_blocks_pipeline(self) -> None:
        client = _FakePipelineClient(organize_ok=False, organize_blockers=["organize_skipped_items"])
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "zho", "title": "国语"}],
                "subtitle_streams": [],
            }

        res = run_muvyo_routing_pipeline(client, decision, tmdb_id=3005, config=RouterPipelineConfig(), run_id="r5", probe_runner=_probe)  # type: ignore[arg-type]
        self.assertEqual(res.status, "manual_review_staging")
        self.assertIn("organize_skipped_items", res.reason)

    def test_subtitle_gate_failure_stops_in_staging_without_organize_or_delete(self) -> None:
        client = _FakePipelineClient()
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(subtitle_policy="auto")

        def _probe_no_zh(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "eng", "title": "English"}],
            }

        res = run_muvyo_routing_pipeline(client, decision, tmdb_id=3002, config=cfg, run_id="r2", probe_runner=_probe_no_zh)  # type: ignore[arg-type]
        self.assertEqual(res.status, "manual_review_staging")
        self.assertIn("embedded_chinese_subtitle_missing", res.reason)
        self.assertNotIn("organize_transfer", client.calls)
        self.assertNotIn("generate_strm", client.calls)
        self.assertTrue(res.staging_path.startswith("/mv_staging/"))

    def test_no_candidates_falls_back_to_pt(self) -> None:
        client = _FakePipelineClient(has_results=False)
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="冷门剧"
        )
        res = run_muvyo_routing_pipeline(client, decision, tmdb_id=3003, config=RouterPipelineConfig())  # type: ignore[arg-type]
        self.assertEqual(res.status, "fallback_pt")
        self.assertEqual(res.reason, "no_suitable_share_candidate")

    def test_ensure_115_folder_stop_before_post_aborts_pipeline(self) -> None:
        requests: List[tuple] = []
        stopped = False

        def _transport(method: str, url: str, headers: Dict[str, str], body, timeout: int):
            nonlocal stopped
            requests.append((method, url))
            if "/api/v1/resource-search/search" in url:
                return 200, {}, json.dumps({
                    "success": True,
                    "data": {"items": [{"title": "演示剧集 S01 1080p", "share_code": "sc_raw_1"}]},
                }).encode()
            if "/api/v1/share-transfer/browse" in url:
                return 200, {}, json.dumps({
                    "success": True,
                    "data": {
                        "list": [
                            {"n": "Demo.S01E01.mkv", "fid": "sf1"},
                            {"n": "Demo.S01E02.mkv", "fid": "sf2"},
                        ]
                    },
                }).encode()
            if "/api/v1/files/115/browse" in url:
                if "cid=0" in url:
                    return 200, {}, json.dumps({
                        "success": True,
                        "data": [{"fn": "mv_staging", "fid": "dir_staging"}],
                    }).encode()
                # 第二级隔离目录不存在；在 _find_child_folder 返回空后、POST 建目录前变为 stopped
                stopped = True
                return 200, {}, json.dumps({"success": True, "data": []}).encode()
            return 200, {}, b'{"success": true, "data": {}}'

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport)
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        res = run_muvyo_routing_pipeline(
            client,
            decision,
            tmdb_id=3006,
            config=RouterPipelineConfig(),
            run_id="r6",
            should_stop=lambda: stopped,
        )
        self.assertEqual(res.status, "aborted_stopped")
        self.assertEqual(res.reason, "plugin_stopped_during_ensure_staging")
        self.assertFalse(any(method == "POST" and "/api/v1/files/115/folder" in url for method, url in requests))
        self.assertFalse(any("/api/v1/share-transfer/receive" in url for _, url in requests))

    def test_staging_path_redacts_sensitive_title_segments(self) -> None:
        client = _FakePipelineClient()
        decision = CompletionDecision(
            eligible=True,
            reason="tv_series_ended",
            media_kind="tv",
            season=1,
            expected_episodes=2,
            title='Cookie: sid="RAW_COOKIE_VAL"; uid=RAW_UID_VAL Bearer RAW_BEARER_VAL share.example.local/s/RAW_SHARE_CODE',
            original_title="演示剧集",
        )

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3007,
            config=RouterPipelineConfig(),
            run_id="r7",
            probe_runner=_probe,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertIn("ensure_folder", client.calls)
        self.assertEqual(len(client.ensured_paths), 1)

        sanitized = res.to_sanitized_dict()
        candidates = [
            res.staging_path,
            sanitized["staging_path"],
            client.ensured_paths[0],
        ]
        for path in candidates:
            self.assertTrue(path.startswith("/mv_staging/"), path)
            self.assertNotIn("REDACTED", path)
            for secret in (
                "RAW_COOKIE_VAL",
                "RAW_UID_VAL",
                "RAW_BEARER_VAL",
                "RAW_SHARE_CODE",
            ):
                self.assertNotIn(secret, path)

    def test_staging_path_and_result_redact_quoted_tokens_and_client_api_key(self) -> None:
        client = _FakePipelineClient(api_key="RAW_MV_API_KEY_999")
        decision = CompletionDecision(
            eligible=True,
            reason="tv_series_ended",
            media_kind="tv",
            season=1,
            expected_episodes=2,
            title='演示剧集 token="RAW_QUOTED_TOK" RAW_MV_API_KEY_999',
            original_title="演示剧集",
        )

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3008,
            config=RouterPipelineConfig(),
            run_id="r8",
            probe_runner=_probe,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(len(client.ensured_paths), 1)
        self.assertEqual(len(client.received_paths), 1)

        sanitized = res.to_sanitized_dict()
        sanitized_json = json.dumps(sanitized, ensure_ascii=False)
        for target in (
            client.ensured_paths[0],
            client.received_paths[0],
            res.staging_path,
            sanitized["staging_path"],
            sanitized_json,
        ):
            self.assertNotIn("RAW_QUOTED_TOK", target)
            self.assertNotIn("RAW_MV_API_KEY_999", target)

        for path in (
            client.ensured_paths[0],
            client.received_paths[0],
            res.staging_path,
            sanitized["staging_path"],
        ):
            self.assertTrue(path.startswith("/mv_staging/"), path)
            self.assertNotIn("REDACTED", path)

        explicit_res = PipelineResult(
            status="fallback_pt",
            reason="err RAW_MV_API_KEY_999",
            staging_path="/mv_staging/RAW_MV_API_KEY_999",
        ).to_sanitized_dict(extra_secrets=["RAW_MV_API_KEY_999"])
        self.assertNotIn("RAW_MV_API_KEY_999", json.dumps(explicit_res, ensure_ascii=False))

    def test_staging_settle_waits_for_delayed_115_items(self) -> None:
        client = _FakePipelineClient(
            browse_cloud_side_effects=[
                {"ok": True, "folder_id": "staging_fid_9", "items": []},
                {
                    "ok": True,
                    "folder_id": "staging_fid_9",
                    "items": [{"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"}],
                },
                {
                    "ok": True,
                    "folder_id": "staging_fid_9",
                    "items": [
                        {"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"},
                        {"fn": "Demo.S01E02.mkv", "fid": "v2", "pickcode": "pc_secret_2"},
                    ],
                },
            ]
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=6, staging_settle_interval_seconds=2.0)
        sleep_calls: List[float] = []

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3010,
            config=cfg,
            run_id="r10",
            probe_runner=_probe,
            sleep_fn=sleep_calls.append,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(client.calls.count("browse_cloud"), 3)
        self.assertEqual(sleep_calls, [2.0, 2.0])
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 3)
        self.assertIn("organize_transfer", client.calls)

    def test_staging_settle_exits_immediately_when_complete_on_first_browse(self) -> None:
        client = _FakePipelineClient()
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=6, staging_settle_interval_seconds=2.0)
        sleep_calls: List[float] = []

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3011,
            config=cfg,
            run_id="r11",
            probe_runner=_probe,
            sleep_fn=sleep_calls.append,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(client.calls.count("browse_cloud"), 1)
        self.assertEqual(sleep_calls, [])
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 1)

    def test_staging_settle_exhausted_returns_incomplete_without_organize(self) -> None:
        client = _FakePipelineClient(
            staged_items=[{"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"}]
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=4, staging_settle_interval_seconds=1.5)
        sleep_calls: List[float] = []

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3012,
            config=cfg,
            run_id="r12",
            sleep_fn=sleep_calls.append,
        )
        self.assertEqual(res.status, "manual_review_staging")
        self.assertEqual(res.reason, "staging_episode_coverage_incomplete")
        self.assertEqual(client.calls.count("browse_cloud"), 4)
        self.assertEqual(sleep_calls, [1.5, 1.5, 1.5])
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 4)
        self.assertNotIn("organize_transfer", client.calls)

    def test_staging_settle_aborts_when_stopped_during_wait(self) -> None:
        client = _FakePipelineClient(
            staged_items=[{"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"}]
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=5, staging_settle_interval_seconds=2.0)
        stopped = False
        sleep_calls: List[float] = []

        def _sleep_and_stop(seconds: float) -> None:
            nonlocal stopped
            sleep_calls.append(seconds)
            stopped = True

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3013,
            config=cfg,
            run_id="r13",
            should_stop=lambda: stopped,
            sleep_fn=_sleep_and_stop,
        )
        self.assertEqual(res.status, "manual_review_staging")
        self.assertEqual(res.reason, "plugin_stopped_during_staging_settle")
        self.assertEqual(client.calls.count("browse_cloud"), 1)
        self.assertEqual(sleep_calls, [2.0])
        self.assertEqual(res.diagnostics.get("stage"), "staging_settle")
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 1)
        self.assertNotIn("organize_transfer", client.calls)

    def test_staging_settle_recovers_from_transient_browse_error(self) -> None:
        client = _FakePipelineClient(
            browse_cloud_side_effects=[
                MuvyoClientError("temporary_timeout"),
                {
                    "ok": True,
                    "folder_id": "staging_fid_9",
                    "items": [
                        {"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"},
                        {"fn": "Demo.S01E02.mkv", "fid": "v2", "pickcode": "pc_secret_2"},
                    ],
                },
            ]
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=4, staging_settle_interval_seconds=1.0)
        sleep_calls: List[float] = []

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3014,
            config=cfg,
            run_id="r14",
            probe_runner=_probe,
            sleep_fn=sleep_calls.append,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(client.calls.count("browse_cloud"), 2)
        self.assertEqual(sleep_calls, [1.0])
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 2)
        self.assertIn("organize_transfer", client.calls)

    def test_staging_settle_stop_after_complete_browse_skips_subtitle_gate(self) -> None:
        client = _FakePipelineClient(
            browse_cloud_side_effects=[
                {
                    "ok": True,
                    "folder_id": "staging_fid_9",
                    "items": [{"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"}],
                },
                {
                    "ok": True,
                    "folder_id": "staging_fid_9",
                    "items": [
                        {"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"},
                        {"fn": "Demo.S01E02.mkv", "fid": "v2", "pickcode": "pc_secret_2"},
                    ],
                },
            ]
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=4, staging_settle_interval_seconds=1.0)
        sleep_calls: List[float] = []
        probe_calls = 0

        def _probe(tool: str, url: str, timeout: int):
            nonlocal probe_calls
            probe_calls += 1
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English"}],
                "subtitle_streams": [{"language": "zh-cn", "title": "简体中文"}],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3015,
            config=cfg,
            run_id="r15",
            probe_runner=_probe,
            should_stop=lambda: client.calls.count("browse_cloud") >= 2,
            sleep_fn=sleep_calls.append,
        )
        self.assertEqual(res.status, "manual_review_staging")
        self.assertEqual(res.reason, "plugin_stopped_during_staging_settle")
        self.assertEqual(probe_calls, 0)
        self.assertNotIn("organize_transfer", client.calls)
        self.assertEqual(client.calls.count("browse_cloud"), 2)
        self.assertEqual(sleep_calls, [1.0])
        self.assertEqual(res.video_count, 2)
        self.assertEqual(res.episodes, [1, 2])
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 2)

    def test_staging_settle_not_ok_response_preserves_previous_snapshot(self) -> None:
        client = _FakePipelineClient(
            browse_cloud_side_effects=[
                {
                    "ok": True,
                    "folder_id": "staging_fid_9",
                    "items": [{"fn": "Demo.S01E01.mkv", "fid": "v1", "pickcode": "pc_secret_1"}],
                },
                {"ok": False, "status": 503, "items": []},
                {"ok": False, "status": 503, "items": []},
            ]
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(staging_settle_max_attempts=3, staging_settle_interval_seconds=1.0)
        sleep_calls: List[float] = []

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=3016,
            config=cfg,
            run_id="r16",
            sleep_fn=sleep_calls.append,
        )
        self.assertEqual(res.status, "manual_review_staging")
        self.assertEqual(res.reason, "staging_episode_coverage_incomplete")
        self.assertEqual(res.video_count, 1)
        self.assertEqual(res.episodes, [1])
        self.assertEqual(client.calls.count("browse_cloud"), 3)
        self.assertEqual(sleep_calls, [1.0, 1.0])
        self.assertEqual(res.diagnostics.get("staging_browse_attempts"), 3)
        self.assertEqual(res.diagnostics.get("last_browse_not_ok"), 503)
        self.assertNotIn("organize_transfer", client.calls)

    def test_validate_optional_local_and_emby_strm_roots(self) -> None:
        self.assertEqual(
            validate_router_paths("/mv_staging", "/已整理", "/strm", "/example-strm", "/example-emby-strm"),
            [],
        )
        self.assertEqual(
            validate_router_paths("/mv_staging", "/已整理", "/strm", "", ""),
            [],
        )
        errs = validate_router_paths(
            "/mv_staging",
            "/已整理",
            "/strm",
            local_strm_root="/已整理/series",
            emby_strm_root="/example-emby-strm/../bad/series",
        )
        self.assertIn("local_strm_root_must_not_be_cloud_media_path", errs)
        self.assertIn("local_strm_root_must_be_explicit_strm_path", errs)
        self.assertIn("local_strm_root_must_not_append_category", errs)
        self.assertIn("emby_strm_root_must_not_contain_dot_segments", errs)
        self.assertIn("emby_strm_root_must_not_append_category", errs)

    def test_inspect_staging_root_warnings_non_blocking(self) -> None:
        self.assertEqual(inspect_staging_root_warnings("/mv_staging"), [])
        self.assertEqual(
            inspect_staging_root_warnings("/未整理/mp-router"),
            [
                "staging_root_under_unorganized_monitor_risk",
                "staging_root_contains_router_suffix_risk",
            ],
        )
        self.assertEqual(
            inspect_staging_root_warnings("/custom/sub-router"),
            ["staging_root_contains_router_suffix_risk"],
        )

    def test_resolve_post_process_strm_targets_hoist_and_boundaries(self) -> None:
        # TV season folder safely hoists to show root; emby includes show root + season dir
        res_tv = resolve_post_process_strm_targets(
            resolved_strm_dirs=["/strm/anime/DemoShow (1995)/Season 01"],
            resolved_target_dirs=["/已整理/anime/DemoShow (1995)/Season 01"],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            emby_strm_root="/example-emby-strm",
            media_kind="tv",
        )
        self.assertEqual(res_tv["warnings"], [])
        self.assertEqual(res_tv["scrape_dirs"], ["/example-strm/anime/DemoShow (1995)"])
        self.assertEqual(
            res_tv["emby_refresh_dirs"],
            [
                "/example-emby-strm/anime/DemoShow (1995)",
                "/example-emby-strm/anime/DemoShow (1995)/Season 01",
            ],
        )

        # Fallback to resolved_target_dirs when resolved_strm_dirs is empty
        res_fallback = resolve_post_process_strm_targets(
            resolved_strm_dirs=[],
            resolved_target_dirs=["/已整理/series/演示剧集/第1季"],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            emby_strm_root="",
            media_kind="tv",
        )
        self.assertEqual(res_fallback["scrape_dirs"], ["/example-strm/series/演示剧集"])
        self.assertEqual(
            res_fallback["emby_refresh_dirs"],
            ["/example-strm/series/演示剧集", "/example-strm/series/演示剧集/第1季"],
        )

        # Never hoist into category directory (anime/series/kids)
        res_cat = resolve_post_process_strm_targets(
            resolved_strm_dirs=["/strm/anime/Season 01", "/strm/foo/series/S02"],
            resolved_target_dirs=[],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            media_kind="tv",
        )
        self.assertEqual(
            res_cat["scrape_dirs"],
            ["/example-strm/anime/Season 01", "/example-strm/foo/series/S02"],
        )

        # Movie does not hoist even if folder name looks unusual
        res_movie = resolve_post_process_strm_targets(
            resolved_strm_dirs=["/strm/movies/DemoMovie (2020)"],
            resolved_target_dirs=[],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            media_kind="movie",
        )
        self.assertEqual(res_movie["scrape_dirs"], ["/example-strm/movies/DemoMovie (2020)"])

        # Bare category or dot-segments / missing scoped target never falls back to local_strm_root
        res_empty = resolve_post_process_strm_targets(
            resolved_strm_dirs=["/strm/series", "/strm/../escape/Show", "/other/Show"],
            resolved_target_dirs=[],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            media_kind="tv",
        )
        self.assertEqual(res_empty["scrape_dirs"], [])
        self.assertEqual(res_empty["emby_refresh_dirs"], [])
        self.assertIn("strm_post_process_skipped_missing_scoped_target_dir", res_empty["warnings"])

    def test_post_process_settle_scrape_and_emby_refresh_success(self) -> None:
        client = _FakePipelineClient(
            resolved_strm_dirs=["/strm/anime/DemoShow (1995)/Season 01"],
            resolved_target_dirs=["/已整理/anime/DemoShow (1995)/Season 01"],
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            staging_root="/未整理/mp-router",  # verify old staging still runs with warnings
            local_strm_root="/example-strm",
            emby_strm_root="/example-emby-strm",
            auto_scrape_strm=True,
            auto_refresh_emby=True,
            strm_settle_max_attempts=4,
            strm_settle_interval_seconds=1.0,
        )
        exists_checks = [False, False, True]
        sleep_calls: List[float] = []
        scrape_calls: List[tuple] = []
        emby_calls: List[List[str]] = []

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "jpn", "title": "Japanese"}],
                "subtitle_streams": [{"language": "chi", "title": "简体中文"}],
            }

        def _exists(path: str) -> bool:
            return exists_checks.pop(0) if exists_checks else True

        def _scrape(target_dir: str, tmdb_id: int, media_kind: str) -> Dict[str, Any]:
            scrape_calls.append((target_dir, tmdb_id, media_kind))
            return {"ok": True, "target_dir": target_dir}

        def _refresh(paths: List[str]) -> Dict[str, Any]:
            emby_calls.append(paths)
            return {"ok": True, "path_count": len(paths)}

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=890,
            config=cfg,
            run_id="r20",
            probe_runner=_probe,
            sleep_fn=sleep_calls.append,
            scrape_runner=_scrape,
            emby_refresh_runner=_refresh,
            path_exists_fn=_exists,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(sleep_calls, [1.0, 1.0])
        self.assertEqual(scrape_calls, [("/example-strm/anime/DemoShow (1995)", 890, "tv")])
        self.assertEqual(
            emby_calls,
            [[
                "/example-emby-strm/anime/DemoShow (1995)",
                "/example-emby-strm/anime/DemoShow (1995)/Season 01",
            ]],
        )
        self.assertIn("staging_root_under_unorganized_monitor_risk", res.diagnostics["warnings"])
        self.assertIn("staging_root_contains_router_suffix_risk", res.diagnostics["warnings"])
        self.assertEqual(res.diagnostics["post_process"]["scrape_succeeded"], 1)
        self.assertTrue(res.diagnostics["post_process"]["emby_refresh_ok"])

    def test_post_process_symlink_escape_is_blocked_without_scraping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outside = os.path.join(tmpdir, "outside_secret")
            os.makedirs(outside, exist_ok=True)
            strm_root = os.path.join(tmpdir, "example-strm")
            os.makedirs(os.path.join(strm_root, "anime"), exist_ok=True)
            symlink_show = os.path.join(strm_root, "anime", "EscapedShow")
            os.symlink(outside, symlink_show)

            self.assertFalse(is_path_strictly_within_root(symlink_show, strm_root))

            client = _FakePipelineClient(
                resolved_strm_dirs=["/strm/anime/EscapedShow/Season 01"],
            )
            decision = CompletionDecision(
                eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
            )
            cfg = RouterPipelineConfig(
                local_strm_root=strm_root,
                auto_scrape_strm=True,
                auto_refresh_emby=True,
            )
            scrape_called = 0
            emby_called = 0

            def _probe(tool: str, url: str, timeout: int):
                return {
                    "ok": True,
                    "audio_streams": [{"language": "zho", "title": "国语"}],
                    "subtitle_streams": [],
                }

            def _scrape(target_dir: str, tmdb_id: int, media_kind: str) -> Dict[str, Any]:
                nonlocal scrape_called
                scrape_called += 1
                return {"ok": True}

            def _refresh(paths: List[str]) -> Dict[str, Any]:
                nonlocal emby_called
                emby_called += 1
                return {"ok": True}

            res = run_muvyo_routing_pipeline(
                client,  # type: ignore[arg-type]
                decision,
                tmdb_id=891,
                config=cfg,
                run_id="r21",
                probe_runner=_probe,
                scrape_runner=_scrape,
                emby_refresh_runner=_refresh,
            )
            self.assertEqual(res.status, "routed_ok")
            self.assertEqual(scrape_called, 0)
            self.assertEqual(emby_called, 0)
            self.assertIn("strm_scrape_blocked_symlink_or_escape", res.diagnostics["warnings"])
            self.assertIn("emby_refresh_skipped_scrape_failed", res.diagnostics["warnings"])

    def test_post_process_fault_isolation_never_falls_back_to_pt(self) -> None:
        client = _FakePipelineClient(
            api_key="SECRET_KEY_POST_999",
            resolved_strm_dirs=["/strm/anime/DemoShow/Season 01"],
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            local_strm_root="/example-strm",
            auto_scrape_strm=True,
            auto_refresh_emby=True,
        )
        emby_called = 0

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "zho", "title": "国语"}],
                "subtitle_streams": [],
            }

        def _scrape_boom(target_dir: str, tmdb_id: int, media_kind: str) -> Dict[str, Any]:
            raise RuntimeError("scrape boom token=LEAK_SCRAPE_TOK SECRET_KEY_POST_999")

        def _refresh(paths: List[str]) -> Dict[str, Any]:
            nonlocal emby_called
            emby_called += 1
            return {"ok": True}

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=892,
            config=cfg,
            run_id="r22",
            probe_runner=_probe,
            scrape_runner=_scrape_boom,
            emby_refresh_runner=_refresh,
            path_exists_fn=lambda _p: True,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(emby_called, 0)
        self.assertIn("strm_scrape_failed", res.diagnostics["warnings"])
        self.assertIn("emby_refresh_skipped_scrape_failed", res.diagnostics["warnings"])
        dumped = json.dumps(res.to_sanitized_dict(extra_secrets=["SECRET_KEY_POST_999"]), ensure_ascii=False)
        self.assertNotIn("LEAK_SCRAPE_TOK", dumped)
        self.assertNotIn("SECRET_KEY_POST_999", dumped)

    def test_post_process_stop_during_strm_settle_aborts_cleanly_and_keeps_routed_ok(self) -> None:
        client = _FakePipelineClient(resolved_strm_dirs=["/strm/anime/DemoShow/Season 01"])
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            local_strm_root="/example-strm",
            auto_scrape_strm=True,
            auto_refresh_emby=True,
            strm_settle_max_attempts=5,
            strm_settle_interval_seconds=1.5,
        )
        stopped = False
        scrape_called = 0
        emby_called = 0

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "zho", "title": "国语"}],
                "subtitle_streams": [],
            }

        def _sleep_and_stop(_sec: float) -> None:
            nonlocal stopped
            stopped = True

        def _scrape_counter(*_a: Any) -> Dict[str, Any]:
            nonlocal scrape_called
            scrape_called += 1
            return {"ok": True}

        def _emby_counter(*_a: Any) -> Dict[str, Any]:
            nonlocal emby_called
            emby_called += 1
            return {"ok": True}

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=893,
            config=cfg,
            run_id="r23",
            probe_runner=_probe,
            should_stop=lambda: stopped,
            sleep_fn=_sleep_and_stop,
            scrape_runner=_scrape_counter,
            emby_refresh_runner=_emby_counter,
            path_exists_fn=lambda _p: False,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(scrape_called, 0)
        self.assertEqual(emby_called, 0)
        self.assertIn("strm_scrape_aborted_plugin_stopped", res.diagnostics["warnings"])
        self.assertIn("emby_refresh_skipped_plugin_stopped", res.diagnostics["warnings"])

    def test_post_process_emby_refresh_runs_when_auto_scrape_disabled(self) -> None:
        client = _FakePipelineClient(resolved_strm_dirs=["/strm/anime/DemoShow/Season 01"])
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            local_strm_root="/example-strm",
            auto_scrape_strm=False,
            auto_refresh_emby=True,
        )
        emby_calls: List[List[str]] = []

        def _probe(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "zho", "title": "国语"}],
                "subtitle_streams": [],
            }

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=894,
            config=cfg,
            run_id="r24",
            probe_runner=_probe,
            emby_refresh_runner=lambda paths: (emby_calls.append(paths) or {"ok": True}),
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertEqual(len(emby_calls), 1)
        self.assertTrue(res.diagnostics["post_process"]["emby_refresh_ok"])

    def test_p1_1_escaped_target_never_enters_emby_refresh_mixed_or_scrape_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outside = os.path.join(tmpdir, "outside_secret")
            os.makedirs(outside, exist_ok=True)
            strm_root = os.path.join(tmpdir, "example-strm")
            safe_season = os.path.join(strm_root, "anime", "SafeShow", "Season 01")
            os.makedirs(safe_season, exist_ok=True)
            escaped_show = os.path.join(strm_root, "anime", "EscapedShow")
            os.symlink(outside, escaped_show)

            # Case 1: Mixed batch (1 safe target + 1 escaped target) with auto_scrape_strm=True
            client1 = _FakePipelineClient(
                resolved_strm_dirs=[
                    "/strm/anime/SafeShow/Season 01",
                    "/strm/anime/EscapedShow/Season 01",
                ]
            )
            decision = CompletionDecision(
                eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
            )
            cfg1 = RouterPipelineConfig(
                local_strm_root=strm_root,
                emby_strm_root="/example-emby-strm",
                auto_scrape_strm=True,
                auto_refresh_emby=True,
            )
            scraped1: List[str] = []
            refreshed1: List[List[str]] = []

            def _probe(tool: str, url: str, timeout: int):
                return {"ok": True, "audio_streams": [{"language": "zho", "title": "国语"}], "subtitle_streams": []}

            res1 = run_muvyo_routing_pipeline(
                client1,  # type: ignore[arg-type]
                decision,
                tmdb_id=901,
                config=cfg1,
                run_id="r31",
                probe_runner=_probe,
                scrape_runner=lambda d, tid, k: (scraped1.append(d) or {"ok": True}),
                emby_refresh_runner=lambda paths: (refreshed1.append(paths) or {"ok": True}),
            )
            self.assertEqual(res1.status, "routed_ok")
            self.assertEqual(scraped1, [os.path.join(strm_root, "anime", "SafeShow")])
            self.assertEqual(
                refreshed1,
                [["/example-emby-strm/anime/SafeShow", "/example-emby-strm/anime/SafeShow/Season 01"]],
            )
            self.assertIn("strm_scrape_blocked_symlink_or_escape", res1.diagnostics["warnings"])

            # Case 2: Single escaped target with auto_scrape_strm=False must also be rejected from Emby refresh
            client2 = _FakePipelineClient(resolved_strm_dirs=["/strm/anime/EscapedShow/Season 01"])
            cfg2 = RouterPipelineConfig(
                local_strm_root=strm_root,
                emby_strm_root="/example-emby-strm",
                auto_scrape_strm=False,
                auto_refresh_emby=True,
            )
            refreshed2: List[List[str]] = []
            res2 = run_muvyo_routing_pipeline(
                client2,  # type: ignore[arg-type]
                decision,
                tmdb_id=902,
                config=cfg2,
                run_id="r32",
                probe_runner=_probe,
                emby_refresh_runner=lambda paths: (refreshed2.append(paths) or {"ok": True}),
            )
            self.assertEqual(res2.status, "routed_ok")
            self.assertEqual(refreshed2, [])
            self.assertIn("emby_refresh_blocked_symlink_or_escape", res2.diagnostics["warnings"])
            self.assertIn("emby_refresh_skipped_no_safe_target_dir", res2.diagnostics["warnings"])

    def test_p1_2_pure_category_strm_dirs_do_not_pollute_or_block_cloud_fallback(self) -> None:
        # 1) Multi-level category /strm/series/anime alongside real item dir is stripped
        res1 = resolve_post_process_strm_targets(
            resolved_strm_dirs=[
                "/strm/series/anime",
                "/strm/series/anime/DemoShow (1995)/Season 01",
            ],
            resolved_target_dirs=["/已整理/series/anime/DemoShow (1995)/Season 01"],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            media_kind="tv",
        )
        self.assertEqual(res1["scrape_dirs"], ["/example-strm/series/anime/DemoShow (1995)"])
        self.assertNotIn("/example-strm/series/anime", res1["scrape_dirs"])

        # 2) Only pure category STRM dirs (/strm/anime or /strm/series/anime) -> falls back to resolved_target_dirs
        res2 = resolve_post_process_strm_targets(
            resolved_strm_dirs=["/strm/anime", "/strm/series/anime"],
            resolved_target_dirs=["/已整理/anime/DemoShow (1995)/Season 01"],
            mv3_strm_root="/strm",
            cloud_organize_root="/已整理",
            local_strm_root="/example-strm",
            emby_strm_root="/example-emby-strm",
            media_kind="tv",
        )
        self.assertEqual(res2["warnings"], [])
        self.assertEqual(res2["scrape_dirs"], ["/example-strm/anime/DemoShow (1995)"])
        self.assertEqual(
            res2["emby_refresh_dirs"],
            [
                "/example-emby-strm/anime/DemoShow (1995)",
                "/example-emby-strm/anime/DemoShow (1995)/Season 01",
            ],
        )

    def test_p2_3_existing_show_root_still_waits_for_new_season_dir_to_settle(self) -> None:
        client = _FakePipelineClient(
            resolved_strm_dirs=["/strm/anime/OldShow/Season 02"],
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            local_strm_root="/example-strm",
            emby_strm_root="/example-emby-strm",
            auto_scrape_strm=True,
            auto_refresh_emby=True,
            strm_settle_max_attempts=4,
            strm_settle_interval_seconds=1.0,
        )
        # Show root already exists on attempt 1, but Season 02 only appears on attempt 3
        season2_checks = [False, False, True]
        sleep_calls: List[float] = []
        scrape_calls: List[str] = []
        emby_calls: List[List[str]] = []

        def _probe(tool: str, url: str, timeout: int):
            return {"ok": True, "audio_streams": [{"language": "zho", "title": "国语"}], "subtitle_streams": []}

        def _exists(path: str) -> bool:
            if path.endswith("/OldShow"):
                return True
            if path.endswith("/Season 02"):
                return season2_checks.pop(0) if season2_checks else True
            return False

        res_ok = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=903,
            config=cfg,
            run_id="r33",
            probe_runner=_probe,
            sleep_fn=sleep_calls.append,
            scrape_runner=lambda d, tid, k: (scrape_calls.append(d) or {"ok": True}),
            emby_refresh_runner=lambda paths: (emby_calls.append(paths) or {"ok": True}),
            path_exists_fn=_exists,
        )
        self.assertEqual(res_ok.status, "routed_ok")
        self.assertEqual(sleep_calls, [1.0, 1.0])
        self.assertEqual(scrape_calls, ["/example-strm/anime/OldShow"])
        self.assertEqual(
            emby_calls,
            [["/example-emby-strm/anime/OldShow", "/example-emby-strm/anime/OldShow/Season 02"]],
        )

        # If Season 02 never appears, skip scrape & Emby refresh and emit warning
        scrape_calls2: List[str] = []
        emby_calls2: List[List[str]] = []
        res_timeout = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=904,
            config=RouterPipelineConfig(
                local_strm_root="/example-strm",
                auto_scrape_strm=True,
                auto_refresh_emby=True,
                strm_settle_max_attempts=3,
                strm_settle_interval_seconds=0.5,
            ),
            run_id="r34",
            probe_runner=_probe,
            sleep_fn=lambda _s: None,
            scrape_runner=lambda d, tid, k: (scrape_calls2.append(d) or {"ok": True}),
            emby_refresh_runner=lambda paths: (emby_calls2.append(paths) or {"ok": True}),
            path_exists_fn=lambda p: p.endswith("/OldShow"),
        )
        self.assertEqual(res_timeout.status, "routed_ok")
        self.assertEqual(scrape_calls2, [])
        self.assertEqual(emby_calls2, [])
        self.assertIn("strm_scrape_target_not_found", res_timeout.diagnostics["warnings"])
        self.assertIn("emby_refresh_skipped_scrape_failed", res_timeout.diagnostics["warnings"])

    def test_p2_4_trigger_strm_generate_skipped_also_skips_post_process(self) -> None:
        client = _FakePipelineClient(
            resolved_target_dirs=[],
            resolved_strm_dirs=["/strm/anime/DemoShow/Season 01"],
        )
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            trigger_strm_generate=True,
            local_strm_root="/example-strm",
            auto_scrape_strm=True,
            auto_refresh_emby=True,
        )
        scrape_called = 0
        emby_called = 0

        def _probe(tool: str, url: str, timeout: int):
            return {"ok": True, "audio_streams": [{"language": "zho", "title": "国语"}], "subtitle_streams": []}

        def _scrape(*_a: Any) -> Dict[str, Any]:
            nonlocal scrape_called
            scrape_called += 1
            return {"ok": True}

        def _refresh(*_a: Any) -> Dict[str, Any]:
            nonlocal emby_called
            emby_called += 1
            return {"ok": True}

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=905,
            config=cfg,
            run_id="r35",
            probe_runner=_probe,
            scrape_runner=_scrape,
            emby_refresh_runner=_refresh,
            path_exists_fn=lambda _p: True,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertNotIn("generate_strm", client.calls)
        self.assertEqual(scrape_called, 0)
        self.assertEqual(emby_called, 0)
        self.assertIn("strm_generate_skipped_missing_scoped_target_dir", res.diagnostics["warnings"])
        self.assertIn("strm_post_process_skipped_strm_generate_not_run", res.diagnostics["warnings"])
        self.assertEqual(res.diagnostics["post_process"]["skipped_reason"], "strm_generate_not_run")

    def test_p2_5_partial_emby_refresh_failure_records_warning_and_keeps_routed_ok(self) -> None:
        client = _FakePipelineClient(resolved_strm_dirs=["/strm/anime/DemoShow/Season 01"])
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            local_strm_root="/example-strm",
            auto_scrape_strm=True,
            auto_refresh_emby=True,
        )

        def _probe(tool: str, url: str, timeout: int):
            return {"ok": True, "audio_streams": [{"language": "zho", "title": "国语"}], "subtitle_streams": []}

        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=906,
            config=cfg,
            run_id="r36",
            probe_runner=_probe,
            scrape_runner=lambda *a: {"ok": True},
            emby_refresh_runner=lambda paths: {
                "ok": False,
                "partial_failure": True,
                "refreshed_services": 1,
                "failed_services": 1,
                "path_count": len(paths),
                "errors": ["emby2:http_503"],
            },
            path_exists_fn=lambda _p: True,
        )
        self.assertEqual(res.status, "routed_ok")
        self.assertFalse(res.diagnostics["post_process"]["emby_refresh_ok"])
        self.assertIn("emby_refresh_partial_failed", res.diagnostics["warnings"])

    def test_p3_6_empty_staging_failure_includes_staging_root_warnings(self) -> None:
        client = _FakePipelineClient(staged_items=[])
        decision = CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=2, title="演示剧集"
        )
        cfg = RouterPipelineConfig(
            staging_root="/未整理/mp-router",
            staging_settle_max_attempts=1,
            staging_settle_interval_seconds=0.0,
        )
        res = run_muvyo_routing_pipeline(
            client,  # type: ignore[arg-type]
            decision,
            tmdb_id=907,
            config=cfg,
            run_id="r37",
        )
        self.assertEqual(res.status, "manual_review_staging")
        self.assertEqual(res.reason, "staging_contains_no_videos")
        self.assertIn("staging_root_under_unorganized_monitor_risk", res.diagnostics.get("warnings", []))
        self.assertIn("staging_root_contains_router_suffix_risk", res.diagnostics.get("warnings", []))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
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
        self.assertTrue(res.staging_path.startswith("/未整理/mp-router/"))

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
                        "data": [{"fn": "未整理", "fid": "dir_unorg"}],
                    }).encode()
                if "cid=dir_unorg" in url:
                    return 200, {}, json.dumps({
                        "success": True,
                        "data": [{"fn": "mp-router", "fid": "dir_router"}],
                    }).encode()
                # 第三级隔离目录不存在；在 _find_child_folder 返回空后、POST 建目录前变为 stopped
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
            self.assertTrue(path.startswith("/未整理/mp-router/"), path)
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
            self.assertTrue(path.startswith("/未整理/mp-router/"), path)
            self.assertNotIn("REDACTED", path)

        explicit_res = PipelineResult(
            status="fallback_pt",
            reason="err RAW_MV_API_KEY_999",
            staging_path="/未整理/mp-router/RAW_MV_API_KEY_999",
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


if __name__ == "__main__":
    unittest.main()

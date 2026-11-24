from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from muvyosubscriberouter.client import MuvyoClient
from muvyosubscriberouter.completion import CompletionDecision
from muvyosubscriberouter.pipeline import (
    RouterPipelineConfig,
    run_muvyo_routing_pipeline,
    validate_router_paths,
)


class _FakePipelineClient:
    def __init__(
        self,
        *,
        has_results: bool = True,
        staged_items: List[Dict[str, Any]] | None = None,
        organize_ok: bool = True,
        organize_blockers: List[str] | None = None,
        resolved_target_dirs: List[str] | None = None,
    ) -> None:
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
        self.calls: List[str] = []
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
        return {"ok": True, "target_path": target_path, "final_folder_id": "staging_fid_9"}

    def receive_share(self, share_code: str, file_ids, target_path: str, receive_code: str = "") -> Dict[str, Any]:
        self.calls.append("receive")
        return {"ok": True, "file_id_count": len(file_ids), "target_path": target_path}

    def browse_cloud_folder(self, folder_id: str = "", path: str = "", limit: int = 1150) -> Dict[str, Any]:
        self.calls.append("browse_cloud")
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


if __name__ == "__main__":
    unittest.main()

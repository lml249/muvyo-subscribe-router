from __future__ import annotations

from concurrent.futures import Future
import importlib
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

import muvyosubscriberouter as plugin_mod
from muvyosubscriberouter import Event, EventType, MuvyoSubscribeRouter
from muvyosubscriberouter.pipeline import PipelineResult


class _SyncExecutor:
    def __init__(self, fail_submit: bool = False) -> None:
        self.fail_submit = fail_submit
        self.shutdown_called = False

    def submit(self, fn, *args, **kwargs):
        if self.fail_submit:
            raise RuntimeError("pool exhausted")
        fn(*args, **kwargs)
        return None

    def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:
        self.shutdown_called = True


class _FakeSubscribeOper:
    def __init__(
        self,
        subs: Dict[int, Dict[str, Any]],
        *,
        fail_add_history: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.subs = subs
        self.fail_add_history = fail_add_history
        self.fail_delete = fail_delete
        self.updates: List[tuple] = []
        self.deleted: List[int] = []
        self.histories: List[Dict[str, Any]] = []

    def get(self, sid: int):
        return self.subs.get(sid)

    def update(self, sid: int, payload: Dict[str, Any]):
        self.updates.append((sid, dict(payload)))
        if sid in self.subs:
            self.subs[sid].update(payload)

    def delete(self, sid: int):
        if self.fail_delete:
            raise RuntimeError("db delete failed")
        self.deleted.append(sid)
        self.subs.pop(sid, None)

    def add_history(self, **kwargs):
        if self.fail_add_history:
            raise RuntimeError("db add_history failed")
        self.histories.append(kwargs)


class TestPluginLifecycle(unittest.TestCase):
    def _make_plugin(self, **overrides: Any) -> MuvyoSubscribeRouter:
        plugin = MuvyoSubscribeRouter()
        cfg = {
            "enabled": True,
            "mv_base_url": "http://mv3.example.local:7811",
            "mv_api_key": "demo_key_secret",
            "staging_root": "/未整理/mp-router",
            "cloud_organize_root": "/已整理",
            "mv3_strm_root": "/strm",
            "notify": True,
        }
        cfg.update(overrides)
        plugin.init_plugin(cfg)
        return plugin

    def test_contract_form_page_api_and_render_mode(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)
        self.assertTrue(plugin.get_state())
        self.assertEqual(plugin.get_render_mode(), ("vuetify", None))
        form, defaults = plugin.get_form()
        self.assertIsInstance(form, list)
        self.assertEqual(defaults["subtitle_policy"], "auto")
        apis = plugin.get_api()
        paths = {a["path"] for a in apis}
        self.assertEqual(paths, {"/health", "/dry-run"})
        for api in apis:
            self.assertIn("dependencies", api)
        page = plugin.get_page()
        self.assertGreaterEqual(len(page), 2)
        self.assertEqual(getattr(plugin.on_subscribe_added, "_registered_event_type", None), EventType.SubscribeAdded)

    def test_plugin_version_matches_package_manifest(self) -> None:
        package_path = Path(__file__).resolve().parents[1] / "package.v2.json"
        manifest = json.loads(package_path.read_text(encoding="utf-8"))
        entry = manifest["MuvyoSubscribeRouter"]
        self.assertEqual(MuvyoSubscribeRouter.plugin_version, "0.1")
        self.assertEqual(entry["version"], MuvyoSubscribeRouter.plugin_version)
        self.assertIn("v0.1", entry["history"])

    def test_subscribe_added_pauses_then_completes_on_routed_ok(self) -> None:
        plugin = self._make_plugin(on_success_action="complete")
        self.addCleanup(plugin.stop_service)
        plugin._executor = _SyncExecutor()  # type: ignore[assignment]

        fake_oper = _FakeSubscribeOper({
            101: {"id": 101, "name": "完结剧", "type": "电视剧", "season": 1, "tmdbid": 5001, "state": "N"}
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}
        ok_result = PipelineResult(status="routed_ok", reason="muvyo_routed_and_organized", video_count=4, episodes=[1, 2, 3, 4])

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", return_value=ok_result):
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 101, "mediainfo": media}))

        self.assertEqual(fake_oper.updates[0], (101, {"state": "S"}))
        self.assertEqual(fake_oper.deleted, [101])
        self.assertEqual(len(fake_oper.histories), 1)
        self.assertEqual(len(plugin.get_data("history") or []), 1)

    def test_subscribe_added_restores_n_on_fallback_or_submit_error(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)
        plugin._executor = _SyncExecutor(fail_submit=True)  # type: ignore[assignment]

        fake_oper = _FakeSubscribeOper({
            102: {"id": 102, "name": "异常剧", "type": "电视剧", "season": 1, "tmdbid": 5002, "state": "N"}
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper):
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 102, "mediainfo": media}))

        self.assertEqual(fake_oper.updates, [(102, {"state": "S"}), (102, {"state": "N"})])
        self.assertEqual(fake_oper.subs[102]["state"], "N")

    def test_subtitle_gate_fail_keeps_paused_and_records_manual_review(self) -> None:
        plugin = self._make_plugin(on_subtitle_gate_fail="keep_paused")
        self.addCleanup(plugin.stop_service)
        plugin._executor = _SyncExecutor()  # type: ignore[assignment]

        fake_oper = _FakeSubscribeOper({
            103: {"id": 103, "name": "无中字外语剧", "type": "电视剧", "season": 1, "tmdbid": 5003, "state": "N"}
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}
        gate_fail = PipelineResult(
            status="manual_review_staging",
            reason="subtitle_gate_blocked:embedded_chinese_subtitle_missing",
            staging_path="/未整理/mp-router/demo_tmdb5003_s01_r1",
        )

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", return_value=gate_fail):
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 103, "mediainfo": media}))

        self.assertEqual(fake_oper.updates, [(103, {"state": "S"})])
        self.assertEqual(fake_oper.deleted, [])
        self.assertEqual(len(plugin.get_data("manual_reviews") or []), 1)
        page_text = str(plugin.get_page())
        self.assertIn("待人工复核列表", page_text)
        self.assertIn("/未整理/mp-router/demo_tmdb5003_s01_r1", page_text)

    def test_add_history_failure_blocks_delete_and_complete_event(self) -> None:
        plugin = self._make_plugin(on_success_action="complete")
        self.addCleanup(plugin.stop_service)
        plugin._executor = _SyncExecutor()  # type: ignore[assignment]

        fake_oper = _FakeSubscribeOper(
            {104: {"id": 104, "name": "归档失败剧", "type": "电视剧", "season": 1, "tmdbid": 5004, "state": "N"}},
            fail_add_history=True,
        )
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}
        ok_result = PipelineResult(status="routed_ok", reason="muvyo_routed_and_organized", video_count=4, episodes=[1, 2, 3, 4])

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", return_value=ok_result), \
             patch.object(plugin_mod.eventmanager, "send_event") as mock_send:
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 104, "mediainfo": media}))

        self.assertEqual(fake_oper.deleted, [])
        mock_send.assert_not_called()

    def test_delete_failure_blocks_subscribe_complete_event(self) -> None:
        plugin = self._make_plugin(on_success_action="complete")
        self.addCleanup(plugin.stop_service)
        plugin._executor = _SyncExecutor()  # type: ignore[assignment]

        fake_oper = _FakeSubscribeOper(
            {105: {"id": 105, "name": "删除失败剧", "type": "电视剧", "season": 1, "tmdbid": 5005, "state": "N"}},
            fail_delete=True,
        )
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}
        ok_result = PipelineResult(status="routed_ok", reason="muvyo_routed_and_organized", video_count=4, episodes=[1, 2, 3, 4])

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", return_value=ok_result), \
             patch.object(plugin_mod.eventmanager, "send_event") as mock_send:
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 105, "mediainfo": media}))

        self.assertEqual(len(fake_oper.histories), 1)
        self.assertEqual(fake_oper.deleted, [])
        mock_send.assert_not_called()

    def test_state_drift_prevents_overwriting_user_resumed_subscribe(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)
        fake_oper = _FakeSubscribeOper({
            106: {"id": 106, "name": "用户手动恢复剧", "type": "电视剧", "season": 1, "tmdbid": 5006, "state": "R"}
        })
        decision = plugin_mod.CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=4, title="用户手动恢复剧"
        )
        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline") as mock_pipe:
            plugin._process_subscription_worker(106, fake_oper.subs[106], {}, decision)

        mock_pipe.assert_not_called()
        self.assertEqual(fake_oper.updates, [])
        self.assertEqual(fake_oper.subs[106]["state"], "R")

    def test_stopped_plugin_aborts_queued_worker_without_running_pipeline(self) -> None:
        plugin = self._make_plugin()
        fake_oper = _FakeSubscribeOper({
            107: {"id": 107, "name": "停用排队剧", "type": "电视剧", "season": 1, "tmdbid": 5007, "state": "S"}
        })
        decision = plugin_mod.CompletionDecision(
            eligible=True, reason="tv_series_ended", media_kind="tv", season=1, expected_episodes=4, title="停用排队剧"
        )
        plugin.stop_service()
        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline") as mock_pipe:
            plugin._process_subscription_worker(107, fake_oper.subs[107], {}, decision)

        mock_pipe.assert_not_called()
        self.assertEqual(fake_oper.subs[107]["state"], "N")

    def test_get_api_wraps_depends_and_api_dry_run_accepts_dict_or_none(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)

        apis = plugin.get_api()
        for api in apis:
            deps = api.get("dependencies") or []
            self.assertEqual(len(deps), 1)
            dep = deps[0]
            self.assertFalse(callable(dep))
            self.assertIs(getattr(dep, "dependency", None), plugin_mod.get_current_admin)

        missing_res = plugin.api_dry_run()
        self.assertFalse(missing_res["ok"])
        self.assertEqual(missing_res["error"], "title_and_tmdb_id_required")

        fake_result = PipelineResult(
            status="dry_run_matched",
            reason="dry_run_selected",
            video_count=8,
            episodes=[1, 2, 3, 4, 5, 6, 7, 8],
        )
        with patch.object(plugin_mod, "run_muvyo_routing_pipeline", return_value=fake_result) as mock_pipe:
            res = plugin.api_dry_run({
                "title": "示例剧",
                "tmdb_id": 9001,
                "media_kind": "tv",
                "season": 4,
                "expected_episodes": 8,
            })
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "dry_run_matched")
        self.assertEqual(res["video_count"], 8)
        mock_pipe.assert_called_once()

    def test_neo_import_prefers_user_oper_get_current_admin_without_downgrading_base(self) -> None:
        sentinel_admin = lambda: {"username": "neo_admin"}

        class _HostPluginBase:
            pass

        class _HostEventManager:
            def register(self, event_type: Any):
                def _dec(fn):
                    return fn
                return _dec

            def send_event(self, event_type: Any, data: Any = None) -> None:
                return None

        fake_modules: Dict[str, Any] = {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.event": types.ModuleType("app.core.event"),
            "app.core.security": types.ModuleType("app.core.security"),  # intentionally lacks get_current_admin
            "app.db": types.ModuleType("app.db"),
            "app.db.subscribe_oper": types.ModuleType("app.db.subscribe_oper"),
            "app.db.user_oper": types.ModuleType("app.db.user_oper"),
            "app.chain": types.ModuleType("app.chain"),
            "app.chain.subscribe": types.ModuleType("app.chain.subscribe"),
            "app.chain.tmdb": types.ModuleType("app.chain.tmdb"),
            "app.log": types.ModuleType("app.log"),
            "app.plugins": types.ModuleType("app.plugins"),
            "app.schemas": types.ModuleType("app.schemas"),
            "app.schemas.types": types.ModuleType("app.schemas.types"),
        }
        fake_modules["app.core.event"].Event = object
        fake_modules["app.core.event"].eventmanager = _HostEventManager()
        fake_modules["app.db.subscribe_oper"].SubscribeOper = object
        fake_modules["app.db.user_oper"].get_current_admin = sentinel_admin
        fake_modules["app.chain.subscribe"].SubscribeChain = object
        fake_modules["app.chain.tmdb"].TmdbChain = object
        fake_modules["app.log"].logger = object()
        fake_modules["app.plugins"]._PluginBase = _HostPluginBase
        fake_modules["app.schemas.types"].EventType = types.SimpleNamespace(
            SubscribeAdded="subscribe.added", SubscribeComplete="subscribe.complete"
        )
        fake_modules["app.schemas.types"].MediaType = types.SimpleNamespace(TV="电视剧", MOVIE="电影")
        fake_modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Subscribe="subscribe")

        with patch.dict(sys.modules, fake_modules, clear=False):
            try:
                reloaded = importlib.reload(plugin_mod)
                self.assertIs(reloaded.get_current_admin, sentinel_admin)
                self.assertTrue(issubclass(reloaded.MuvyoSubscribeRouter, _HostPluginBase))
            finally:
                pass
        importlib.reload(plugin_mod)

    def test_host_mode_without_get_current_admin_fails_closed(self) -> None:
        class _HostPluginBase:
            pass

        class _HostEventManager:
            def register(self, event_type: Any):
                return lambda fn: fn

            def send_event(self, event_type: Any, data: Any = None) -> None:
                return None

        fake_modules: Dict[str, Any] = {
            "app": types.ModuleType("app"),
            "app.core": types.ModuleType("app.core"),
            "app.core.event": types.ModuleType("app.core.event"),
            "app.core.security": types.ModuleType("app.core.security"),
            "app.db": types.ModuleType("app.db"),
            "app.db.subscribe_oper": types.ModuleType("app.db.subscribe_oper"),
            "app.db.user_oper": types.ModuleType("app.db.user_oper"),
            "app.chain": types.ModuleType("app.chain"),
            "app.chain.subscribe": types.ModuleType("app.chain.subscribe"),
            "app.chain.tmdb": types.ModuleType("app.chain.tmdb"),
            "app.log": types.ModuleType("app.log"),
            "app.plugins": types.ModuleType("app.plugins"),
            "app.schemas": types.ModuleType("app.schemas"),
            "app.schemas.types": types.ModuleType("app.schemas.types"),
        }
        fake_modules["app.core.event"].Event = object
        fake_modules["app.core.event"].eventmanager = _HostEventManager()
        fake_modules["app.db.subscribe_oper"].SubscribeOper = object
        fake_modules["app.chain.subscribe"].SubscribeChain = object
        fake_modules["app.chain.tmdb"].TmdbChain = object
        fake_modules["app.log"].logger = object()
        fake_modules["app.plugins"]._PluginBase = _HostPluginBase
        fake_modules["app.schemas.types"].EventType = types.SimpleNamespace(
            SubscribeAdded="subscribe.added", SubscribeComplete="subscribe.complete"
        )
        fake_modules["app.schemas.types"].MediaType = types.SimpleNamespace(TV="电视剧", MOVIE="电影")
        fake_modules["app.schemas.types"].NotificationType = types.SimpleNamespace(Subscribe="subscribe")

        try:
            with patch.dict(sys.modules, fake_modules, clear=False):
                reloaded = importlib.reload(plugin_mod)
                self.assertTrue(reloaded._HAS_MOVIEPILOT_HOST)
                with self.assertRaises(Exception) as ctx:
                    reloaded.get_current_admin()
                self.assertIn("admin_auth_unavailable", str(ctx.exception))
        finally:
            importlib.reload(plugin_mod)

    def test_stop_service_restores_cancelled_queued_subscription_and_clears_inflight(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)

        fake_oper = _FakeSubscribeOper({
            201: {"id": 201, "name": "并发剧1", "type": "电视剧", "season": 1, "tmdbid": 6001, "state": "N"},
            202: {"id": 202, "name": "并发剧2", "type": "电视剧", "season": 1, "tmdbid": 6002, "state": "N"},
            203: {"id": 203, "name": "排队剧3", "type": "电视剧", "season": 1, "tmdbid": 6003, "state": "N"},
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}

        started_count = 0
        started_lock = threading.Lock()
        both_started = threading.Event()
        release_workers = threading.Event()
        ran_sids: List[int] = []

        def _blocking_pipeline(client, decision, tmdb_id=0, config=None, run_id="", should_stop=None):
            nonlocal started_count
            with started_lock:
                ran_sids.append(tmdb_id)
                started_count += 1
                if started_count >= 2:
                    both_started.set()
            release_workers.wait(timeout=5.0)
            return PipelineResult(status="aborted_stopped", reason="stopped_during_pipeline")

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", side_effect=_blocking_pipeline):
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 201, "mediainfo": media}))
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 202, "mediainfo": media}))
            self.assertTrue(both_started.wait(timeout=3.0), "expected 2 workers to saturate thread pool")

            # 3rd subscription sits queued in ThreadPoolExecutor (max_workers=2)
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 203, "mediainfo": media}))
            self.assertEqual(fake_oper.subs[203]["state"], "S")
            self.assertIn(203, plugin._inflight_sids)

            # Stopping service cancels queued 3rd future and must immediately restore it to 'N' & clear inflight
            plugin.stop_service()
            self.assertEqual(fake_oper.subs[203]["state"], "N")
            self.assertNotIn(203, plugin._inflight_sids)

            # Release the 2 running workers and wait briefly for clean shutdown
            release_workers.set()
            deadline = threading.Event()
            for _ in range(50):
                if not plugin._inflight_sids:
                    break
                deadline.wait(0.02)

        self.assertNotIn(6003, ran_sids)
        self.assertEqual(fake_oper.subs[203]["state"], "N")
        self.assertEqual(plugin._inflight_sids, set())

    def test_stop_service_does_not_deadlock_when_pending_future_has_cleanup_callback(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)

        pending_future: Future[Any] = Future()

        class _NoCancelShutdownExecutor:
            def submit(self, fn, *args, **kwargs):
                return pending_future

            def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:
                # Intentionally leave pending_future uncancelled so stop_service()
                # itself must call pending_future.cancel() while _cleanup_future is attached.
                return None

        plugin._executor = _NoCancelShutdownExecutor()  # type: ignore[assignment]
        fake_oper = _FakeSubscribeOper({
            204: {"id": 204, "name": "防死锁剧", "type": "电视剧", "season": 1, "tmdbid": 6004, "state": "N"},
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper):
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 204, "mediainfo": media}))
            self.assertEqual(fake_oper.subs[204]["state"], "S")
            self.assertIn(204, plugin._inflight_sids)
            self.assertIn(pending_future, plugin._pending_futures)

            stop_errors: List[BaseException] = []

            def _run_stop() -> None:
                try:
                    plugin.stop_service()
                except BaseException as exc:  # pragma: no cover
                    stop_errors.append(exc)

            t = threading.Thread(target=_run_stop, daemon=True)
            t.start()
            t.join(timeout=2.0)

        self.assertFalse(t.is_alive(), "stop_service deadlocked while cancelling pending future with done_callback")
        self.assertEqual(stop_errors, [])
        self.assertTrue(pending_future.cancelled())
        self.assertEqual(fake_oper.subs[204]["state"], "N")
        self.assertEqual(plugin._inflight_sids, set())
        self.assertEqual(plugin._pending_futures, {})

    def test_reload_during_submit_aba_window_restores_cancelled_subscription(self) -> None:
        plugin = self._make_plugin()
        self.addCleanup(plugin.stop_service)

        stale_future: Future[Any] = Future()

        class _ReloadingOnSubmitExecutor:
            def __init__(self) -> None:
                self.reloaded = False

            def submit(self, fn, *args, **kwargs):
                # Simulate same-instance init_plugin(enabled=True) running after submit()
                # and before on_subscribe_added acquires self._lock to register stale_future.
                if not self.reloaded:
                    self.reloaded = True
                    plugin.init_plugin({
                        "enabled": True,
                        "mv_base_url": "http://mv3.example.local:7811",
                        "mv_api_key": "demo_key_secret",
                        "staging_root": "/未整理/mp-router",
                        "cloud_organize_root": "/已整理",
                        "mv3_strm_root": "/strm",
                        "notify": False,
                    })
                return stale_future

            def shutdown(self, wait: bool = False, cancel_futures: bool = False) -> None:
                if cancel_futures:
                    stale_future.cancel()

        old_executor = _ReloadingOnSubmitExecutor()
        plugin._executor = old_executor  # type: ignore[assignment]
        fake_oper = _FakeSubscribeOper({
            205: {"id": 205, "name": "重载ABA剧", "type": "电视剧", "season": 1, "tmdbid": 6005, "state": "N"},
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 4}]}

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper):
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 205, "mediainfo": media}))

        self.assertTrue(plugin.get_state())
        self.assertIsNot(plugin._executor, old_executor)
        self.assertTrue(stale_future.cancelled())
        self.assertEqual(fake_oper.subs[205]["state"], "N")
        self.assertEqual(plugin._inflight_sids, set())
        self.assertEqual(plugin._pending_futures, {})

    def test_reload_to_dry_run_aborts_running_old_generation_before_receive(self) -> None:
        plugin = self._make_plugin(
            dry_run=False,
            trigger_pt_search_on_fallback=True,
            notify=False,
        )
        self.addCleanup(plugin.stop_service)

        search_entered = threading.Event()
        release_search = threading.Event()

        class _BlockingSearchClient:
            def __init__(self) -> None:
                self.ensure_calls = 0
                self.receive_calls = 0

            def search_resources(self, keyword: str, channels=None) -> Dict[str, Any]:
                search_entered.set()
                release_search.wait(timeout=5.0)
                return {
                    "ok": True,
                    "items": [{"title": "热重载测试剧 S01 1080p", "share_code": "sc_reload_1"}],
                }

            def parse_share(self, share_url: str, receive_code: str = "") -> Dict[str, Any]:
                return {"ok": True, "share_code": "sc_reload_1", "receive_code": receive_code}

            def browse_share(self, share_code: str, receive_code: str = "", cid: str = "", limit: int = 1150) -> Dict[str, Any]:
                return {
                    "ok": True,
                    "items": [
                        {"name": "ReloadTest.S01E01.mkv", "file_id": "rf1"},
                        {"name": "ReloadTest.S01E02.mkv", "file_id": "rf2"},
                    ],
                }

            def ensure_115_folder(self, target_path: str) -> Dict[str, Any]:
                self.ensure_calls += 1
                return {"ok": True, "target_path": target_path, "final_folder_id": "staging_fid_reload"}

            def receive_share(self, share_code: str, file_ids, target_path: str, receive_code: str = "") -> Dict[str, Any]:
                self.receive_calls += 1
                return {"ok": True, "file_id_count": len(file_ids), "target_path": target_path}

        fake_client = _BlockingSearchClient()
        fake_oper = _FakeSubscribeOper({
            206: {"id": 206, "name": "热重载测试剧", "type": "电视剧", "season": 1, "tmdbid": 6006, "state": "N"},
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 2}]}

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "MuvyoClient", return_value=fake_client), \
             patch.object(plugin_mod, "SubscribeChain") as mock_chain:
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 206, "mediainfo": media}))
            self.assertTrue(search_entered.wait(timeout=3.0), "old generation worker did not enter search")

            plugin.init_plugin({
                "enabled": True,
                "dry_run": True,
                "mv_base_url": "http://mv3.example.local:7811",
                "mv_api_key": "demo_key_secret",
                "staging_root": "/未整理/mp-router",
                "cloud_organize_root": "/已整理",
                "mv3_strm_root": "/strm",
                "trigger_pt_search_on_fallback": True,
                "notify": False,
            })
            release_search.set()

            waiter = threading.Event()
            for _ in range(100):
                if not plugin._inflight_sids and (plugin.get_data("history") or []):
                    break
                waiter.wait(0.02)

        history = plugin.get_data("history") or []
        self.assertGreaterEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "aborted_stopped")
        self.assertEqual(fake_client.ensure_calls, 0)
        self.assertEqual(fake_client.receive_calls, 0)
        self.assertEqual(fake_oper.subs[206]["state"], "N")
        mock_chain.return_value.search.assert_not_called()
        self.assertEqual(plugin._inflight_sids, set())

    def test_reload_during_restore_subscribe_prevents_old_generation_pt_search(self) -> None:
        plugin = self._make_plugin(trigger_pt_search_on_fallback=True)
        self.addCleanup(plugin.stop_service)

        update_n_entered = threading.Event()
        release_update_n = threading.Event()

        class _BlockingRestoreOper(_FakeSubscribeOper):
            def update(self, sid: int, payload: Dict[str, Any]):
                if payload.get("state") == "N":
                    update_n_entered.set()
                    release_update_n.wait(timeout=5.0)
                super().update(sid, payload)

        fake_oper = _BlockingRestoreOper({
            207: {"id": 207, "name": "回退重载竞态剧", "type": "电视剧", "season": 1, "tmdbid": 6007, "state": "N"},
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 2}]}
        fallback_result = PipelineResult(status="fallback_pt", reason="no_suitable_share_candidate")

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", return_value=fallback_result), \
             patch.object(plugin_mod, "SubscribeChain") as mock_chain:
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 207, "mediainfo": media}))
            self.assertTrue(update_n_entered.wait(timeout=3.0), "old generation worker did not enter restore update")

            plugin.init_plugin({
                "enabled": True,
                "mv_base_url": "http://mv3.example.local:7811",
                "mv_api_key": "demo_key_secret",
                "staging_root": "/未整理/mp-router",
                "cloud_organize_root": "/已整理",
                "mv3_strm_root": "/strm",
                "trigger_pt_search_on_fallback": True,
                "notify": False,
            })
            release_update_n.set()

            waiter = threading.Event()
            for _ in range(100):
                if not plugin._inflight_sids and (plugin.get_data("history") or []):
                    break
                waiter.wait(0.02)

        self.assertEqual(fake_oper.subs[207]["state"], "N")
        mock_chain.return_value.search.assert_not_called()
        self.assertEqual(plugin._inflight_sids, set())

    def test_reload_during_complete_subscribe_get_aborts_before_history_and_delete(self) -> None:
        plugin = self._make_plugin(on_success_action="complete")
        self.addCleanup(plugin.stop_service)

        routed_done = threading.Event()
        get_entered = threading.Event()
        release_get = threading.Event()

        class _BlockingCompleteGetOper(_FakeSubscribeOper):
            def __init__(self, subs: Dict[int, Dict[str, Any]]) -> None:
                super().__init__(subs)
                self._blocked_once = False

            def get(self, sid: int):
                res = super().get(sid)
                if routed_done.is_set() and not self._blocked_once:
                    self._blocked_once = True
                    get_entered.set()
                    release_get.wait(timeout=5.0)
                return res

        fake_oper = _BlockingCompleteGetOper({
            208: {"id": 208, "name": "收口重载竞态剧", "type": "电视剧", "season": 1, "tmdbid": 6008, "state": "N"},
        })
        media = {"status": "Ended", "last_air_date": "2024-01-01", "season_info": [{"season_number": 1, "episode_count": 2}]}
        ok_result = PipelineResult(
            status="routed_ok",
            reason="muvyo_routed_and_organized",
            video_count=2,
            episodes=[1, 2],
        )

        def _fake_pipeline(*args, **kwargs):
            routed_done.set()
            return ok_result

        with patch.object(plugin_mod, "SubscribeOper", return_value=fake_oper), \
             patch.object(plugin_mod, "run_muvyo_routing_pipeline", side_effect=_fake_pipeline), \
             patch.object(plugin_mod.eventmanager, "send_event") as mock_send_event, \
             patch.object(plugin, "_send_notice") as mock_send_notice:
            plugin.on_subscribe_added(Event(EventType.SubscribeAdded, {"subscribe_id": 208, "mediainfo": media}))
            self.assertTrue(get_entered.wait(timeout=3.0), "old generation worker did not enter _complete_subscribe get")

            plugin.init_plugin({
                "enabled": True,
                "mv_base_url": "http://mv3.example.local:7811",
                "mv_api_key": "demo_key_secret",
                "staging_root": "/未整理/mp-router",
                "cloud_organize_root": "/已整理",
                "mv3_strm_root": "/strm",
                "on_success_action": "pause",
                "notify": False,
            })
            release_get.set()

            waiter = threading.Event()
            for _ in range(100):
                if not plugin._inflight_sids:
                    break
                waiter.wait(0.02)

        self.assertEqual(fake_oper.histories, [])
        self.assertEqual(fake_oper.deleted, [])
        mock_send_event.assert_not_called()
        mock_send_notice.assert_not_called()
        self.assertEqual(fake_oper.subs[208]["state"], "S")
        self.assertEqual(plugin._inflight_sids, set())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from muvyosubscriberouter.selector import (
    extract_season_and_episode,
    item_is_folder,
    select_best_share_candidate,
)


class _FakeSelectorClient:
    def __init__(self, search_map: Dict[str, List[Dict[str, Any]]], browse_map: Dict[tuple, List[Dict[str, Any]]]) -> None:
        self._search_map = search_map
        self._browse_map = browse_map
        self.parse_calls: List[str] = []

    def search_resources(self, keyword: str, channels=None) -> Dict[str, Any]:
        items = self._search_map.get(keyword, [])
        return {"ok": True, "status": 200, "items": items, "result_count": len(items)}

    def parse_share(self, share_url: str, receive_code: str = "") -> Dict[str, Any]:
        self.parse_calls.append(share_url)
        code = share_url.rsplit("/", 1)[-1]
        return {"ok": True, "status": 200, "share_code": code, "receive_code": receive_code or "parsed_rc"}

    def browse_share(self, share_code: str, receive_code: str = "", cid: str = "", limit: int = 1150) -> Dict[str, Any]:
        items = self._browse_map.get((share_code, cid), [])
        return {"ok": True, "status": 200, "items": items, "item_count": len(items)}


class TestSelector(unittest.TestCase):
    def test_extract_season_and_episode_patterns(self) -> None:
        self.assertEqual(extract_season_and_episode("Show.S02E05.1080p.mkv"), (2, 5))
        self.assertEqual(extract_season_and_episode("Show.1x03.mkv"), (1, 3))
        self.assertEqual(extract_season_and_episode("示例剧 第2季 第08集.mp4"), (2, 8))

    def test_skips_wrong_season_and_incomplete_and_excludes_metadata_sidecars(self) -> None:
        search_map = {
            "示例剧": [
                {"title": "示例剧 S01 缺集版", "share_url": "https://share.example.local/s/bad_incomplete"},
                {"title": "示例剧 S02 错季版", "share_url": "https://share.example.local/s/bad_season"},
                {"title": "示例剧 S01 2160p 全集", "share_url": "https://share.example.local/s/good_nested"},
            ]
        }
        browse_map = {
            ("bad_incomplete", ""): [
                {"name": "Show.S01E01.mkv", "file_id": "i1"},
                {"name": "Show.S01E03.mkv", "file_id": "i3"},
            ],
            ("bad_season", ""): [
                {"name": "Show.S02E01.mkv", "file_id": "w1"},
                {"name": "Show.S02E02.mkv", "file_id": "w2"},
                {"name": "Show.S02E03.mkv", "file_id": "w3"},
            ],
            ("good_nested", ""): [
                {"name": "Season 01", "cid": "dir_s1", "is_dir": True},
                {"name": "Season 02", "cid": "dir_s2", "is_dir": True},
            ],
            ("good_nested", "dir_s1"): [
                {"name": "Show.S01E01.2160p.mkv", "file_id": "g1"},
                {"name": "Show.S01E02.2160p.mkv", "file_id": "g2"},
                {"name": "Show.S01E03.2160p.mkv", "file_id": "g3"},
                {"name": "Show.S01E01.2160p.ass", "file_id": "sub1"},
                {"name": "tvshow.nfo", "file_id": "meta1"},
                {"name": "poster.jpg", "file_id": "meta2"},
                {"name": "fanart.webp", "file_id": "meta3"},
            ],
        }
        client = _FakeSelectorClient(search_map, browse_map)
        plan, diag = select_best_share_candidate(
            client,  # type: ignore[arg-type]
            title="示例剧",
            media_kind="tv",
            season=1,
            expected_episodes=3,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.share_code, "good_nested")
        self.assertEqual(plan.browse_cid, "dir_s1")
        self.assertEqual(plan.episodes, [1, 2, 3])
        self.assertEqual(plan.video_count, 3)
        self.assertEqual(plan.subtitle_sidecar_count, 1)
        self.assertEqual(plan.excluded_metadata_count, 3)
        self.assertEqual(plan.file_ids, ["g1", "g2", "g3", "sub1"])
        self.assertNotIn("meta1", plan.file_ids)
        self.assertNotIn("meta2", plan.file_ids)
        self.assertGreaterEqual(len(diag), 2)

    def test_rejects_wrong_season_title_and_single_wrong_season_folder_drill(self) -> None:
        search_map = {
            "示例剧 第2季": [
                {"title": "示例剧 第一季 全集", "share_url": "https://share.example.local/s/wrong_title_s1"},
                {"title": "示例剧 合集", "share_url": "https://share.example.local/s/wrong_dir_s1"},
            ]
        }
        browse_map = {
            ("wrong_dir_s1", ""): [
                {"fn": "Season 1", "cid": "only_s1_dir", "is_dir": True},
            ],
            ("wrong_dir_s1", "only_s1_dir"): [
                {"fn": "E01.mkv", "fid": "e1"},
                {"fn": "E02.mkv", "fid": "e2"},
            ],
        }
        client = _FakeSelectorClient(search_map, browse_map)
        plan, diag = select_best_share_candidate(
            client,  # type: ignore[arg-type]
            title="示例剧",
            media_kind="tv",
            season=2,
            expected_episodes=2,
        )
        self.assertIsNone(plan)
        self.assertTrue(any(d.get("reason") == "wrong_season_title" for d in diag))

    def test_always_calls_parse_when_share_url_present_even_with_share_code(self) -> None:
        search_map = {
            "示例剧": [
                {
                    "title": "示例剧 S01",
                    "share_code": "stale_code",
                    "share_url": "https://share.example.local/s/fresh_parsed_code",
                }
            ]
        }
        browse_map = {
            ("fresh_parsed_code", ""): [
                {"fn": "Show.S01E01.mkv", "fid": "f1"},
                {"fn": "Show.S01E02.mkv", "fid": "f2"},
            ]
        }
        client = _FakeSelectorClient(search_map, browse_map)
        plan, _ = select_best_share_candidate(
            client,  # type: ignore[arg-type]
            title="示例剧",
            media_kind="tv",
            season=1,
            expected_episodes=2,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(client.parse_calls, ["https://share.example.local/s/fresh_parsed_code"])
        self.assertEqual(plan.share_code, "fresh_parsed_code")
        self.assertEqual(plan.receive_code, "parsed_rc")

    def test_item_is_folder_supports_115_fc_and_pc_priority(self) -> None:
        # 1. Explicit bool
        self.assertTrue(item_is_folder({"name": "a.mkv", "is_dir": True}))
        self.assertFalse(item_is_folder({"name": "Season 4", "cid": "c1", "is_dir": False}))
        self.assertFalse(item_is_folder({"name": "Season 4", "cid": "c1", "is_folder": False}))
        # 2. Explicit type/kind/category
        self.assertTrue(item_is_folder({"name": "Season 4", "type": "folder"}))
        self.assertFalse(item_is_folder({"name": "Season 4", "category": "video"}))
        # 3. 115 native fc (0=folder, 1=file), even with pc present
        self.assertTrue(item_is_folder({"n": "示例剧", "cid": "c_root", "pc": "pc_dir_1", "fc": 0}))
        self.assertTrue(item_is_folder({"n": "Season 4", "cid": "c_s4", "pc": "pc_dir_2", "fc": "0"}))
        self.assertFalse(item_is_folder({"n": "Show.S04E01.mkv", "fid": "f1", "cid": "c_s4", "pc": "pc_f1", "fc": 1}))
        self.assertFalse(item_is_folder({"n": "Show.S04E02.mkv", "fid": "f2", "cid": "c_s4", "pc": "pc_f2", "fc": "1"}))
        # 4. Non-empty fid/file_id without fc -> file
        self.assertFalse(item_is_folder({"n": "README", "fid": "f_readme", "cid": "c_s4"}))
        self.assertFalse(item_is_folder({"name": "README", "file_id": "f_readme2"}))
        # 5. cid/folder_id with pc/pickcode and non-video/subtitle extension -> folder
        self.assertTrue(item_is_folder({"n": "Season 4", "cid": "c_s4", "pc": "pc_dir_3"}))
        self.assertTrue(item_is_folder({"name": "Season 4", "folder_id": "fld_4", "pickcode": "pc_dir_4"}))
        self.assertFalse(item_is_folder({"n": "Show.S04E01.mkv", "cid": "c_s4", "pc": "pc_v1"}))

    def test_two_level_115_folder_drill_with_pc_and_fc(self) -> None:
        search_map = {
            "示例剧 第4季": [
                {
                    "title": "示例剧 第4季 1080p",
                    "drive_type": "115",
                    "share_url": "https://share.example.local/s/demo_s4_share",
                }
            ]
        }
        browse_map = {
            ("demo_s4_share", ""): [
                {"n": "示例剧 (2020)", "cid": "cid_show_root", "pc": "pc_root_dir", "fc": 0},
            ],
            ("demo_s4_share", "cid_show_root"): [
                {"n": "Season 3", "cid": "cid_s3", "pc": "pc_s3_dir", "fc": 0},
                {"n": "Season 4", "cid": "cid_s4", "pc": "pc_s4_dir", "fc": 0},
            ],
            ("demo_s4_share", "cid_s4"): [
                {
                    "n": f"Demo.Show.S04E{ep:02d}.1080p.mkv",
                    "fid": f"fid_s4e{ep:02d}",
                    "cid": "cid_s4",
                    "pc": f"pc_s4e{ep:02d}",
                    "fc": 1,
                }
                for ep in range(1, 9)
            ],
        }
        client = _FakeSelectorClient(search_map, browse_map)
        plan, _ = select_best_share_candidate(
            client,  # type: ignore[arg-type]
            title="示例剧",
            media_kind="tv",
            season=4,
            expected_episodes=8,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.browse_cid, "cid_s4")
        self.assertEqual(plan.episodes, [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(plan.video_count, 8)
        self.assertEqual(len(plan.file_ids), 8)

    def test_skips_non_115_drive_candidates_with_diagnostics(self) -> None:
        search_map = {
            "示例剧": [
                {"title": "示例剧 S01 夸克", "drive_type": "quark", "share_url": "https://share.example.local/s/q1"},
                {"title": "示例剧 S01 空白drive", "drive_type": "   ", "pan_type": "quark", "share_url": "https://share.example.local/s/blank_drive"},
                {"title": "示例剧 S01 阿里", "pan_type": "aliyun", "share_url": "https://share.example.local/s/a1"},
                {"title": "示例剧 S01 百度", "drive_type": "baidu", "share_url": "https://share.example.local/s/b1"},
                {"title": "示例剧 S01 UC", "pan_type": "uc", "share_url": "https://share.example.local/s/u1"},
                {"title": "示例剧 S01 115", "drive_type": "115网盘", "share_url": "https://share.example.local/s/ok115"},
            ]
        }
        browse_map = {
            ("ok115", ""): [
                {"n": "Show.S01E01.mkv", "fid": "f1", "cid": "0", "fc": 1},
                {"n": "Show.S01E02.mkv", "fid": "f2", "cid": "0", "fc": 1},
            ]
        }
        client = _FakeSelectorClient(search_map, browse_map)
        plan, diag = select_best_share_candidate(
            client,  # type: ignore[arg-type]
            title="示例剧",
            media_kind="tv",
            season=1,
            expected_episodes=2,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.share_code, "ok115")
        self.assertEqual(client.parse_calls, ["https://share.example.local/s/ok115"])
        non_115_diags = [d for d in diag if d.get("reason") == "non_115_drive"]
        self.assertEqual(len(non_115_diags), 5)


if __name__ == "__main__":
    unittest.main()

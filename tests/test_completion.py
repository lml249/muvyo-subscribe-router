from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from muvyosubscriberouter.completion import evaluate_subscription_completion


class TestCompletion(unittest.TestCase):
    def test_ended_tv_series_is_eligible(self) -> None:
        sub = {"name": "演示剧集", "type": "电视剧", "season": 1, "tmdbid": 1001}
        media = {
            "status": "Ended",
            "last_air_date": "2024-06-01",
            "season_info": [{"season_number": 1, "episode_count": 12, "air_date": "2024-03-01"}],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 1))
        self.assertTrue(res.eligible)
        self.assertEqual(res.reason, "tv_series_ended")
        self.assertEqual(res.expected_episodes, 12)
        self.assertEqual(res.season, 1)

    def test_ongoing_tv_series_passes_through_to_pt(self) -> None:
        sub = {"name": "连载剧集", "type": "电视剧", "season": 2, "tmdbid": 1002}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 2,
            "next_episode_to_air": {"season_number": 2, "episode_number": 5},
            "season_info": [{"season_number": 2, "episode_count": 10}],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 1))
        self.assertFalse(res.eligible)
        self.assertEqual(res.reason, "next_episode_to_air_in_target_season")

    def test_previous_season_of_returning_series_is_eligible(self) -> None:
        sub = {"name": "多季剧集", "type": "电视剧", "season": 1, "tmdbid": 1003}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 2,
            "next_episode_to_air": {"season_number": 2, "episode_number": 3},
            "season_info": [
                {"season_number": 1, "episode_count": 8, "air_date": "2023-01-01"},
                {"season_number": 2, "episode_count": 8, "air_date": "2024-11-01"},
            ],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 1))
        self.assertTrue(res.eligible)
        self.assertEqual(res.reason, "tv_previous_season_completed")
        self.assertEqual(res.expected_episodes, 8)

    def test_unknown_status_and_missing_mediainfo_are_conservative(self) -> None:
        sub = {"name": "未知状态剧", "type": "电视剧", "season": 1, "tmdbid": 1004}
        res_none = evaluate_subscription_completion(sub, None)
        self.assertFalse(res_none.eligible)
        self.assertEqual(res_none.reason, "missing_mediainfo_conservative_passthrough")

        res_unk = evaluate_subscription_completion(sub, {"status": ""})
        self.assertFalse(res_unk.eligible)
        self.assertTrue(res_unk.reason.startswith("tv_status_unknown"))

    def test_tmdb_fallback_fetcher_used_when_mediainfo_missing(self) -> None:
        sub = {"name": "补查剧集", "type": "电视剧", "season": 1, "tmdbid": 1005}
        called = []

        def _fetcher(tid: int, kind: str):
            called.append((tid, kind))
            return {
                "status": "Ended",
                "last_air_date": "2022-05-01",
                "season_info": [{"season_number": 1, "episode_count": 6}],
            }

        res = evaluate_subscription_completion(
            sub, None, tmdb_fallback_fetcher=_fetcher, today=date(2025, 1, 1)
        )
        self.assertEqual(called, [(1005, "tv")])
        self.assertTrue(res.eligible)
        self.assertEqual(res.expected_episodes, 6)

    def test_tmdb_fallback_enriches_partial_mediainfo_missing_status(self) -> None:
        sub = {"name": "部分元数据剧集", "type": "电视剧", "season": 1, "tmdbid": 1006}
        partial_media = {"title": "部分元数据剧集", "year": "2024"}
        called = []

        def _fetcher(tid: int, kind: str):
            called.append((tid, kind))
            return {
                "status": "Ended",
                "last_air_date": "2024-08-01",
                "season_info": [{"season_number": 1, "episode_count": 10}],
            }

        res = evaluate_subscription_completion(
            sub, partial_media, tmdb_fallback_fetcher=_fetcher, today=date(2025, 1, 1)
        )
        self.assertEqual(called, [(1006, "tv")])
        self.assertTrue(res.eligible)
        self.assertEqual(res.expected_episodes, 10)

    def test_movie_and_best_version_guards(self) -> None:
        movie_sub = {"name": "示例电影", "type": "电影", "tmdbid": 2001}
        self.assertFalse(evaluate_subscription_completion(movie_sub, handle_movies=False).eligible)
        ok_movie = evaluate_subscription_completion(movie_sub, handle_movies=True)
        self.assertTrue(ok_movie.eligible)
        self.assertEqual(ok_movie.media_kind, "movie")

        bv_sub = {"name": "洗版剧", "type": "电视剧", "season": 1, "best_version": 1}
        self.assertEqual(evaluate_subscription_completion(bv_sub).reason, "skip_best_version")

        vr_sub = {"name": "多版本剧", "type": "电视剧", "season": 1, "version_rules": [{"name": "4K"}]}
        self.assertEqual(evaluate_subscription_completion(vr_sub).reason, "skip_version_rules")


if __name__ == "__main__":
    unittest.main()

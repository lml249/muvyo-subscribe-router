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

    def test_previous_season_eligible_when_next_season_scheduled_in_future(self) -> None:
        sub = {"name": "多季示例剧", "type": "电视剧", "season": 3, "tmdbid": 1007}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "next_episode_to_air": {"season_number": 4, "episode_number": 1, "air_date": "2025-02-01"},
            "season_info": [
                {"season_number": 3, "episode_count": 8, "air_date": "2024-01-10"},
                {"season_number": 4, "episode_count": 8, "air_date": "2025-02-01"},
            ],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 15))
        self.assertTrue(res.eligible)
        self.assertEqual(res.reason, "tv_previous_season_completed")
        self.assertEqual(res.expected_episodes, 8)

    def test_previous_season_not_eligible_when_target_season_not_started_yet(self) -> None:
        sub = {"name": "未来季示例剧", "type": "电视剧", "season": 3, "tmdbid": 1008}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "next_episode_to_air": {"season_number": 4, "episode_number": 1, "air_date": "2025-04-01"},
            "season_info": [
                {"season_number": 3, "episode_count": 8, "air_date": "2025-03-01"},
                {"season_number": 4, "episode_count": 8, "air_date": "2025-04-01"},
            ],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 15))
        self.assertFalse(res.eligible)
        self.assertEqual(res.reason, "tv_ongoing_status:returning series")

    def test_latest_season_quiet_completed_is_eligible(self) -> None:
        sub = {"name": "连载示例剧", "type": "电视剧", "season": 4, "tmdbid": 1009}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "last_air_date": "2024-11-01",
            "next_episode_to_air": None,
            "season_info": [
                {"season_number": 3, "episode_count": 8, "air_date": "2023-01-01"},
                {"season_number": 4, "episode_count": 8, "air_date": "2024-09-01"},
            ],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 1))
        self.assertTrue(res.eligible)
        self.assertEqual(res.reason, "tv_latest_season_quiet_completed")
        self.assertEqual(res.expected_episodes, 8)

    def test_latest_season_recent_air_date_passes_through_to_pt(self) -> None:
        sub = {"name": "连载示例剧", "type": "电视剧", "season": 4, "tmdbid": 1010}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "last_air_date": "2024-12-22",
            "next_episode_to_air": None,
            "season_info": [
                {"season_number": 4, "episode_count": 8, "air_date": "2024-10-01"},
            ],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 1))
        self.assertFalse(res.eligible)
        self.assertEqual(res.reason, "tv_ongoing_status:returning series")

    def test_unstarted_future_season_passes_through_to_pt(self) -> None:
        sub = {"name": "未开播示例剧", "type": "电视剧", "season": 5, "tmdbid": 1011}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 5,
            "last_air_date": "2024-06-01",
            "next_episode_to_air": None,
            "season_info": [
                {"season_number": 4, "episode_count": 8, "air_date": "2024-04-01"},
                {"season_number": 5, "episode_count": 8, "air_date": "2025-02-01"},
            ],
        }
        res = evaluate_subscription_completion(sub, media, today=date(2025, 1, 1))
        self.assertFalse(res.eligible)
        self.assertEqual(res.reason, "tv_ongoing_status:returning series")

    def test_latest_season_quiet_disabled_passes_through_to_pt(self) -> None:
        sub = {"name": "连载示例剧", "type": "电视剧", "season": 4, "tmdbid": 1012}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "last_air_date": "2024-11-01",
            "next_episode_to_air": None,
            "season_info": [
                {"season_number": 4, "episode_count": 8, "air_date": "2024-09-01"},
            ],
        }
        res = evaluate_subscription_completion(
            sub,
            media,
            allow_completed_latest_season=False,
            today=date(2025, 1, 1),
        )
        self.assertFalse(res.eligible)
        self.assertEqual(res.reason, "tv_ongoing_status:returning series")

    def test_latest_season_quiet_days_none_or_empty_falls_back_to_30(self) -> None:
        sub = {"name": "静默天数回退剧", "type": "电视剧", "season": 4, "tmdbid": 1013}
        media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "last_air_date": "2024-11-01",
            "next_episode_to_air": None,
            "season_info": [
                {"season_number": 4, "episode_count": 8, "air_date": "2024-09-01"},
            ],
        }
        for bad_days in (None, "", "invalid", float("inf"), float("-inf")):
            res = evaluate_subscription_completion(
                sub,
                media,
                allow_completed_latest_season=True,
                latest_season_quiet_days=bad_days,  # type: ignore[arg-type]
                today=date(2025, 1, 1),
            )
            self.assertTrue(res.eligible)
            self.assertEqual(res.reason, "tv_latest_season_quiet_completed")

        # Verify threshold genuinely falls back to 30 (not 7): 22 days since last_air_date must stay passthrough
        recent_media = {
            "status": "Returning Series",
            "number_of_seasons": 4,
            "last_air_date": "2024-12-10",
            "next_episode_to_air": None,
            "season_info": [
                {"season_number": 4, "episode_count": 8, "air_date": "2024-10-01"},
            ],
        }
        for inf_days in (float("inf"), float("-inf")):
            res_recent = evaluate_subscription_completion(
                sub,
                recent_media,
                allow_completed_latest_season=True,
                latest_season_quiet_days=inf_days,  # type: ignore[arg-type]
                today=date(2025, 1, 1),
            )
            self.assertFalse(res_recent.eligible)
            self.assertEqual(res_recent.reason, "tv_ongoing_status:returning series")


if __name__ == "__main__":
    unittest.main()

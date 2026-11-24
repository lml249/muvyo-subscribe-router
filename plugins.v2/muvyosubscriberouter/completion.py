from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Optional

ENDED_STATUSES = {"ended", "canceled", "cancelled"}
ONGOING_STATUSES = {"returning series", "in production", "planned", "pilot", "ongoing"}


@dataclass(frozen=True)
class CompletionDecision:
    eligible: bool
    reason: str
    media_kind: str  # "tv" or "movie"
    season: int = 0
    expected_episodes: int = 0
    tmdb_status: str = ""
    title: str = ""
    original_title: str = ""
    year: str = ""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _positive_int(value: Any) -> int:
    try:
        num = int(value)
        return num if num > 0 else 0
    except (TypeError, ValueError):
        return 0


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _normalize_media_kind(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    if raw in {"tv", "电视剧", "剧集", "series", "show"}:
        return "tv"
    if raw in {"movie", "电影", "film"}:
        return "movie"
    return raw


def _find_season_entry(mediainfo: Any, season: int) -> Optional[Dict[str, Any]]:
    season_info = _get(mediainfo, "season_info") or _get(mediainfo, "seasons")
    if isinstance(season_info, list):
        for item in season_info:
            if not isinstance(item, dict):
                continue
            sn = _positive_int(item.get("season_number") or item.get("season"))
            if sn == season:
                return item
    if isinstance(season_info, dict):
        entry = season_info.get(season) or season_info.get(str(season))
        if isinstance(entry, dict):
            return entry
    return None


def _expected_episodes_from_sources(
    subscribe: Any,
    mediainfo: Any,
    season: int,
    season_entry: Optional[Dict[str, Any]],
) -> int:
    if season_entry:
        ep_count = _positive_int(season_entry.get("episode_count"))
        if ep_count:
            return ep_count
    seasons_map = _get(mediainfo, "seasons")
    if isinstance(seasons_map, dict):
        eps = seasons_map.get(season) or seasons_map.get(str(season))
        if isinstance(eps, list) and eps:
            return len(eps)
        if isinstance(eps, int) and eps > 0:
            return eps
    sub_total = _positive_int(_get(subscribe, "total_episode"))
    if sub_total:
        return sub_total
    number_of_episodes = _positive_int(_get(mediainfo, "number_of_episodes"))
    number_of_seasons = _positive_int(_get(mediainfo, "number_of_seasons"))
    if number_of_episodes and number_of_seasons == 1:
        return number_of_episodes
    return 0


def _merge_mediainfo(primary: Any, fallback: Any) -> Dict[str, Any]:
    fields = (
        "status", "next_episode_to_air", "last_air_date", "season_info", "seasons",
        "number_of_seasons", "number_of_episodes", "original_title", "original_name",
        "title", "year", "tmdb_id", "type",
    )
    merged: Dict[str, Any] = {}
    for key in fields:
        val = _get(primary, key)
        if val in (None, "", [], {}):
            val = _get(fallback, key)
        if val not in (None, ""):
            merged[key] = val
    return merged


def evaluate_subscription_completion(
    subscribe: Any,
    mediainfo: Any = None,
    *,
    handle_movies: bool = False,
    skip_best_version: bool = True,
    skip_version_rules: bool = True,
    allow_completed_past_season: bool = True,
    tmdb_fallback_fetcher: Optional[Callable[[int, str], Any]] = None,
    today: Optional[date] = None,
) -> CompletionDecision:
    """
    Conservatively determine whether a newly added MoviePilot subscription should be
    routed to Muvyo/MV3 first. Ongoing current/future seasons and uncertain TV series
    are passed through to PT; strictly past completed seasons of returning series may
    be routed when allow_completed_past_season is True.
    """
    ref_today = today or date.today()
    media_kind = _normalize_media_kind(_get(subscribe, "type") or _get(mediainfo, "type"))
    title = str(_get(subscribe, "name") or _get(mediainfo, "title") or "").strip()
    original_title = str(
        _get(mediainfo, "original_title") or _get(mediainfo, "original_name") or ""
    ).strip()
    year = str(_get(subscribe, "year") or _get(mediainfo, "year") or "").strip()
    season = _positive_int(_get(subscribe, "season"))

    if skip_best_version and bool(_get(subscribe, "best_version")):
        return CompletionDecision(
            eligible=False,
            reason="skip_best_version",
            media_kind=media_kind or "unknown",
            season=season,
            title=title,
            original_title=original_title,
            year=year,
        )

    if skip_version_rules and bool(_get(subscribe, "version_rules")):
        return CompletionDecision(
            eligible=False,
            reason="skip_version_rules",
            media_kind=media_kind or "unknown",
            season=season,
            title=title,
            original_title=original_title,
            year=year,
        )

    if media_kind == "movie":
        if not handle_movies:
            return CompletionDecision(
                eligible=False,
                reason="movies_disabled",
                media_kind="movie",
                title=title,
                original_title=original_title,
                year=year,
            )
        return CompletionDecision(
            eligible=True,
            reason="movie_eligible",
            media_kind="movie",
            expected_episodes=1,
            title=title,
            original_title=original_title,
            year=year,
        )

    if media_kind != "tv":
        return CompletionDecision(
            eligible=False,
            reason=f"unsupported_media_type:{media_kind or 'empty'}",
            media_kind=media_kind or "unknown",
            season=season,
            title=title,
            original_title=original_title,
            year=year,
        )

    tmdbid = _positive_int(_get(subscribe, "tmdbid") or _get(mediainfo, "tmdb_id"))
    target_season = season or 1
    needs_tmdb_enrich = (
        mediainfo is None
        or not str(_get(mediainfo, "status") or "").strip()
        or _find_season_entry(mediainfo, target_season) is None
    )
    if needs_tmdb_enrich and tmdbid and tmdb_fallback_fetcher is not None:
        fetched = tmdb_fallback_fetcher(tmdbid, "tv")
        if fetched is not None:
            mediainfo = _merge_mediainfo(mediainfo, fetched) if mediainfo is not None else fetched
            original_title = original_title or str(
                _get(mediainfo, "original_title") or _get(mediainfo, "original_name") or ""
            ).strip()
            year = year or str(_get(mediainfo, "year") or "").strip()

    if mediainfo is None:
        return CompletionDecision(
            eligible=False,
            reason="missing_mediainfo_conservative_passthrough",
            media_kind="tv",
            season=season,
            title=title,
            original_title=original_title,
            year=year,
        )

    status = str(_get(mediainfo, "status") or "").strip()
    status_lower = status.lower()
    next_ep = _get(mediainfo, "next_episode_to_air")
    next_ep_season = _positive_int(_get(next_ep, "season_number")) if next_ep else 0
    last_air_date = _parse_date(_get(mediainfo, "last_air_date"))

    season_entry = _find_season_entry(mediainfo, target_season)
    expected_episodes = _expected_episodes_from_sources(
        subscribe, mediainfo, target_season, season_entry
    )

    # 1. Explicitly ongoing / returning series: only allow if requesting a strictly older season
    # that is not the next_episode_to_air season and has a known newer season already aired.
    if next_ep and (next_ep_season == 0 or next_ep_season == target_season):
        return CompletionDecision(
            eligible=False,
            reason="next_episode_to_air_in_target_season",
            media_kind="tv",
            season=target_season,
            expected_episodes=expected_episodes,
            tmdb_status=status,
            title=title,
            original_title=original_title,
            year=year,
        )

    if status_lower in ENDED_STATUSES:
        if expected_episodes <= 0:
            return CompletionDecision(
                eligible=False,
                reason="expected_episodes_unknown",
                media_kind="tv",
                season=target_season,
                expected_episodes=0,
                tmdb_status=status,
                title=title,
                original_title=original_title,
                year=year,
            )
        if last_air_date and last_air_date > ref_today:
            return CompletionDecision(
                eligible=False,
                reason="last_air_date_in_future",
                media_kind="tv",
                season=target_season,
                expected_episodes=expected_episodes,
                tmdb_status=status,
                title=title,
                original_title=original_title,
                year=year,
            )
        return CompletionDecision(
            eligible=True,
            reason="tv_series_ended",
            media_kind="tv",
            season=target_season,
            expected_episodes=expected_episodes,
            tmdb_status=status,
            title=title,
            original_title=original_title,
            year=year,
        )

    if status_lower in ONGOING_STATUSES:
        number_of_seasons = _positive_int(_get(mediainfo, "number_of_seasons"))
        next_season_entry = _find_season_entry(mediainfo, target_season + 1)
        next_season_air_date = _parse_date(_get(next_season_entry, "air_date")) if next_season_entry else None
        # Only consider a past season finished if a strictly later season already started airing
        # and next_episode_to_air is not in target_season.
        if (
            allow_completed_past_season
            and target_season > 0
            and number_of_seasons > target_season
            and expected_episodes > 0
            and next_season_air_date is not None
            and next_season_air_date <= ref_today
            and (not next_ep or next_ep_season > target_season)
        ):
            return CompletionDecision(
                eligible=True,
                reason="tv_previous_season_completed",
                media_kind="tv",
                season=target_season,
                expected_episodes=expected_episodes,
                tmdb_status=status,
                title=title,
                original_title=original_title,
                year=year,
            )
        return CompletionDecision(
            eligible=False,
            reason=f"tv_ongoing_status:{status_lower}",
            media_kind="tv",
            season=target_season,
            expected_episodes=expected_episodes,
            tmdb_status=status,
            title=title,
            original_title=original_title,
            year=year,
        )

    return CompletionDecision(
        eligible=False,
        reason=f"tv_status_unknown:{status or 'empty'}",
        media_kind="tv",
        season=target_season,
        expected_episodes=expected_episodes,
        tmdb_status=status,
        title=title,
        original_title=original_title,
        year=year,
    )

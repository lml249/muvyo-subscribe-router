from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client import MuvyoClient
from .redaction import short_hash

VIDEO_EXTENSIONS = {
    ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".rmvb", ".ts", ".webm", ".wmv",
}
SUBTITLE_SIDECAR_EXTENSIONS = {
    ".ass", ".idx", ".srt", ".ssa", ".sub", ".sup", ".vtt",
}
METADATA_SIDECAR_EXTENSIONS = {
    ".jpeg", ".jpg", ".nfo", ".png", ".webp",
}

_SEASON_EP_RE = re.compile(r"(?i)(?:^|[^a-z0-9])S(\d{1,2})\s*[._ -]*E(\d{1,4})(?:[^0-9]|$)")
_X_EP_RE = re.compile(r"(?i)(?:^|[^a-z0-9])(\d{1,2})x(\d{1,4})(?:[^0-9]|$)")
_EP_ONLY_RE = re.compile(r"(?i)(?:^|[^a-z0-9])(?:EP?|Episode)\s*[._ -]*(\d{1,4})(?:[^0-9]|$)")
_CN_EP_RE = re.compile(r"第\s*(\d{1,4})\s*[集话話]")
_SEASON_ONLY_RE = re.compile(r"(?i)(?:^|[^a-z0-9])(?:Season\s*|S)(\d{1,2})(?:[^0-9]|$)")
_SEASON_RANGE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])S(\d{1,2})\s*[-~～到至]\s*S?(\d{1,2})(?:[^0-9]|$)")
_CN_SEASON_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]{1,3})\s*季")
_CN_SEASON_RANGE_RE = re.compile(r"(?:第?\s*(\d{1,2})\s*[-~～到至]\s*(\d{1,2})\s*季)")
_MULTI_SEASON_HINT_RE = re.compile(r"(?i)(全\s*\d+\s*季|全季|合集|complete\s*series|all\s*seasons)")
_BRACKET_EP_RE = re.compile(r"[\[\(【（]\s*(\d{1,3})\s*[\]\)】）]")

_CN_DIGIT_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

_QUALITY_SCORES = (
    ("remux", 45),
    ("2160p", 40),
    ("4k", 38),
    ("bluray", 35),
    ("blu-ray", 35),
    ("web-dl", 30),
    ("webdl", 30),
    ("1080p", 25),
    ("720p", 10),
)


@dataclass
class SelectedSharePlan:
    title: str
    title_hash: str
    share_code: str
    receive_code: str
    browse_cid: str
    file_ids: List[str]
    video_count: int
    subtitle_sidecar_count: int
    excluded_metadata_count: int
    episodes: List[int] = field(default_factory=list)
    score: int = 0
    matched_keyword: str = ""
    warnings: List[str] = field(default_factory=list)


def item_name(row: Dict[str, Any]) -> str:
    for key in ("name", "fn", "file_name", "filename", "n", "title"):
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def item_file_id(row: Dict[str, Any]) -> str:
    for key in ("file_id", "fid", "cid", "folder_id", "id"):
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def item_is_folder(row: Dict[str, Any]) -> bool:
    if isinstance(row.get("is_dir"), bool):
        return bool(row["is_dir"])
    if isinstance(row.get("is_folder"), bool):
        return bool(row["is_folder"])
    kind = str(row.get("type") or row.get("kind") or row.get("category") or "").strip().lower()
    if kind in {"folder", "dir", "directory"}:
        return True
    if kind in {"file", "video", "subtitle"}:
        return False
    fc = row.get("fc")
    if fc is not None and not isinstance(fc, bool):
        fc_str = str(fc).strip()
        if fc_str == "0":
            return True
        if fc_str == "1":
            return False
    if row.get("fid") not in (None, "") or row.get("file_id") not in (None, ""):
        return False
    if row.get("cid") not in (None, "") or row.get("folder_id") not in (None, ""):
        ext = os.path.splitext(item_name(row))[1].lower()
        return ext not in VIDEO_EXTENSIONS and ext not in SUBTITLE_SIDECAR_EXTENSIONS
    return False


def classify_media_kind(name: str) -> str:
    ext = os.path.splitext(str(name or "").strip())[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in SUBTITLE_SIDECAR_EXTENSIONS:
        return "subtitle_sidecar"
    if ext in METADATA_SIDECAR_EXTENSIONS:
        return "metadata_sidecar"
    return "other"


def extract_season_and_episode(text: str) -> Tuple[Optional[int], Optional[int]]:
    raw = str(text or "")
    m = _SEASON_EP_RE.search(raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _X_EP_RE.search(raw)
    if m:
        return int(m.group(1)), int(m.group(2))
    season = extract_season_only(raw)
    m = _CN_EP_RE.search(raw)
    if m:
        return season, int(m.group(1))
    m = _EP_ONLY_RE.search(raw)
    if m:
        return season, int(m.group(1))
    m = _BRACKET_EP_RE.search(raw)
    if m:
        ep = int(m.group(1))
        if 1 <= ep <= 999:
            return season, ep
    return season, None


def _parse_cn_season_num(token: str) -> Optional[int]:
    if token.isdigit():
        return int(token)
    if token in _CN_DIGIT_MAP:
        return _CN_DIGIT_MAP[token]
    if token.startswith("十") and len(token) == 2 and token[1] in _CN_DIGIT_MAP:
        return 10 + _CN_DIGIT_MAP[token[1]]
    return None


def extract_season_only(text: str) -> Optional[int]:
    seasons = extract_explicit_seasons(text)
    if len(seasons) == 1:
        return next(iter(seasons))
    return None


def extract_explicit_seasons(text: str) -> set[int]:
    raw = str(text or "")
    found: set[int] = set()
    for m in _SEASON_RANGE_RE.finditer(raw):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 1 <= lo <= hi <= 50:
            found.update(range(lo, hi + 1))
    for m in _CN_SEASON_RANGE_RE.finditer(raw):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 1 <= lo <= hi <= 50:
            found.update(range(lo, hi + 1))
    for m in _SEASON_ONLY_RE.finditer(raw):
        found.add(int(m.group(1)))
    for m in _CN_SEASON_RE.finditer(raw):
        num = _parse_cn_season_num(m.group(1))
        if num is not None:
            found.add(num)
    return found


def text_conflicts_with_season(text: str, target_season: int) -> bool:
    if target_season <= 0:
        return False
    seasons = extract_explicit_seasons(text)
    if not seasons:
        return False
    if target_season in seasons:
        return False
    if _MULTI_SEASON_HINT_RE.search(str(text or "")) and len(seasons) > 1:
        return False
    return True


def _normalize_match_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "").casefold())


def _title_matches(candidate_title: str, expected_titles: Sequence[str]) -> bool:
    norm_cand = _normalize_match_text(candidate_title)
    if not norm_cand:
        return False
    for exp in expected_titles:
        norm_exp = _normalize_match_text(exp)
        if norm_exp and (norm_exp in norm_cand or norm_cand in norm_exp):
            return True
    return False


def _quality_score(text: str) -> int:
    lowered = str(text or "").lower()
    best = 0
    for token, score in _QUALITY_SCORES:
        if token in lowered:
            best = max(best, score)
    return best


def build_search_keywords(
    title: str,
    *,
    original_title: str = "",
    year: str = "",
    media_kind: str = "tv",
    season: int = 0,
) -> List[str]:
    candidates: List[str] = []
    clean_title = str(title or "").strip()
    clean_orig = str(original_title or "").strip()
    clean_year = str(year or "").strip()
    if clean_title:
        if media_kind == "tv" and season > 1:
            candidates.append(f"{clean_title} 第{season}季")
            candidates.append(f"{clean_title} S{season:02d}")
        candidates.append(clean_title)
        if clean_year:
            candidates.append(f"{clean_title} {clean_year}")
    if clean_orig and clean_orig.casefold() != clean_title.casefold():
        if media_kind == "tv" and season > 1:
            candidates.append(f"{clean_orig} S{season:02d}")
        if clean_year:
            candidates.append(f"{clean_orig} {clean_year}")
        candidates.append(clean_orig)
    deduped: List[str] = []
    seen = set()
    for kw in candidates:
        key = kw.casefold()
        if kw and key not in seen:
            seen.add(key)
            deduped.append(kw)
    return deduped


def _pick_drill_folder(
    folders: List[Dict[str, Any]],
    *,
    media_kind: str,
    target_season: int,
) -> Optional[Dict[str, Any]]:
    if not folders:
        return None
    if media_kind == "tv" and target_season > 0:
        for folder in folders:
            seasons = extract_explicit_seasons(item_name(folder))
            if target_season in seasons:
                return folder
    if len(folders) == 1:
        sole = folders[0]
        if media_kind == "tv" and target_season > 0 and text_conflicts_with_season(item_name(sole), target_season):
            return None
        return sole
    return None


def _evaluate_browse_items(
    items: List[Dict[str, Any]],
    *,
    media_kind: str,
    target_season: int,
    expected_episodes: int,
    inherited_season: Optional[int] = None,
) -> Tuple[bool, List[str], List[int], int, int, int, str]:
    videos: List[Dict[str, Any]] = []
    subtitles: List[Dict[str, Any]] = []
    metadata_count = 0
    for row in items:
        if item_is_folder(row):
            continue
        kind = classify_media_kind(item_name(row))
        if kind == "video":
            videos.append(row)
        elif kind == "subtitle_sidecar":
            subtitles.append(row)
        elif kind == "metadata_sidecar":
            metadata_count += 1

    if not videos:
        return False, [], [], 0, len(subtitles), metadata_count, "no_video_files"

    if media_kind == "movie":
        # Pick largest video file if multiple (e.g. sample + main feature) or all videos if single
        if len(videos) > 1:
            videos = sorted(videos, key=lambda r: int(r.get("size") or r.get("s") or 0), reverse=True)[:1]
        selected_rows = videos + subtitles
        fids = [item_file_id(r) for r in selected_rows if item_file_id(r)]
        if not fids:
            return False, [], [], len(videos), len(subtitles), metadata_count, "missing_file_ids"
        return True, fids, [1], len(videos), len(subtitles), metadata_count, "ok"

    # TV series: validate inherited season context, per-file season consistency and episode coverage
    if inherited_season is not None and target_season > 0 and inherited_season != target_season:
        return False, [], [], 0, len(subtitles), metadata_count, "wrong_season"

    matched_videos: List[Dict[str, Any]] = []
    episodes_found: set[int] = set()
    wrong_season_count = 0
    for row in videos:
        sn, ep = extract_season_and_episode(item_name(row))
        effective_season = sn if sn is not None else inherited_season
        if effective_season is not None and target_season > 0 and effective_season != target_season:
            wrong_season_count += 1
            continue
        if ep is not None and ep > 0:
            matched_videos.append(row)
            episodes_found.add(ep)

    if wrong_season_count and not matched_videos:
        return False, [], [], 0, len(subtitles), metadata_count, "wrong_season"
    if not matched_videos or not episodes_found:
        return False, [], [], 0, len(subtitles), metadata_count, "episodes_unrecognized"

    sorted_eps = sorted(episodes_found)
    if expected_episodes > 0:
        expected_set = set(range(1, expected_episodes + 1))
        missing = expected_set - episodes_found
        if missing:
            return False, [], sorted_eps, len(matched_videos), len(subtitles), metadata_count, "episodes_incomplete"
        # Filter to only expected season videos + subtitle sidecars
        kept_videos = [
            r for r in matched_videos
            if (extract_season_and_episode(item_name(r))[1] in expected_set)
        ]
        # Avoid duplicate files for the same episode by keeping the largest per episode
        best_by_ep: Dict[int, Dict[str, Any]] = {}
        for r in kept_videos:
            _, ep = extract_season_and_episode(item_name(r))
            if ep is None:
                continue
            prev = best_by_ep.get(ep)
            if prev is None or int(r.get("size") or r.get("s") or 0) > int(prev.get("size") or prev.get("s") or 0):
                best_by_ep[ep] = r
        matched_videos = [best_by_ep[ep] for ep in sorted(best_by_ep)]
        sorted_eps = sorted(best_by_ep)
    else:
        # Must be contiguous starting at 1
        if sorted_eps[0] != 1 or len(sorted_eps) != sorted_eps[-1]:
            return False, [], sorted_eps, len(matched_videos), len(subtitles), metadata_count, "episode_gap_detected"

    selected_rows = matched_videos + subtitles
    fids = [item_file_id(r) for r in selected_rows if item_file_id(r)]
    if len(fids) < len(matched_videos):
        return False, [], sorted_eps, len(matched_videos), len(subtitles), metadata_count, "missing_file_ids"
    return True, fids, sorted_eps, len(matched_videos), len(subtitles), metadata_count, "ok"


def select_best_share_candidate(
    client: MuvyoClient,
    *,
    title: str,
    original_title: str = "",
    year: str = "",
    media_kind: str = "tv",
    season: int = 1,
    expected_episodes: int = 0,
    channels: Optional[List[str]] = None,
    max_candidates: int = 8,
    max_drill_depth: int = 3,
) -> Tuple[Optional[SelectedSharePlan], List[Dict[str, Any]]]:
    """
    Search Muvyo resources, parse & browse candidate shares (with bounded folder drill-down),
    exclude metadata sidecars, verify season/episode completeness, and return the best plan.
    """
    keywords = build_search_keywords(
        title,
        original_title=original_title,
        year=year,
        media_kind=media_kind,
        season=season,
    )
    expected_titles = [t for t in (title, original_title) if t]
    diagnostics: List[Dict[str, Any]] = []
    best_plan: Optional[SelectedSharePlan] = None
    inspected_shares: set[str] = set()
    candidates_checked = 0

    for kw in keywords:
        search_res = client.search_resources(kw, channels=channels)
        if not search_res.get("ok"):
            diagnostics.append({"keyword": kw, "stage": "search", "reason": "search_failed"})
            continue
        for raw_item in search_res.get("items", []):
            if candidates_checked >= max(1, int(max_candidates)):
                break
            cand_title = str(
                raw_item.get("title") or raw_item.get("name") or raw_item.get("resource_name") or ""
            ).strip()
            drive_raw = (
                str(raw_item.get("drive_type") or "").strip()
                or str(raw_item.get("pan_type") or "").strip()
            )
            if drive_raw and "115" not in drive_raw.lower():
                diagnostics.append({
                    "keyword": kw,
                    "title_hash": short_hash(cand_title or drive_raw),
                    "stage": "filter",
                    "reason": "non_115_drive",
                })
                continue

            if expected_titles and cand_title and not _title_matches(cand_title, expected_titles):
                diagnostics.append({
                    "keyword": kw,
                    "title_hash": short_hash(cand_title),
                    "stage": "filter",
                    "reason": "title_mismatch",
                })
                continue

            if media_kind == "tv" and season > 0 and cand_title and text_conflicts_with_season(cand_title, season):
                diagnostics.append({
                    "keyword": kw,
                    "title_hash": short_hash(cand_title),
                    "stage": "filter",
                    "reason": "wrong_season_title",
                })
                continue

            share_url = str(
                raw_item.get("share_url") or raw_item.get("share_link") or raw_item.get("url") or raw_item.get("link") or ""
            ).strip()
            share_code = str(raw_item.get("share_code") or raw_item.get("shareId") or raw_item.get("share_id") or "").strip()
            receive_code = str(
                raw_item.get("receive_code") or raw_item.get("receiveCode") or raw_item.get("password") or raw_item.get("pwd") or ""
            ).strip()

            dedup_key = short_hash(share_url or share_code)
            if not dedup_key or dedup_key in inspected_shares:
                continue
            inspected_shares.add(dedup_key)
            candidates_checked += 1

            if share_url:
                parsed = client.parse_share(share_url, receive_code=receive_code)
                if parsed.get("ok"):
                    share_code = str(parsed.get("share_code") or "") or share_code
                    receive_code = str(parsed.get("receive_code") or "") or receive_code
                elif not share_code:
                    diagnostics.append({"keyword": kw, "share_hash": dedup_key, "stage": "parse", "reason": "parse_failed"})
                    continue

            if not share_code:
                diagnostics.append({"keyword": kw, "share_hash": dedup_key, "stage": "parse", "reason": "no_share_code"})
                continue

            cid = ""
            inherited_season: Optional[int] = extract_season_only(cand_title) if media_kind == "tv" else None
            matched_result = None
            last_reason = "empty_share"
            for _depth in range(max(1, int(max_drill_depth)) + 1):
                browse_res = client.browse_share(share_code, receive_code=receive_code, cid=cid)
                if not browse_res.get("ok"):
                    last_reason = "browse_failed"
                    break
                rows = browse_res.get("items", [])
                ok, fids, eps, v_cnt, s_cnt, m_cnt, reason = _evaluate_browse_items(
                    rows,
                    media_kind=media_kind,
                    target_season=season,
                    expected_episodes=expected_episodes,
                    inherited_season=inherited_season,
                )
                if ok:
                    matched_result = (cid, fids, eps, v_cnt, s_cnt, m_cnt)
                    break
                last_reason = reason
                folders = [r for r in rows if item_is_folder(r)]
                next_folder = _pick_drill_folder(folders, media_kind=media_kind, target_season=season)
                if not next_folder:
                    break
                next_cid = item_file_id(next_folder)
                if not next_cid or next_cid == cid:
                    break
                folder_season = extract_season_only(item_name(next_folder))
                if folder_season is not None:
                    inherited_season = folder_season
                cid = next_cid

            if not matched_result:
                diagnostics.append({"keyword": kw, "share_hash": dedup_key, "stage": "browse", "reason": last_reason})
                continue

            used_cid, fids, eps, v_cnt, s_cnt, m_cnt = matched_result
            score = 100 + _quality_score(cand_title)
            if media_kind == "tv" and season > 0 and extract_season_only(cand_title) == season:
                score += 20
            if s_cnt > 0:
                score += 5
            plan = SelectedSharePlan(
                title=cand_title,
                title_hash=short_hash(cand_title or share_code),
                share_code=share_code,
                receive_code=receive_code,
                browse_cid=used_cid,
                file_ids=fids,
                video_count=v_cnt,
                subtitle_sidecar_count=s_cnt,
                excluded_metadata_count=m_cnt,
                episodes=eps,
                score=score,
                matched_keyword=kw,
            )
            if best_plan is None or plan.score > best_plan.score:
                best_plan = plan

        if best_plan is not None and best_plan.score >= 135:
            break

    return best_plan, diagnostics

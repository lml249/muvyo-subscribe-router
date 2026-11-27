from __future__ import annotations

import inspect
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .client import MuvyoClient, MuvyoClientError
from .completion import CompletionDecision
from .redaction import redact_exception, redact_sensitive_data, redact_sensitive_text, short_hash
from .selector import (
    classify_media_kind,
    extract_season_and_episode,
    item_file_id,
    item_is_folder,
    item_name,
    select_best_share_candidate,
)
from .subtitle_gate import ProbeRunner, evaluate_embedded_subtitle_gate

CATEGORY_SUFFIXES = {"series", "tv", "movies", "movie", "anime", "电视剧", "电影"}
NON_SHOW_CATEGORY_DIRS = {
    "anime",
    "series",
    "tv",
    "movies",
    "movie",
    "kids",
    "documentary",
    "documentaries",
    "variety",
    "电视剧",
    "电影",
    "动漫",
    "动画",
    "剧集",
    "儿童",
    "综艺",
    "纪录片",
}
SEASON_DIR_RE = re.compile(
    r"^(?:season[\s._-]*\d+|s\d{1,3}|specials?|第\s*\d+\s*季)$",
    re.IGNORECASE,
)


def normalize_cloud_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    parts = [p for p in raw.split("/") if p]
    return "/" + "/".join(parts) if parts else ""


def looks_like_cloud_media_path(path: str) -> bool:
    norm = normalize_cloud_path(path)
    if not norm:
        return False
    parts = [p for p in norm.split("/") if p]
    lower_parts = [p.lower() for p in parts]
    lowered = norm.lower()
    if "已整理" in parts or "未整理" in parts:
        return True
    if "/media/cloud-media" in lowered or "/cloud/media" in lowered or "/115/media" in lowered:
        return True
    if "cloud-media" in lower_parts or "115-media" in lower_parts:
        return True
    return False


def looks_like_strm_side_path(path: str) -> bool:
    norm = normalize_cloud_path(path)
    if not norm or looks_like_cloud_media_path(norm):
        return False
    parts = [p.lower() for p in norm.split("/") if p]
    lowered = norm.lower()
    if "strm" in parts:
        return True
    if any(
        p.startswith(("strm-", "strm_"))
        or p.endswith(("-strm", "_strm"))
        or "-strm-" in p
        or "_strm_" in p
        for p in parts
    ):
        return True
    return "/cloud-strm" in lowered or "/mv3/strm" in lowered


def _has_dot_segments(path: str) -> bool:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return False
    return any(seg in {".", ".."} for seg in raw.split("/"))


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in str(text or ""))


def _validate_optional_strm_root(raw_path: str, field_name: str, errors: List[str]) -> None:
    raw = str(raw_path or "").strip()
    if not raw:
        return
    if _has_dot_segments(raw):
        errors.append(f"{field_name}_must_not_contain_dot_segments")
    norm = normalize_cloud_path(raw)
    if not norm:
        errors.append(f"{field_name}_must_be_explicit_strm_path")
        return
    if looks_like_cloud_media_path(norm):
        errors.append(f"{field_name}_must_not_be_cloud_media_path")
    if not looks_like_strm_side_path(norm):
        errors.append(f"{field_name}_must_be_explicit_strm_path")
    last_seg = norm.rsplit("/", 1)[-1].lower()
    if last_seg in CATEGORY_SUFFIXES:
        errors.append(f"{field_name}_must_not_append_category")


def validate_router_paths(
    staging_root: str,
    cloud_organize_root: str,
    mv3_strm_root: str,
    local_strm_root: str = "",
    emby_strm_root: str = "",
) -> List[str]:
    errors: List[str] = []
    if _has_dot_segments(staging_root):
        errors.append("staging_root_must_not_contain_dot_segments")
    if _has_dot_segments(cloud_organize_root):
        errors.append("cloud_organize_root_must_not_contain_dot_segments")
    if _has_dot_segments(mv3_strm_root):
        errors.append("mv3_strm_root_must_not_contain_dot_segments")

    s_root = normalize_cloud_path(staging_root)
    o_root = normalize_cloud_path(cloud_organize_root)
    strm_root = normalize_cloud_path(mv3_strm_root)

    if not s_root:
        errors.append("staging_root_required")
    elif looks_like_strm_side_path(s_root):
        errors.append("staging_root_must_not_be_strm_side")

    if not o_root:
        errors.append("cloud_organize_root_required")
    else:
        if looks_like_strm_side_path(o_root):
            errors.append("cloud_organize_root_must_not_be_strm_side")
        last_seg = o_root.rsplit("/", 1)[-1].lower()
        if last_seg in CATEGORY_SUFFIXES:
            errors.append("cloud_organize_root_must_not_append_category")

    if not strm_root:
        errors.append("mv3_strm_root_required")
    else:
        if looks_like_cloud_media_path(strm_root):
            errors.append("mv3_strm_root_must_not_be_cloud_media_path")
        if not looks_like_strm_side_path(strm_root):
            errors.append("mv3_strm_root_must_be_explicit_strm_path")
        last_strm = strm_root.rsplit("/", 1)[-1].lower()
        if last_strm in CATEGORY_SUFFIXES:
            errors.append("mv3_strm_root_must_not_append_category")

    _validate_optional_strm_root(local_strm_root, "local_strm_root", errors)
    _validate_optional_strm_root(emby_strm_root, "emby_strm_root", errors)

    return sorted(set(errors))


def inspect_staging_root_warnings(staging_root: str) -> List[str]:
    norm = normalize_cloud_path(staging_root)
    if not norm:
        return []
    parts = [p for p in norm.split("/") if p]
    warnings: List[str] = []
    if parts and parts[0] == "未整理":
        warnings.append("staging_root_under_unorganized_monitor_risk")
    if any(seg.lower() == "mp-router" or seg.lower().endswith("-router") for seg in parts):
        warnings.append("staging_root_contains_router_suffix_risk")
    return warnings


def is_path_strictly_within_root(target_path: str, root_path: str) -> bool:
    raw_target = str(target_path or "").strip()
    raw_root = str(root_path or "").strip()
    if not raw_target or not raw_root:
        return False
    if _has_control_chars(raw_target) or _has_control_chars(raw_root):
        return False
    if _has_dot_segments(raw_target) or _has_dot_segments(raw_root):
        return False
    try:
        real_root = os.path.realpath(raw_root)
        real_target = os.path.realpath(raw_target)
        if not real_root or not real_target or real_target == real_root:
            return False
        return os.path.commonpath([real_root, real_target]) == real_root
    except (OSError, ValueError):
        return False


def _extract_safe_rel_path(full_path: str, prefix_root: str) -> str:
    raw = str(full_path or "").strip()
    if not raw or not prefix_root or _has_control_chars(raw) or _has_dot_segments(raw):
        return ""
    norm_p = normalize_cloud_path(raw)
    if not norm_p or not norm_p.startswith(prefix_root + "/"):
        return ""
    rel = norm_p[len(prefix_root) + 1:].strip("/")
    if not rel or _has_control_chars(rel) or _has_dot_segments(rel):
        return ""
    segs = [s for s in rel.split("/") if s]
    if not segs or any(s in {".", ".."} for s in segs):
        return ""
    return "/".join(segs)


def is_pure_category_rel_path(rel_path: str) -> bool:
    segs = [s.strip().lower() for s in str(rel_path or "").split("/") if s.strip()]
    return bool(segs) and all(seg in NON_SHOW_CATEGORY_DIRS for seg in segs)


def _dedupe_preserve_order(items: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def resolve_post_process_strm_targets(
    *,
    resolved_strm_dirs: Sequence[str],
    resolved_target_dirs: Sequence[str],
    mv3_strm_root: str,
    cloud_organize_root: str,
    local_strm_root: str,
    emby_strm_root: str = "",
    media_kind: str = "tv",
) -> Dict[str, Any]:
    norm_mv3_strm = normalize_cloud_path(mv3_strm_root)
    norm_cloud_org = normalize_cloud_path(cloud_organize_root)
    norm_local_strm = normalize_cloud_path(local_strm_root)
    norm_emby_strm = normalize_cloud_path(emby_strm_root) or norm_local_strm

    if not norm_local_strm:
        return {
            "targets": [],
            "scrape_dirs": [],
            "emby_refresh_dirs": [],
            "warnings": ["strm_post_process_skipped_local_strm_root_empty"],
        }

    strm_rel_paths: List[str] = []
    for raw_strm in resolved_strm_dirs or ():
        rel = _extract_safe_rel_path(raw_strm, norm_mv3_strm)
        if rel and not is_pure_category_rel_path(rel):
            strm_rel_paths.append(rel)

    if strm_rel_paths:
        rel_paths = _dedupe_preserve_order(strm_rel_paths)
    else:
        cloud_rel_paths: List[str] = []
        for raw_target in resolved_target_dirs or ():
            rel = _extract_safe_rel_path(raw_target, norm_cloud_org)
            if rel and not is_pure_category_rel_path(rel):
                cloud_rel_paths.append(rel)
        rel_paths = _dedupe_preserve_order(cloud_rel_paths)

    targets_by_scrape_dir: Dict[str, Dict[str, Any]] = {}
    target_order: List[str] = []
    kind_norm = str(media_kind or "tv").strip().lower()

    for rel in rel_paths:
        segs = rel.split("/")
        if is_pure_category_rel_path(rel):
            continue

        scrape_rel = rel
        refresh_rels: List[str] = [rel]
        if kind_norm == "tv" and len(segs) >= 2 and SEASON_DIR_RE.match(segs[-1].strip()):
            parent_segs = segs[:-1]
            parent_tail = parent_segs[-1].strip().lower()
            parent_rel = "/".join(parent_segs)
            # Only hoist to show root when parent is a real show directory, never a category root.
            if (
                parent_tail
                and parent_tail not in NON_SHOW_CATEGORY_DIRS
                and not is_pure_category_rel_path(parent_rel)
            ):
                scrape_rel = parent_rel
                refresh_rels = [scrape_rel, rel]

        scrape_dir = f"{norm_local_strm}/{scrape_rel}"
        local_dirs = [f"{norm_local_strm}/{r}" for r in refresh_rels]
        emby_dirs = [f"{norm_emby_strm}/{r}" for r in refresh_rels]

        if scrape_dir not in targets_by_scrape_dir:
            targets_by_scrape_dir[scrape_dir] = {
                "scrape_dir": scrape_dir,
                "settle_dirs": [],
                "local_refresh_dirs": [],
                "emby_refresh_dirs": [],
            }
            target_order.append(scrape_dir)

        entry = targets_by_scrape_dir[scrape_dir]
        entry["settle_dirs"] = _dedupe_preserve_order(entry["settle_dirs"] + local_dirs)
        entry["local_refresh_dirs"] = _dedupe_preserve_order(entry["local_refresh_dirs"] + local_dirs)
        entry["emby_refresh_dirs"] = _dedupe_preserve_order(entry["emby_refresh_dirs"] + emby_dirs)

    targets = [targets_by_scrape_dir[k] for k in target_order]
    scrape_dirs = _dedupe_preserve_order([t["scrape_dir"] for t in targets])
    emby_refresh_dirs = _dedupe_preserve_order(
        [p for t in targets for p in t["emby_refresh_dirs"]]
    )

    if not targets:
        return {
            "targets": [],
            "scrape_dirs": [],
            "emby_refresh_dirs": [],
            "warnings": ["strm_post_process_skipped_missing_scoped_target_dir"],
        }

    return {
        "targets": targets,
        "scrape_dirs": scrape_dirs,
        "emby_refresh_dirs": emby_refresh_dirs,
        "warnings": [],
    }


def _safe_folder_segment(text: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or "media")[:max_len]


@dataclass
class RouterPipelineConfig:
    staging_root: str = "/mv_staging"
    cloud_organize_root: str = "/已整理"
    mv3_strm_root: str = "/strm"
    local_strm_root: str = ""
    emby_strm_root: str = ""
    auto_scrape_strm: bool = True
    auto_refresh_emby: bool = True
    subtitle_policy: str = "auto"
    ffprobe_binary: str = ""
    ffprobe_timeout: int = 45
    ffprobe_concurrency: int = 2
    search_channels: List[str] = field(default_factory=list)
    max_candidates: int = 8
    max_drill_depth: int = 3
    trigger_strm_generate: bool = False
    dry_run: bool = False
    staging_settle_max_attempts: int = 6
    staging_settle_interval_seconds: float = 2.0
    strm_settle_max_attempts: int = 5
    strm_settle_interval_seconds: float = 2.0


@dataclass
class PipelineResult:
    status: str  # "routed_ok", "dry_run_matched", "fallback_pt", "manual_review_staging", "config_error"
    reason: str
    staging_path: str = ""
    video_count: int = 0
    episodes: List[int] = field(default_factory=list)
    subtitle_audit: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_sanitized_dict(self, extra_secrets: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": redact_sensitive_text(self.reason, max_length=300, extra_secrets=extra_secrets),
            "staging_path": redact_sensitive_text(self.staging_path, max_length=300, extra_secrets=extra_secrets),
            "video_count": self.video_count,
            "episodes": list(self.episodes),
            "subtitle_audit": {
                "ok": self.subtitle_audit.get("ok"),
                "policy": self.subtitle_audit.get("policy"),
                "passed_count": self.subtitle_audit.get("passed_count", 0),
                "failed_count": self.subtitle_audit.get("failed_count", 0),
                "exempt_chinese_audio_count": self.subtitle_audit.get("exempt_chinese_audio_count", 0),
                "blockers": redact_sensitive_data(
                    list(self.subtitle_audit.get("blockers", [])),
                    extra_secrets=extra_secrets,
                ),
            } if self.subtitle_audit else {},
            "diagnostics": redact_sensitive_data(self.diagnostics, extra_secrets=extra_secrets),
        }


def run_muvyo_routing_pipeline(
    client: MuvyoClient,
    decision: CompletionDecision,
    *,
    tmdb_id: int,
    config: RouterPipelineConfig,
    run_id: str = "",
    probe_runner: Optional[ProbeRunner] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    scrape_runner: Optional[Callable[[str, int, str], Dict[str, Any]]] = None,
    emby_refresh_runner: Optional[Callable[[List[str]], Dict[str, Any]]] = None,
    path_exists_fn: Optional[Callable[[str], bool]] = None,
) -> PipelineResult:
    """
    Execute the end-to-end Muvyo routing pipeline for a single completed subscription:
    Path Guard -> Search/Select -> Ensure Staging -> Receive -> Browse Staging ->
    Embedded Subtitle Gate -> Organize Transfer -> Optional STRM Generate ->
    Optional Local STRM Scrape & Scoped Emby Refresh.
    Never deletes staging files on failure.
    """
    def _stopped() -> bool:
        return bool(should_stop is not None and should_stop())

    if _stopped():
        return PipelineResult(status="aborted_stopped", reason="plugin_stopped_before_start")

    path_errors = validate_router_paths(
        config.staging_root,
        config.cloud_organize_root,
        config.mv3_strm_root,
        local_strm_root=config.local_strm_root,
        emby_strm_root=config.emby_strm_root,
    )
    if path_errors:
        return PipelineResult(
            status="config_error",
            reason="invalid_router_paths:" + ",".join(path_errors),
            diagnostics={"path_errors": path_errors},
        )

    staging_warnings = inspect_staging_root_warnings(config.staging_root)

    def _with_staging_warnings(diag: Dict[str, Any]) -> Dict[str, Any]:
        if staging_warnings:
            merged = _dedupe_preserve_order(list(staging_warnings) + list(diag.get("warnings") or []))
            diag = dict(diag)
            diag["warnings"] = merged
        return diag

    if not tmdb_id or int(tmdb_id) <= 0:
        return PipelineResult(status="fallback_pt", reason="tmdb_id_missing")

    extra_secrets = [getattr(client, "api_key", "")]

    # Step A & B: Resource search + share parse/browse/select
    try:
        plan, search_diag = select_best_share_candidate(
            client,
            title=decision.title,
            original_title=decision.original_title,
            year=decision.year,
            media_kind=decision.media_kind,
            season=decision.season,
            expected_episodes=decision.expected_episodes,
            channels=config.search_channels or None,
            max_candidates=config.max_candidates,
            max_drill_depth=config.max_drill_depth,
        )
    except MuvyoClientError as exc:
        return PipelineResult(
            status="fallback_pt",
            reason=f"mv3_search_error:{redact_exception(exc, extra_secrets=extra_secrets)}",
            diagnostics={"stage": "search"},
        )

    if plan is None:
        return PipelineResult(
            status="fallback_pt",
            reason="no_suitable_share_candidate",
            diagnostics={"stage": "select", "attempts": len(search_diag)},
        )

    if config.dry_run:
        return PipelineResult(
            status="dry_run_matched",
            reason="dry_run_selected_candidate",
            video_count=plan.video_count,
            episodes=list(plan.episodes),
            diagnostics={
                "stage": "dry_run",
                "title_hash": plan.title_hash,
                "score": plan.score,
                "matched_keyword": redact_sensitive_text(
                    plan.matched_keyword, max_length=200, extra_secrets=extra_secrets
                ),
                "excluded_metadata_count": plan.excluded_metadata_count,
            },
        )

    if _stopped():
        return PipelineResult(status="aborted_stopped", reason="plugin_stopped_before_receive")

    # Step D: Ensure isolated staging directory & receive share
    rid = run_id or short_hash(f"{tmdb_id}:{decision.season}:{time.time()}")
    title_seg = _safe_folder_segment(
        redact_sensitive_text(decision.title, max_length=200, extra_secrets=extra_secrets).replace("[REDACTED]", " ")
    )
    season_seg = f"s{decision.season:02d}" if decision.media_kind == "tv" else "movie"
    staging_base = normalize_cloud_path(config.staging_root)
    staging_path = f"{staging_base}/{title_seg}_tmdb{int(tmdb_id)}_{season_seg}_{rid}"

    try:
        ensure_kwargs: Dict[str, Any] = {}
        try:
            sig = inspect.signature(client.ensure_115_folder)
            if "should_stop" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                ensure_kwargs["should_stop"] = _stopped
        except (TypeError, ValueError):
            pass
        ensure_res = client.ensure_115_folder(staging_path, **ensure_kwargs)
    except MuvyoClientError as exc:
        return PipelineResult(
            status="fallback_pt",
            reason=f"ensure_staging_failed:{redact_exception(exc, extra_secrets=extra_secrets)}",
            staging_path=staging_path,
        )

    if ensure_res.get("aborted") or _stopped():
        return PipelineResult(
            status="aborted_stopped",
            reason="plugin_stopped_during_ensure_staging",
            staging_path=staging_path,
        )

    if not ensure_res.get("ok") or not ensure_res.get("final_folder_id"):
        return PipelineResult(
            status="fallback_pt",
            reason="ensure_staging_folder_not_ok",
            staging_path=staging_path,
        )
    staging_folder_id = str(ensure_res["final_folder_id"])

    if _stopped():
        return PipelineResult(
            status="aborted_stopped",
            reason="plugin_stopped_before_receive",
            staging_path=staging_path,
        )

    try:
        receive_res = client.receive_share(
            plan.share_code,
            plan.file_ids,
            target_path=staging_path,
            receive_code=plan.receive_code,
        )
    except MuvyoClientError as exc:
        return PipelineResult(
            status="fallback_pt",
            reason=f"share_receive_failed:{redact_exception(exc, extra_secrets=extra_secrets)}",
            staging_path=staging_path,
        )

    if not receive_res.get("ok"):
        return PipelineResult(
            status="fallback_pt",
            reason="share_receive_not_ok",
            staging_path=staging_path,
        )

    # Step E: Browse staging (with settle polling for 115 eventual consistency) + verify coverage + embedded Chinese subtitle gate
    max_attempts = max(1, int(config.staging_settle_max_attempts))
    settle_interval = max(0.0, float(config.staging_settle_interval_seconds))
    sleeper = sleep_fn if sleep_fn is not None else time.sleep
    expected_min_videos = max(1, int(getattr(plan, "video_count", 0) or 0))
    expected_ep_set = (
        set(range(1, decision.expected_episodes + 1))
        if (decision.media_kind == "tv" and decision.expected_episodes > 0)
        else set()
    )

    staged_videos: List[Dict[str, Any]] = []
    staged_sub_sidecars: List[Dict[str, Any]] = []
    staged_episodes: List[int] = []
    has_browse_ok = False
    last_browse_exc: Optional[MuvyoClientError] = None
    last_browse_not_ok: Optional[Any] = None
    staging_browse_attempts = 0

    for attempt in range(1, max_attempts + 1):
        staging_browse_attempts = attempt
        try:
            browse_res = client.browse_cloud_folder(folder_id=staging_folder_id, path=staging_path)
        except MuvyoClientError as exc:
            last_browse_exc = exc
        else:
            last_browse_exc = None
            if browse_res.get("ok"):
                has_browse_ok = True
                last_browse_not_ok = None
                rows = browse_res.get("items", [])
                cur_videos: List[Dict[str, Any]] = []
                cur_sub_sidecars: List[Dict[str, Any]] = []
                cur_episodes: List[int] = []
                for row in rows:
                    if item_is_folder(row):
                        continue
                    fname = item_name(row)
                    kind = classify_media_kind(fname)
                    if kind == "video":
                        cur_videos.append(row)
                        _, ep = extract_season_and_episode(fname)
                        if ep is not None and ep > 0:
                            cur_episodes.append(ep)
                    elif kind == "subtitle_sidecar":
                        cur_sub_sidecars.append(row)

                staged_videos = cur_videos
                staged_sub_sidecars = cur_sub_sidecars
                staged_episodes = cur_episodes

                is_complete = len(staged_videos) >= expected_min_videos and (
                    not expected_ep_set or expected_ep_set.issubset(set(staged_episodes))
                )
                if _stopped():
                    return PipelineResult(
                        status="manual_review_staging",
                        reason="plugin_stopped_during_staging_settle",
                        staging_path=staging_path,
                        video_count=len(staged_videos),
                        episodes=sorted(set(staged_episodes)),
                        diagnostics={
                            "stage": "staging_settle",
                            "staging_browse_attempts": staging_browse_attempts,
                        },
                    )
                if is_complete:
                    break
            else:
                raw_not_ok = browse_res.get("status")
                if raw_not_ok is None:
                    raw_not_ok = browse_res.get("error") or "not_ok"
                last_browse_not_ok = (
                    raw_not_ok
                    if isinstance(raw_not_ok, int)
                    else redact_sensitive_text(str(raw_not_ok), max_length=200, extra_secrets=extra_secrets)
                )

        if attempt < max_attempts:
            if _stopped():
                return PipelineResult(
                    status="manual_review_staging",
                    reason="plugin_stopped_during_staging_settle",
                    staging_path=staging_path,
                    video_count=len(staged_videos),
                    episodes=sorted(set(staged_episodes)),
                    diagnostics={
                        "stage": "staging_settle",
                        "staging_browse_attempts": attempt,
                    },
                )
            if settle_interval > 0:
                sleeper(settle_interval)
                if _stopped():
                    return PipelineResult(
                        status="manual_review_staging",
                        reason="plugin_stopped_during_staging_settle",
                        staging_path=staging_path,
                        video_count=len(staged_videos),
                        episodes=sorted(set(staged_episodes)),
                        diagnostics={
                            "stage": "staging_settle",
                            "staging_browse_attempts": attempt,
                        },
                    )

    if not has_browse_ok and last_browse_exc is not None:
        return PipelineResult(
            status="manual_review_staging",
            reason=f"staging_browse_failed:{redact_exception(last_browse_exc, extra_secrets=extra_secrets)}",
            staging_path=staging_path,
            diagnostics=_with_staging_warnings({
                "stage": "staging_settle",
                "staging_browse_attempts": staging_browse_attempts,
            }),
        )

    if not staged_videos:
        empty_diag: Dict[str, Any] = {
            "stage": "staging_settle",
            "staging_browse_attempts": staging_browse_attempts,
        }
        if last_browse_not_ok is not None:
            empty_diag["last_browse_not_ok"] = last_browse_not_ok
        return PipelineResult(
            status="manual_review_staging",
            reason="staging_contains_no_videos",
            staging_path=staging_path,
            diagnostics=_with_staging_warnings(empty_diag),
        )

    probe_targets: List[Dict[str, Any]] = []
    organize_files: List[Dict[str, Any]] = []
    for row in staged_videos:
        fname = item_name(row)
        fid = item_file_id(row)
        pickcode = str(
            row.get("pc") or row.get("pickcode") or row.get("pick_code") or row.get("pickCode") or ""
        ).strip()
        cloud_file_path = f"{staging_path}/{fname}"
        _, ep = extract_season_and_episode(fname)
        probe_url = client.build_redirect_probe_url(cloud_file_path, pickcode)
        probe_targets.append({"name": fname, "episode": ep, "probe_url": probe_url})
        organize_files.append({
            "name": fname,
            "source_path": cloud_file_path,
            "source_file_id": fid,
            "is_cloud_source": True,
        })

    if expected_ep_set and not expected_ep_set.issubset(set(staged_episodes)):
        incomplete_diag: Dict[str, Any] = {
            "stage": "staging_settle",
            "staging_browse_attempts": staging_browse_attempts,
        }
        if last_browse_not_ok is not None:
            incomplete_diag["last_browse_not_ok"] = last_browse_not_ok
        return PipelineResult(
            status="manual_review_staging",
            reason="staging_episode_coverage_incomplete",
            staging_path=staging_path,
            video_count=len(staged_videos),
            episodes=sorted(set(staged_episodes)),
            diagnostics=_with_staging_warnings(incomplete_diag),
        )

    if _stopped():
        return PipelineResult(
            status="manual_review_staging",
            reason="plugin_stopped_before_subtitle_gate",
            staging_path=staging_path,
            video_count=len(staged_videos),
            episodes=sorted(set(staged_episodes)),
            diagnostics={
                "stage": "staging_settle",
                "staging_browse_attempts": staging_browse_attempts,
            },
        )

    sub_audit = evaluate_embedded_subtitle_gate(
        probe_targets,
        policy=config.subtitle_policy,
        probe_binary=config.ffprobe_binary,
        timeout=config.ffprobe_timeout,
        max_workers=config.ffprobe_concurrency,
        probe_runner=probe_runner,
    )
    if not sub_audit.get("ok"):
        # CRITICAL: stop in staging for manual review; NEVER organize, NEVER generate STRM, NEVER delete staging!
        safe_blockers = [
            redact_sensitive_text(str(b), max_length=200, extra_secrets=extra_secrets)
            for b in sub_audit.get("blockers", [])
        ]
        safe_sub_audit = dict(sub_audit)
        safe_sub_audit["blockers"] = safe_blockers
        return PipelineResult(
            status="manual_review_staging",
            reason="subtitle_gate_blocked:" + ",".join(safe_blockers),
            staging_path=staging_path,
            video_count=len(staged_videos),
            episodes=sorted(set(staged_episodes)),
            subtitle_audit=safe_sub_audit,
            diagnostics={
                "stage": "subtitle_gate",
                "staging_browse_attempts": staging_browse_attempts,
            },
        )

    if _stopped():
        return PipelineResult(
            status="manual_review_staging",
            reason="plugin_stopped_before_organize",
            staging_path=staging_path,
            video_count=len(staged_videos),
            episodes=sorted(set(staged_episodes)),
            subtitle_audit=sub_audit,
            diagnostics={"staging_browse_attempts": staging_browse_attempts},
        )

    # Step F: Organize transfer + optional explicit STRM generate (strictly scoped)
    org_root = normalize_cloud_path(config.cloud_organize_root)
    strm_root = normalize_cloud_path(config.mv3_strm_root)
    try:
        org_res = client.organize_transfer(
            files=organize_files,
            target_dir=org_root,
            strm_dir=strm_root,
            tmdb_id=int(tmdb_id),
            mode="move",
            is_cloud_target=True,
            copy_subtitles=bool(staged_sub_sidecars),
            enable_primary_category=True,
            enable_secondary_category=True,
            background=False,
        )
    except MuvyoClientError as exc:
        return PipelineResult(
            status="manual_review_staging",
            reason=f"organize_transfer_failed:{redact_exception(exc, extra_secrets=extra_secrets)}",
            staging_path=staging_path,
            video_count=len(staged_videos),
            episodes=sorted(set(staged_episodes)),
            subtitle_audit=sub_audit,
            diagnostics={"staging_browse_attempts": staging_browse_attempts},
        )

    if not org_res.get("ok"):
        org_blockers = [
            redact_sensitive_text(str(b), max_length=200, extra_secrets=extra_secrets)
            for b in (org_res.get("blockers") or [])
            if b
        ]
        reason = "organize_transfer_blocked:" + ",".join(org_blockers) if org_blockers else "organize_transfer_not_ok"
        return PipelineResult(
            status="manual_review_staging",
            reason=reason,
            staging_path=staging_path,
            video_count=len(staged_videos),
            episodes=sorted(set(staged_episodes)),
            subtitle_audit=sub_audit,
            diagnostics={
                "stage": "organize_transfer",
                "blockers": org_blockers,
                "staging_browse_attempts": staging_browse_attempts,
            },
        )

    strm_generated = False
    strm_warnings: List[str] = []
    if config.trigger_strm_generate:
        if _stopped():
            return PipelineResult(
                status="manual_review_staging",
                reason="plugin_stopped_before_strm_generate",
                staging_path=staging_path,
                video_count=len(staged_videos),
                episodes=sorted(set(staged_episodes)),
                subtitle_audit=sub_audit,
                diagnostics={"staging_browse_attempts": staging_browse_attempts},
            )
        resolved_dirs = [
            normalize_cloud_path(d)
            for d in (org_res.get("resolved_target_dirs") or [])
            if normalize_cloud_path(d) and normalize_cloud_path(d) != org_root
        ]
        if not resolved_dirs:
            # Never fall back to scanning the entire cloud_organize_root; skip with warning.
            strm_warnings.append("strm_generate_skipped_missing_scoped_target_dir")
        else:
            for scoped_dir in resolved_dirs:
                if _stopped():
                    return PipelineResult(
                        status="manual_review_staging",
                        reason="plugin_stopped_before_strm_generate",
                        staging_path=staging_path,
                        video_count=len(staged_videos),
                        episodes=sorted(set(staged_episodes)),
                        subtitle_audit=sub_audit,
                        diagnostics={"staging_browse_attempts": staging_browse_attempts},
                    )
                try:
                    strm_res = client.generate_strm(
                        source_dir=scoped_dir,
                        target_dir=strm_root,
                        cloud=True,
                        incremental=True,
                        overwrite=False,
                    )
                    if not strm_res.get("ok"):
                        return PipelineResult(
                            status="manual_review_staging",
                            reason="strm_generate_not_ok",
                            staging_path=staging_path,
                            video_count=len(staged_videos),
                            episodes=sorted(set(staged_episodes)),
                            subtitle_audit=sub_audit,
                            diagnostics={"staging_browse_attempts": staging_browse_attempts},
                        )
                    strm_generated = True
                except MuvyoClientError as exc:
                    return PipelineResult(
                        status="manual_review_staging",
                        reason=f"strm_generate_failed:{redact_exception(exc, extra_secrets=extra_secrets)}",
                        staging_path=staging_path,
                        video_count=len(staged_videos),
                        episodes=sorted(set(staged_episodes)),
                        subtitle_audit=sub_audit,
                        diagnostics={"staging_browse_attempts": staging_browse_attempts},
                    )

    # Step G: Post-process local STRM scrape & scoped Emby refresh (fault-isolated: never falls back to PT)
    all_warnings: List[str] = list(staging_warnings) + list(strm_warnings)
    post_process_diag: Dict[str, Any] = {
        "enabled": bool(config.auto_scrape_strm or config.auto_refresh_emby),
        "scrape_enabled": bool(config.auto_scrape_strm),
        "emby_refresh_enabled": bool(config.auto_refresh_emby),
        "scrape_dirs": [],
        "emby_refresh_dirs": [],
        "scrape_attempted": 0,
        "scrape_succeeded": 0,
        "scrape_failed": 0,
        "scrape_results": [],
        "emby_refresh_called": False,
        "emby_refresh_ok": None,
        "emby_refresh_result": {},
        "skipped_reason": "",
    }

    if not (config.auto_scrape_strm or config.auto_refresh_emby):
        post_process_diag["skipped_reason"] = "post_process_disabled"
    elif _stopped():
        post_process_diag["skipped_reason"] = "plugin_stopped_before_post_process"
        all_warnings.append("post_process_skipped_plugin_stopped")
    elif config.trigger_strm_generate and not strm_generated:
        post_process_diag["skipped_reason"] = "strm_generate_not_run"
        all_warnings.append("strm_post_process_skipped_strm_generate_not_run")
    elif not normalize_cloud_path(config.local_strm_root):
        post_process_diag["skipped_reason"] = "local_strm_root_not_configured"
        all_warnings.append("strm_post_process_skipped_local_strm_root_empty")
    else:
        resolved_targets = resolve_post_process_strm_targets(
            resolved_strm_dirs=org_res.get("resolved_strm_dirs") or [],
            resolved_target_dirs=org_res.get("resolved_target_dirs") or [],
            mv3_strm_root=strm_root,
            cloud_organize_root=org_root,
            local_strm_root=config.local_strm_root,
            emby_strm_root=config.emby_strm_root,
            media_kind=decision.media_kind,
        )
        all_warnings.extend(resolved_targets.get("warnings") or [])
        targets: List[Dict[str, Any]] = list(resolved_targets.get("targets") or [])
        post_process_diag["scrape_dirs"] = list(resolved_targets.get("scrape_dirs") or [])

        if not targets:
            post_process_diag["skipped_reason"] = "missing_scoped_target_dir"
        else:
            norm_local_strm = normalize_cloud_path(config.local_strm_root)
            exists_check = path_exists_fn if path_exists_fn is not None else os.path.isdir
            strm_settle_attempts = max(1, int(config.strm_settle_max_attempts))
            strm_settle_interval = max(0.0, float(config.strm_settle_interval_seconds))
            approved_emby_refresh_dirs: List[str] = []

            def _all_local_dirs_within_root(local_dirs: Sequence[str]) -> bool:
                return bool(local_dirs) and all(
                    is_path_strictly_within_root(d, norm_local_strm) for d in local_dirs
                )

            def _approve_target_emby_dirs(target: Dict[str, Any]) -> None:
                local_refs = list(target.get("local_refresh_dirs") or [])
                emby_refs = list(target.get("emby_refresh_dirs") or [])
                if len(local_refs) != len(emby_refs):
                    return
                for ldir, edir in zip(local_refs, emby_refs):
                    if is_path_strictly_within_root(ldir, norm_local_strm) and edir not in approved_emby_refresh_dirs:
                        approved_emby_refresh_dirs.append(edir)

            if config.auto_scrape_strm:
                if scrape_runner is None:
                    all_warnings.append("strm_scrape_skipped_runner_unavailable")
                else:
                    for target in targets:
                        target_dir = str(target.get("scrape_dir") or "")
                        settle_dirs = list(target.get("settle_dirs") or [target_dir])
                        local_refresh_dirs = list(target.get("local_refresh_dirs") or settle_dirs)
                        if _stopped():
                            all_warnings.append("strm_scrape_aborted_plugin_stopped")
                            break

                        # Boundary guard across scrape_dir, all settle_dirs, and all local_refresh_dirs
                        if not _all_local_dirs_within_root([target_dir] + settle_dirs + local_refresh_dirs):
                            post_process_diag["scrape_failed"] += 1
                            post_process_diag["scrape_results"].append({
                                "target_dir": target_dir,
                                "ok": False,
                                "error": "symlink_or_root_escape_blocked",
                            })
                            all_warnings.append("strm_scrape_blocked_symlink_or_escape")
                            continue

                        visible = False
                        escape_blocked = False
                        aborted_wait = False
                        attempts_used = 0
                        for attempt in range(1, strm_settle_attempts + 1):
                            attempts_used = attempt
                            if _stopped():
                                aborted_wait = True
                                break
                            all_found = True
                            for sdir in settle_dirs:
                                try:
                                    found = bool(exists_check(sdir))
                                except Exception:
                                    found = False
                                if not found:
                                    all_found = False
                                    break
                                if not is_path_strictly_within_root(sdir, norm_local_strm):
                                    escape_blocked = True
                                    all_found = False
                                    break
                            if escape_blocked:
                                break
                            if all_found:
                                if not _all_local_dirs_within_root([target_dir] + settle_dirs + local_refresh_dirs):
                                    escape_blocked = True
                                else:
                                    visible = True
                                break
                            if attempt < strm_settle_attempts:
                                if _stopped():
                                    aborted_wait = True
                                    break
                                if strm_settle_interval > 0:
                                    sleeper(strm_settle_interval)
                                    if _stopped():
                                        aborted_wait = True
                                        break

                        if aborted_wait:
                            all_warnings.append("strm_scrape_aborted_plugin_stopped")
                            break
                        if escape_blocked:
                            post_process_diag["scrape_failed"] += 1
                            post_process_diag["scrape_results"].append({
                                "target_dir": target_dir,
                                "ok": False,
                                "error": "symlink_or_root_escape_blocked",
                                "settle_attempts": attempts_used,
                            })
                            all_warnings.append("strm_scrape_blocked_symlink_or_escape")
                            continue
                        if not visible:
                            post_process_diag["scrape_failed"] += 1
                            post_process_diag["scrape_results"].append({
                                "target_dir": target_dir,
                                "ok": False,
                                "error": "local_strm_dir_not_visible_after_settle",
                                "settle_attempts": attempts_used,
                            })
                            all_warnings.append("strm_scrape_target_not_found")
                            continue
                        if _stopped():
                            all_warnings.append("strm_scrape_aborted_plugin_stopped")
                            break

                        post_process_diag["scrape_attempted"] += 1
                        try:
                            scrape_res = scrape_runner(target_dir, int(tmdb_id), decision.media_kind)
                        except Exception as exc:
                            err_text = redact_exception(exc, extra_secrets=extra_secrets)
                            post_process_diag["scrape_failed"] += 1
                            post_process_diag["scrape_results"].append({
                                "target_dir": target_dir,
                                "ok": False,
                                "error": err_text,
                                "settle_attempts": attempts_used,
                            })
                            all_warnings.append("strm_scrape_failed")
                        else:
                            safe_res = (
                                redact_sensitive_data(scrape_res, extra_secrets=extra_secrets)
                                if isinstance(scrape_res, dict)
                                else {"ok": bool(scrape_res)}
                            )
                            if safe_res.get("ok"):
                                post_process_diag["scrape_succeeded"] += 1
                                post_process_diag["scrape_results"].append({
                                    "target_dir": target_dir,
                                    "ok": True,
                                    "settle_attempts": attempts_used,
                                    "detail": safe_res,
                                })
                                _approve_target_emby_dirs(target)
                            else:
                                post_process_diag["scrape_failed"] += 1
                                post_process_diag["scrape_results"].append({
                                    "target_dir": target_dir,
                                    "ok": False,
                                    "settle_attempts": attempts_used,
                                    "error": str(safe_res.get("error") or "scrape_runner_not_ok"),
                                    "detail": safe_res,
                                })
                                all_warnings.append("strm_scrape_failed")
            else:
                # Even when auto_scrape_strm is disabled, enforce strict root/symlink boundary before Emby refresh.
                for target in targets:
                    target_dir = str(target.get("scrape_dir") or "")
                    settle_dirs = list(target.get("settle_dirs") or [target_dir])
                    local_refresh_dirs = list(target.get("local_refresh_dirs") or settle_dirs)
                    if not _all_local_dirs_within_root([target_dir] + settle_dirs + local_refresh_dirs):
                        all_warnings.append("emby_refresh_blocked_symlink_or_escape")
                        continue
                    _approve_target_emby_dirs(target)

            approved_emby_refresh_dirs = _dedupe_preserve_order(approved_emby_refresh_dirs)
            post_process_diag["emby_refresh_dirs"] = approved_emby_refresh_dirs

            if config.auto_refresh_emby:
                if _stopped():
                    all_warnings.append("emby_refresh_skipped_plugin_stopped")
                elif emby_refresh_runner is None:
                    all_warnings.append("emby_refresh_skipped_runner_unavailable")
                elif config.auto_scrape_strm and post_process_diag["scrape_succeeded"] <= 0:
                    all_warnings.append("emby_refresh_skipped_scrape_failed")
                elif not approved_emby_refresh_dirs:
                    all_warnings.append("emby_refresh_skipped_no_safe_target_dir")
                else:
                    post_process_diag["emby_refresh_called"] = True
                    try:
                        refresh_res = emby_refresh_runner(list(approved_emby_refresh_dirs))
                    except Exception as exc:
                        err_text = redact_exception(exc, extra_secrets=extra_secrets)
                        post_process_diag["emby_refresh_ok"] = False
                        post_process_diag["emby_refresh_result"] = {"ok": False, "error": err_text}
                        all_warnings.append("emby_refresh_failed")
                    else:
                        safe_refresh = (
                            redact_sensitive_data(refresh_res, extra_secrets=extra_secrets)
                            if isinstance(refresh_res, dict)
                            else {"ok": bool(refresh_res)}
                        )
                        failed_svcs = int(safe_refresh.get("failed_services") or 0)
                        refreshed_svcs = int(safe_refresh.get("refreshed_services") or 0)
                        partial_fail = bool(safe_refresh.get("partial_failure")) or (
                            failed_svcs > 0 and refreshed_svcs > 0
                        )
                        post_process_diag["emby_refresh_result"] = safe_refresh
                        if partial_fail:
                            post_process_diag["emby_refresh_ok"] = False
                            all_warnings.append("emby_refresh_partial_failed")
                        elif not safe_refresh.get("ok") or failed_svcs > 0:
                            post_process_diag["emby_refresh_ok"] = False
                            all_warnings.append("emby_refresh_failed")
                        else:
                            post_process_diag["emby_refresh_ok"] = True

    return PipelineResult(
        status="routed_ok",
        reason="muvyo_routed_and_organized",
        staging_path=staging_path,
        video_count=len(staged_videos),
        episodes=sorted(set(staged_episodes)),
        subtitle_audit=sub_audit,
        diagnostics={
            "stage": "done",
            "title_hash": plan.title_hash,
            "excluded_metadata_count": plan.excluded_metadata_count,
            "staging_browse_attempts": staging_browse_attempts,
            "strm_generate_called": strm_generated,
            "post_process": post_process_diag,
            "warnings": _dedupe_preserve_order(all_warnings),
        },
    )

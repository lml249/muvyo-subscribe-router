from __future__ import annotations

import inspect
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


def validate_router_paths(
    staging_root: str,
    cloud_organize_root: str,
    mv3_strm_root: str,
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

    return sorted(set(errors))


def _safe_folder_segment(text: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(text or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or "media")[:max_len]


@dataclass
class RouterPipelineConfig:
    staging_root: str = "/未整理/mp-router"
    cloud_organize_root: str = "/已整理"
    mv3_strm_root: str = "/strm"
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
) -> PipelineResult:
    """
    Execute the end-to-end Muvyo routing pipeline for a single completed subscription:
    Path Guard -> Search/Select -> Ensure Staging -> Receive -> Browse Staging ->
    Embedded Subtitle Gate -> Organize Transfer -> Optional STRM Generate.
    Never deletes staging files on failure.
    """
    def _stopped() -> bool:
        return bool(should_stop is not None and should_stop())

    if _stopped():
        return PipelineResult(status="aborted_stopped", reason="plugin_stopped_before_start")

    path_errors = validate_router_paths(
        config.staging_root, config.cloud_organize_root, config.mv3_strm_root
    )
    if path_errors:
        return PipelineResult(
            status="config_error",
            reason="invalid_router_paths:" + ",".join(path_errors),
            diagnostics={"path_errors": path_errors},
        )

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
            diagnostics={
                "stage": "staging_settle",
                "staging_browse_attempts": staging_browse_attempts,
            },
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
            diagnostics=empty_diag,
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
            diagnostics=incomplete_diag,
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
            "warnings": strm_warnings,
        },
    )

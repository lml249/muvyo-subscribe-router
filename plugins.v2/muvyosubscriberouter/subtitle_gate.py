from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .redaction import redact_sensitive_text

CHINESE_LANGUAGE_CODES = {
    "chi", "zh", "zho", "zhs", "zht", "chs", "cht", "cmn", "yue",
    "zh-cn", "zh-hans", "zh-sg", "zh-my", "zh-tw", "zh-hant", "zh-hk", "zh-mo",
}

NON_CHINESE_LANGUAGE_CODES = {
    "jpn", "ja", "kor", "ko", "eng", "en", "fra", "fre", "fr", "deu", "ger", "de",
    "spa", "es", "rus", "ru", "ita", "it", "por", "pt", "tha", "th", "vie", "vi",
    "ind", "id", "ara", "ar", "hin", "hi", "nld", "dut", "nl", "pol", "pl",
}

CHINESE_TEXT_MARKERS = (
    "中文", "中字", "简体", "簡體", "简中", "簡中", "繁体", "繁體", "繁中",
    "国语", "國語", "普通话", "普通話", "粤语", "粵語", "华语", "華語",
    "中英", "简英", "繁英", "简日", "繁日", "中日", "简繁", "中韩",
    "chinese", "simplified", "traditional", "mandarin", "cantonese",
)

CHINESE_WORD_TOKEN_RE = re.compile(r"(?i)(?:^|[\s._\-\[\]()【】/])(chs|cht|zhs|zht|sc|tc)(?:$|[\s._\-\[\]()【】/])")
KANA_OR_HANGUL_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
EXPLICIT_FOREIGN_MARKERS = ("日本語", "日本语", "日语", "日語", "日文", "japanese", "korean", "한국어")

SUPPORTED_POLICIES = {"auto", "required"}

ProbeRunner = Callable[[str, str, int], Dict[str, Any]]


def normalize_subtitle_policy(policy: str) -> str:
    val = str(policy or "auto").strip().lower().replace("_", "-")
    if val in {"require", "strict", "required"}:
        return "required"
    if val in {"auto", "default"}:
        return "auto"
    return "required"


def _normalize_lang(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    aliases = {
        "cn": "zh-cn",
        "sc": "chs",
        "tc": "cht",
        "zh-hans-cn": "zh-hans",
        "zh-hant-tw": "zh-hant",
    }
    return aliases.get(cleaned, cleaned)


def _lang_is_explicit_non_chinese(lang: str) -> bool:
    if not lang or lang in {"und", "unk", "unknown", "mul", "zxx"}:
        return False
    if lang in NON_CHINESE_LANGUAGE_CODES:
        return True
    base = lang.split("-", 1)[0]
    return base in NON_CHINESE_LANGUAGE_CODES


def stream_is_chinese(stream: Dict[str, Any]) -> bool:
    lang = _normalize_lang(str(stream.get("language") or ""))
    if lang in CHINESE_LANGUAGE_CODES or lang.startswith("zh-"):
        return True

    title_and_handler = " ".join(
        [str(stream.get("title") or ""), str(stream.get("handler_name") or "")]
    )
    haystack = title_and_handler.lower()
    has_explicit_zh_marker = any(marker.lower() in haystack for marker in CHINESE_TEXT_MARKERS) or bool(
        CHINESE_WORD_TOKEN_RE.search(title_and_handler)
    )
    if has_explicit_zh_marker:
        return True

    if _lang_is_explicit_non_chinese(lang):
        return False
    if KANA_OR_HANGUL_RE.search(title_and_handler):
        return False
    if any(marker.lower() in haystack for marker in EXPLICIT_FOREIGN_MARKERS):
        return False
    return False


def resolve_probe_binary(probe_binary: str = "", probe_runner: Optional[ProbeRunner] = None) -> str:
    if probe_runner is not None:
        return str(probe_binary or "ffprobe")
    if probe_binary:
        found = shutil.which(probe_binary)
        if found:
            return found
        return probe_binary if Path(probe_binary).exists() else ""
    return shutil.which("ffprobe") or ""


def _parse_ffprobe_streams(payload: Dict[str, Any]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    subs: List[Dict[str, Any]] = []
    audios: List[Dict[str, Any]] = []
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return subs, audios
    for raw in streams:
        if not isinstance(raw, dict):
            continue
        ctype = str(raw.get("codec_type") or "").lower()
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        entry = {
            "index": raw.get("index"),
            "codec_type": ctype,
            "codec_name": str(raw.get("codec_name") or ""),
            "language": str(tags.get("language") or ""),
            "title": str(tags.get("title") or ""),
            "handler_name": str(tags.get("handler_name") or ""),
        }
        entry["chinese_match"] = stream_is_chinese(entry)
        if ctype == "subtitle":
            subs.append(entry)
        elif ctype == "audio":
            audios.append(entry)
    return subs, audios


def _run_ffprobe(probe_tool: str, target_url: str, timeout: int) -> Dict[str, Any]:
    cmd = [
        probe_tool,
        "-v", "error",
        "-show_entries", "stream=index,codec_type,codec_name:stream_tags=language,title,handler_name",
        "-of", "json",
        target_url,
    ]
    try:
        completed = subprocess.run(cmd, text=True, capture_output=True, timeout=max(1, int(timeout)))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ffprobe_timeout", "subtitle_streams": [], "audio_streams": []}
    except OSError as exc:
        return {
            "ok": False,
            "error": redact_sensitive_text(f"{exc.__class__.__name__}:{exc}", max_length=200),
            "subtitle_streams": [],
            "audio_streams": [],
        }

    if completed.returncode != 0:
        err = redact_sensitive_text(completed.stderr or f"returncode:{completed.returncode}", max_length=200)
        return {"ok": False, "error": err, "subtitle_streams": [], "audio_streams": []}

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "ffprobe_json_parse_failed", "subtitle_streams": [], "audio_streams": []}

    subs, audios = _parse_ffprobe_streams(payload if isinstance(payload, dict) else {})
    return {"ok": True, "subtitle_streams": subs, "audio_streams": audios}


def _probe_single_target(
    target: Dict[str, Any],
    *,
    probe_tool: str,
    timeout: int,
    policy: str,
    probe_runner: Optional[ProbeRunner],
) -> Dict[str, Any]:
    name = str(target.get("name") or "")
    episode = target.get("episode")
    probe_url = str(target.get("probe_url") or "")
    blockers: List[str] = []
    warnings: List[str] = []
    subs: List[Dict[str, Any]] = []
    audios: List[Dict[str, Any]] = []

    if not probe_url:
        blockers.append("probe_target_missing")
    elif not probe_tool:
        blockers.append("ffprobe_missing")
    else:
        if probe_runner is not None:
            raw = dict(probe_runner(probe_tool, probe_url, timeout))
            raw.setdefault("ok", not raw.get("error"))
            if "subtitle_streams" not in raw or "audio_streams" not in raw:
                p_subs, p_audios = _parse_ffprobe_streams(raw)
                raw.setdefault("subtitle_streams", p_subs)
                raw.setdefault("audio_streams", p_audios)
            probe_res = raw
        else:
            probe_res = _run_ffprobe(probe_tool, probe_url, timeout)

        if not probe_res.get("ok"):
            blockers.append("probe_failed")
            err = redact_sensitive_text(probe_res.get("error") or "probe_error", max_length=200)
            warnings.append(f"probe_failed:{name}:{err}")
        else:
            subs = [s for s in probe_res.get("subtitle_streams", []) if isinstance(s, dict)]
            audios = [a for a in probe_res.get("audio_streams", []) if isinstance(a, dict)]
            zh_subs = [s for s in subs if stream_is_chinese(s)]
            zh_audios = [a for a in audios if stream_is_chinese(a)]
            if not zh_subs:
                if policy == "auto" and zh_audios:
                    warnings.append("chinese_audio_exempt_missing_embedded_zh_sub")
                else:
                    blockers.append("embedded_chinese_subtitle_missing")

    zh_sub_cnt = sum(1 for s in subs if stream_is_chinese(s))
    zh_aud_cnt = sum(1 for a in audios if stream_is_chinese(a))
    return {
        "name": name,
        "episode": episode,
        "ok": not blockers,
        "has_chinese_embedded_subtitle": zh_sub_cnt > 0,
        "has_chinese_audio": zh_aud_cnt > 0,
        "subtitle_stream_count": len(subs),
        "audio_stream_count": len(audios),
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }


def evaluate_embedded_subtitle_gate(
    targets: Sequence[Dict[str, Any]],
    *,
    policy: str = "auto",
    probe_binary: str = "",
    timeout: int = 45,
    max_workers: int = 2,
    probe_runner: Optional[ProbeRunner] = None,
) -> Dict[str, Any]:
    """
    Verify embedded Chinese subtitle gate for staged cloud videos.
    External sidecar subtitles never satisfy this gate.
    Never logs or returns raw probe URLs or pickcodes.
    """
    norm_policy = normalize_subtitle_policy(policy)
    probe_tool = resolve_probe_binary(probe_binary, probe_runner)
    blockers: List[str] = []
    warnings: List[str] = []

    if not targets:
        blockers.append("no_targets_to_probe")
    if not probe_tool:
        blockers.append("ffprobe_missing")

    if blockers:
        return {
            "ok": False,
            "policy": norm_policy,
            "probe_tool_found": bool(probe_tool),
            "target_count": len(targets),
            "passed_count": 0,
            "failed_count": len(targets),
            "exempt_chinese_audio_count": 0,
            "items": [],
            "blockers": sorted(set(blockers)),
            "warnings": warnings,
        }

    results: List[Dict[str, Any]] = [{}] * len(targets)
    workers = max(1, min(int(max_workers or 2), len(targets)))
    if workers == 1:
        for idx, item in enumerate(targets):
            results[idx] = _probe_single_target(
                item, probe_tool=probe_tool, timeout=timeout, policy=norm_policy, probe_runner=probe_runner
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(
                    _probe_single_target,
                    item,
                    probe_tool=probe_tool,
                    timeout=timeout,
                    policy=norm_policy,
                    probe_runner=probe_runner,
                ): idx
                for idx, item in enumerate(targets)
            }
            for fut in as_completed(future_map):
                idx = future_map[fut]
                results[idx] = fut.result()

    for res in results:
        blockers.extend(res.get("blockers", []))
        warnings.extend(res.get("warnings", []))

    passed_cnt = sum(1 for r in results if r.get("ok"))
    exempt_cnt = sum(
        1 for r in results
        if r.get("ok") and not r.get("has_chinese_embedded_subtitle") and r.get("has_chinese_audio")
    )
    return {
        "ok": not blockers,
        "policy": norm_policy,
        "probe_tool_found": True,
        "target_count": len(results),
        "passed_count": passed_cnt,
        "failed_count": len(results) - passed_cnt,
        "exempt_chinese_audio_count": exempt_cnt,
        "items": results,
        "blockers": sorted(set(blockers)),
        "warnings": sorted(set(warnings)),
    }

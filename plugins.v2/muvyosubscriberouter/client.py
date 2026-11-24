from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .redaction import redact_exception, redact_sensitive_data, redact_sensitive_text

TransportFn = Callable[[str, str, Dict[str, str], Optional[bytes], int], Tuple[int, Dict[str, str], bytes]]


class MuvyoClientError(RuntimeError):
    """Sanitized exception for Muvyo / MV3 client calls."""

    def __init__(self, message: str, status: int = 0, endpoint: str = "") -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(redact_sensitive_text(message, max_length=500))


def _default_transport(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: int,
) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout))) as resp:
            return int(resp.status), dict(resp.headers.items()), resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers.items()), exc.read(512 * 1024)


def _parse_json(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": redact_sensitive_text(text, max_length=300)}


def _api_success(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return True
    if "success" in parsed:
        return bool(parsed.get("success"))
    if "ok" in parsed:
        return bool(parsed.get("ok"))
    if "code" in parsed:
        return parsed.get("code") in (0, "0", 200, "200", "success", "ok")
    return True


def _unwrap_payload(parsed: Any) -> Any:
    if isinstance(parsed, dict):
        for key in ("data", "result", "payload"):
            if key in parsed and parsed[key] is not None:
                return parsed[key]
    return parsed


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "files", "list", "results", "children", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    for key in ("data", "result", "payload"):
        val = payload.get(key)
        if isinstance(val, dict):
            nested = _extract_rows(val)
            if nested:
                return nested
    return []


def _extract_search_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "results", "list", "data"):
        val = payload.get(key)
        if isinstance(val, list):
            return [dict(item) for item in val if isinstance(item, dict)]
    for key in ("data", "result", "payload"):
        val = payload.get(key)
        if isinstance(val, dict):
            nested = _extract_search_rows(val)
            if nested:
                return nested
    merged: List[Dict[str, Any]] = []
    for key, val in payload.items():
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            entry = dict(item)
            if not entry.get("channel"):
                entry["channel"] = str(key)
            merged.append(entry)
    return merged


def _row_is_non_folder(row: Dict[str, Any]) -> bool:
    if isinstance(row.get("is_dir"), bool):
        return not bool(row["is_dir"])
    if isinstance(row.get("is_folder"), bool):
        return not bool(row["is_folder"])
    kind = str(row.get("type") or row.get("kind") or row.get("category") or "").strip().lower()
    if kind in {"folder", "dir", "directory"}:
        return False
    if kind in {"file", "video", "subtitle"}:
        return True
    fc = row.get("fc")
    if fc is not None and not isinstance(fc, bool):
        fc_str = str(fc).strip()
        if fc_str == "0":
            return False
        if fc_str == "1":
            return True
    if row.get("file_id") not in (None, ""):
        return True
    if row.get("fid") not in (None, ""):
        has_parent_cid = row.get("cid") not in (None, "") or row.get("folder_id") not in (None, "")
        has_pc = row.get("pc") not in (None, "") or row.get("pickcode") not in (None, "")
        raw_size = row.get("size") if row.get("size") is not None else row.get("s")
        has_size = isinstance(raw_size, (int, float)) and raw_size > 0
        ext = os.path.splitext(_row_name(row))[1].lower()
        if has_parent_cid or has_pc or has_size or bool(ext):
            return True
    return False


def _extract_folder_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("cid", "file_id", "folder_id", "id"):
            v = value.get(key)
            if v not in (None, ""):
                return str(v)
        if value.get("fid") and not value.get("pc") and not value.get("pickcode"):
            return str(value.get("fid"))
        paths = value.get("paths")
        if isinstance(paths, list) and paths:
            nested = _extract_folder_id(paths[-1])
            if nested:
                return nested
        for key in ("data", "folder", "result"):
            nested = _extract_folder_id(value.get(key))
            if nested:
                return nested
    if isinstance(value, list) and value:
        return _extract_folder_id(value[0])
    return ""


def _row_name(row: Dict[str, Any]) -> str:
    for key in ("name", "fn", "file_name", "filename", "n", "title"):
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _row_file_id(row: Dict[str, Any]) -> str:
    for key in ("file_id", "fid", "cid", "id"):
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _organize_status_is_blocked(status: str) -> bool:
    norm = str(status or "").strip().lower()
    if not norm:
        return False
    if norm in {"ok", "success", "succeeded", "done", "completed", "moved", "copied"}:
        return False
    bad_tokens = ("fail", "error", "skip", "block", "reject", "exist", "conflict", "abort", "cancel", "deny")
    return any(tok in norm for tok in bad_tokens)


def _evaluate_organize_outcome(payload: Any, *, expected_file_count: int, target_dir: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": True, "blockers": [], "resolved_target_dirs": []}

    counts: Dict[str, int] = {}
    for key in ("success", "failed", "skipped"):
        val = payload.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, int):
            counts[key] = val
        elif isinstance(val, str) and val.strip().isdigit():
            counts[key] = int(val.strip())

    blockers: List[str] = []
    if counts.get("failed", 0) > 0:
        blockers.append("organize_failed_items")
    if counts.get("skipped", 0) > 0:
        blockers.append("organize_skipped_items")
    if "success" in counts and expected_file_count > 0 and counts["success"] < expected_file_count:
        blockers.append("organize_success_count_below_expected")

    items = payload.get("items")
    resolved_dirs: List[str] = []
    norm_root = "/" + "/".join(p for p in str(target_dir or "").strip().replace("\\", "/").split("/") if p)
    if isinstance(items, list):
        for row in items:
            if not isinstance(row, dict):
                continue
            status_text = str(row.get("status") or row.get("message") or row.get("error") or "")
            if _organize_status_is_blocked(status_text):
                blockers.append("organize_blocked_item_status")
            new_path = str(row.get("new_path") or row.get("target_path") or "").strip().replace("\\", "/")
            if new_path and "/" in new_path:
                parent_dir = new_path.rsplit("/", 1)[0]
                if parent_dir and parent_dir != norm_root and (not norm_root or parent_dir.startswith(norm_root + "/")):
                    if parent_dir not in resolved_dirs:
                        resolved_dirs.append(parent_dir)

    for top_key in ("dest_dir", "output_dir", "organized_dir"):
        val = str(payload.get(top_key) or "").strip().replace("\\", "/").rstrip("/")
        if val and val != norm_root and (not norm_root or val.startswith(norm_root + "/")):
            if val not in resolved_dirs:
                resolved_dirs.append(val)

    return {
        "ok": not blockers,
        "counts": counts,
        "blockers": sorted(set(blockers)),
        "resolved_target_dirs": resolved_dirs,
    }


def _first_present(row: Dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        val = row.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


class MuvyoClient:
    """Thin, sanitized HTTP client for external Muvyo / MV3 APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        auth_scheme: str = "x-api-key",
        timeout: int = 30,
        storage: str = "115-default",
        transport: Optional[TransportFn] = None,
    ) -> None:
        scheme = str(auth_scheme or "x-api-key").strip().lower()
        if scheme not in {"x-api-key", "bearer"}:
            raise ValueError(f"unsupported auth_scheme: {scheme}")
        self.base_url = str(base_url or "").strip().rstrip("/")
        raw_api_key = str(api_key or "")
        if "\r" in raw_api_key or "\n" in raw_api_key:
            raise MuvyoClientError("invalid api_key: contains newline characters")
        self.api_key = raw_api_key.strip()
        self.auth_scheme = scheme
        self.timeout = max(1, int(timeout or 30))
        self.storage = str(storage or "115-default").strip()
        self._transport = transport or _default_transport

    def _headers(self, has_json_body: bool = False) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if has_json_body:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            if self.auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers["X-API-Key"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise MuvyoClientError("mv3 base_url is not configured", endpoint=path)
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        url = self._url(path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = self._headers(has_json_body=payload is not None)
        try:
            status, _resp_headers, raw = self._transport(
                method, url, headers, body, int(timeout or self.timeout)
            )
        except Exception as exc:
            raise MuvyoClientError(
                f"request failed for {method} {path}: {redact_exception(exc, extra_secrets=[self.api_key])}",
                status=0,
                endpoint=path,
            ) from exc
        parsed = _parse_json(raw)
        api_ok = _api_success(parsed)
        http_ok = 200 <= status < 300
        return {
            "ok": http_ok and api_ok,
            "http_ok": http_ok,
            "api_success": api_ok,
            "status": status,
            "parsed": parsed,
            "payload": _unwrap_payload(parsed),
        }

    def health_check(self) -> Dict[str, Any]:
        for path in ("/api/v1/config", "/api/v1/strm/config", "/openapi.json"):
            try:
                res = self._request_json("GET", path, timeout=min(self.timeout, 10))
            except MuvyoClientError as exc:
                last_err = str(exc)
                continue
            if res["http_ok"]:
                return {"ok": True, "endpoint": path, "status": res["status"]}
            last_err = f"http {res['status']} on {path}"
        return {"ok": False, "error": redact_sensitive_text(locals().get("last_err", "unreachable"))}

    def search_resources(
        self,
        keyword: str,
        channels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"keyword": str(keyword or "").strip()}
        if channels:
            body["channels"] = [str(c).strip() for c in channels if str(c).strip()]
        res = self._request_json("POST", "/api/v1/resource-search/search", body)
        items = _extract_search_rows(res["payload"])
        return {
            "ok": res["ok"],
            "status": res["status"],
            "items": items,
            "result_count": len(items),
        }

    def parse_share(self, share_url: str, receive_code: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"share_url": share_url}
        if receive_code:
            body["receive_code"] = receive_code
        res = self._request_json("POST", "/api/v1/share-transfer/parse", body)
        payload = res["payload"] if isinstance(res["payload"], dict) else {}
        share_code = _first_present(payload, ("share_code", "shareCode", "shareId", "share_id"))
        rec_code = _first_present(payload, ("receive_code", "receiveCode", "password", "pwd")) or receive_code
        return {
            "ok": res["ok"] and bool(share_code),
            "status": res["status"],
            "share_code": share_code,
            "receive_code": rec_code,
        }

    def browse_share(
        self,
        share_code: str,
        receive_code: str = "",
        cid: str = "",
        limit: int = 1150,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "share_code": share_code,
            "cid": str(cid or "0"),
            "limit": max(1, int(limit or 1150)),
        }
        if receive_code:
            body["receive_code"] = receive_code
        if self.storage:
            body["storage"] = self.storage
        res = self._request_json("POST", "/api/v1/share-transfer/browse", body)
        items = _extract_rows(res["payload"])
        return {
            "ok": res["ok"],
            "status": res["status"],
            "items": items,
            "item_count": len(items),
        }

    def receive_share(
        self,
        share_code: str,
        file_ids: Sequence[str],
        target_path: str,
        receive_code: str = "",
    ) -> Dict[str, Any]:
        clean_ids = [str(fid).strip() for fid in file_ids if str(fid).strip()]
        body: Dict[str, Any] = {
            "share_code": share_code,
            "file_ids": clean_ids,
            "target_path": target_path,
        }
        if receive_code:
            body["receive_code"] = receive_code
        if self.storage:
            body["storage"] = self.storage
        res = self._request_json("POST", "/api/v1/share-transfer/receive", body)
        return {
            "ok": res["ok"],
            "status": res["status"],
            "file_id_count": len(clean_ids),
            "target_path": target_path,
            "response": redact_sensitive_data(res["payload"]),
        }

    def browse_cloud_folder(
        self,
        folder_id: str = "",
        path: str = "",
        limit: int = 1150,
    ) -> Dict[str, Any]:
        cid = str(folder_id or "").strip()
        if not cid and path:
            info = self.get_cloud_info(path=path)
            cid = _extract_folder_id(info.get("payload"))
        if not cid:
            return {"ok": False, "status": 0, "folder_id": "", "items": [], "error": "folder_id_not_found"}

        query = urllib.parse.urlencode({"cid": cid, "limit": max(1, int(limit or 1150)), "storage": self.storage})
        res = self._request_json("GET", f"/api/v1/files/cloud/browse?{query}")
        if not res["http_ok"]:
            res = self._request_json("GET", f"/api/v1/files/115/browse?{query}")
        items = _extract_rows(res["payload"])
        return {
            "ok": res["ok"],
            "status": res["status"],
            "folder_id": cid,
            "items": items,
            "item_count": len(items),
        }

    def get_cloud_info(self, path: str = "", file_id: str = "") -> Dict[str, Any]:
        params: Dict[str, str] = {"storage": self.storage}
        if file_id:
            params["file_id"] = str(file_id)
        if path:
            params["path"] = str(path)
        query = urllib.parse.urlencode(params)
        res = self._request_json("GET", f"/api/v1/files/cloud/info?{query}")
        return {
            "ok": res["ok"],
            "status": res["status"],
            "payload": res["payload"] if isinstance(res["payload"], dict) else {},
        }

    def _find_child_folder(self, parent_id: str, name: str) -> str:
        query = urllib.parse.urlencode({"cid": parent_id, "limit": 1000, "storage": self.storage})
        res = self._request_json("GET", f"/api/v1/files/115/browse?{query}")
        if not res["http_ok"]:
            res = self._request_json("GET", f"/api/v1/files/cloud/browse?{query}")
        if not res["http_ok"]:
            return ""
        for row in _extract_rows(res["payload"]):
            if _row_is_non_folder(row):
                continue
            if _row_name(row) == name:
                fid = _extract_folder_id(row) or _row_file_id(row)
                if fid:
                    return fid
        return ""

    def ensure_115_folder(
        self,
        target_path: str,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        segments = [seg for seg in str(target_path or "").strip("/").split("/") if seg]
        if not segments:
            raise ValueError("target_path must contain at least one segment")
        full_target = "/" + "/".join(segments)
        parent_id = "0"
        steps: List[Dict[str, Any]] = []
        current_path = ""
        for seg in segments:
            if should_stop is not None and should_stop():
                return {
                    "ok": False,
                    "aborted": True,
                    "target_path": full_target,
                    "final_folder_id": "",
                    "steps": steps,
                }
            current_path = f"{current_path}/{seg}"
            existing = self._find_child_folder(parent_id, seg)
            if existing:
                parent_id = existing
                steps.append({"path": current_path, "action": "reused", "folder_id": parent_id})
                continue
            if should_stop is not None and should_stop():
                return {
                    "ok": False,
                    "aborted": True,
                    "target_path": full_target,
                    "final_folder_id": "",
                    "steps": steps,
                }
            body: Dict[str, Any] = {"parent_id": parent_id, "name": seg}
            if self.storage:
                body["storage"] = self.storage
            res = self._request_json("POST", "/api/v1/files/115/folder", body)
            folder_id = _extract_folder_id(res["payload"])
            if res["ok"] and not folder_id:
                folder_id = self._find_child_folder(parent_id, seg)
            ok = bool(res["ok"] and folder_id)
            steps.append({"path": current_path, "action": "created", "ok": ok, "folder_id": folder_id})
            if not ok:
                return {
                    "ok": False,
                    "target_path": full_target,
                    "final_folder_id": "",
                    "steps": steps,
                }
            parent_id = folder_id
        return {
            "ok": True,
            "target_path": full_target,
            "final_folder_id": parent_id,
            "steps": steps,
        }

    def organize_transfer(
        self,
        *,
        files: List[Dict[str, Any]],
        target_dir: str,
        strm_dir: str,
        tmdb_id: int,
        mode: str = "move",
        is_cloud_target: bool = True,
        copy_subtitles: bool = False,
        enable_primary_category: bool = True,
        enable_secondary_category: bool = True,
        background: bool = False,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        if mode not in {"move", "copy"}:
            raise ValueError(f"unsupported organize mode: {mode}")
        body: Dict[str, Any] = {
            "files": files,
            "target_dir": target_dir,
            "is_cloud_target": bool(is_cloud_target),
            "mode": mode,
            "strm_dir": strm_dir,
            "tmdb_id": int(tmdb_id),
            "enable_primary_category": bool(enable_primary_category),
            "enable_secondary_category": bool(enable_secondary_category),
            "copy_subtitles": bool(copy_subtitles),
            "copy_non_media": False,
            "background": bool(background),
        }
        res = self._request_json("POST", "/api/v1/organize/transfer", body, timeout=timeout)
        outcome = _evaluate_organize_outcome(
            res["payload"], expected_file_count=len(files), target_dir=target_dir
        )
        return {
            "ok": bool(res["ok"]) and bool(outcome["ok"]),
            "status": res["status"],
            "outcome": outcome,
            "blockers": outcome["blockers"],
            "resolved_target_dirs": outcome["resolved_target_dirs"],
            "response": redact_sensitive_data(res["payload"], extra_secrets=[self.api_key]),
        }

    def generate_strm(
        self,
        *,
        source_dir: str,
        target_dir: str,
        cloud: bool = True,
        incremental: bool = True,
        overwrite: bool = False,
        enable_primary_category: bool = True,
        enable_secondary_category: bool = True,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "source_dir": source_dir,
            "target_dir": target_dir,
            "cloud": bool(cloud),
            "storage": self.storage or None,
            "incremental": bool(incremental),
            "overwrite": bool(overwrite),
            "organize": False,
            "openlist": False,
            "enable_primary_category": bool(enable_primary_category),
            "enable_secondary_category": bool(enable_secondary_category),
        }
        res = self._request_json("POST", "/api/v1/strm/generate", body, timeout=timeout)
        return {
            "ok": res["ok"],
            "status": res["status"],
            "response": redact_sensitive_data(res["payload"]),
        }

    def build_redirect_probe_url(self, cloud_path: str, pickcode: str) -> str:
        """Build an in-memory redirect URL for ffprobe. Never log or persist this return value."""
        if not cloud_path or not pickcode or not self.base_url:
            return ""
        query = urllib.parse.urlencode({"path": cloud_path, "pickcode": pickcode})
        return f"{self.base_url}/redirect?{query}"

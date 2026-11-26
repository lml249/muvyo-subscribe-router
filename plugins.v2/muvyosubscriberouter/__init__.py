from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .client import MuvyoClient
from .completion import CompletionDecision, evaluate_subscription_completion
from .pipeline import RouterPipelineConfig, run_muvyo_routing_pipeline, validate_router_paths
from .redaction import redact_exception, redact_sensitive_data, redact_sensitive_text

try:
    from fastapi import Body, Depends, HTTPException
except ImportError:  # pragma: no cover - fallback when FastAPI is unavailable in standalone tests
    HTTPException = None  # type: ignore[assignment,misc]

    class _FallbackDepends:
        def __init__(self, dependency: Any = None, **kwargs: Any) -> None:
            self.dependency = dependency

    def Depends(dependency: Any = None, **kwargs: Any) -> Any:  # type: ignore[misc]
        return _FallbackDepends(dependency=dependency, **kwargs)

    def Body(default: Any = None, **kwargs: Any) -> Any:  # type: ignore[misc]
        return default

try:
    from app.core.event import Event, eventmanager
    from app.db.subscribe_oper import SubscribeOper
    from app.chain.subscribe import SubscribeChain
    from app.chain.tmdb import TmdbChain
    from app.log import logger
    from app.plugins import _PluginBase
    from app.schemas.types import EventType, MediaType, NotificationType
    _HAS_MOVIEPILOT_HOST = True
except ImportError:  # pragma: no cover - fallback for standalone unit testing without MoviePilot host
    _HAS_MOVIEPILOT_HOST = False
    logger = logging.getLogger("muvyosubscriberouter")

    class _FallbackEvent:
        def __init__(self, event_type: Any = None, event_data: Optional[Dict[str, Any]] = None) -> None:
            self.event_type = event_type
            self.event_data = event_data or {}

    class _FallbackEventManager:
        def register(self, event_type: Any):
            def _decorator(fn):
                setattr(fn, "_registered_event_type", event_type)
                return fn
            return _decorator

        def send_event(self, event_type: Any, data: Optional[Dict[str, Any]] = None) -> None:
            return None

    class _FallbackEventType:
        SubscribeAdded = "subscribe.added"
        SubscribeComplete = "subscribe.complete"

    class _FallbackMediaType:
        TV = "电视剧"
        MOVIE = "电影"

    class _FallbackNotificationType:
        Subscribe = "subscribe"

    class _FallbackPluginBase:
        def __init__(self) -> None:
            self._store: Dict[str, Any] = {}

        def update_config(self, config: Dict[str, Any]) -> None:
            self._store["_config"] = dict(config)

        def save_data(self, key: str, value: Any) -> None:
            self._store[key] = value

        def get_data(self, key: str) -> Any:
            return self._store.get(key)

        def post_message(self, mtype: Any = None, title: str = "", text: str = "", **kwargs: Any) -> None:
            return None

    class _FallbackSubscribeOper:
        def get(self, sid: int) -> Any:
            return None

        def update(self, sid: int, payload: Dict[str, Any]) -> Any:
            return None

        def delete(self, sid: int) -> Any:
            return None

        def add_history(self, **kwargs: Any) -> Any:
            return None

    class _FallbackSubscribeChain:
        def search(self, sid: Optional[int] = None, state: str = "N", manual: bool = False) -> None:
            return None

    class _FallbackTmdbChain:
        def tmdb_info(self, tmdbid: int, mtype: Any) -> Any:
            return None

    Event = _FallbackEvent  # type: ignore[assignment]
    eventmanager = _FallbackEventManager()  # type: ignore[assignment]
    EventType = _FallbackEventType  # type: ignore[assignment]
    MediaType = _FallbackMediaType  # type: ignore[assignment]
    NotificationType = _FallbackNotificationType  # type: ignore[assignment]
    _PluginBase = _FallbackPluginBase  # type: ignore[assignment,misc]
    SubscribeOper = _FallbackSubscribeOper  # type: ignore[assignment,misc]
    SubscribeChain = _FallbackSubscribeChain  # type: ignore[assignment,misc]
    TmdbChain = _FallbackTmdbChain  # type: ignore[assignment,misc]

try:
    from app.db.user_oper import get_current_admin
except ImportError:  # pragma: no cover - MoviePilot V2 compatibility or standalone unit testing
    try:
        from app.core.security import get_current_admin
    except ImportError:
        if _HAS_MOVIEPILOT_HOST:
            def get_current_admin():  # type: ignore[misc]
                if HTTPException is not None:
                    raise HTTPException(status_code=503, detail="admin_auth_unavailable")
                raise RuntimeError("admin_auth_unavailable")
        else:
            def get_current_admin():  # type: ignore[misc]
                return {"username": "admin"}


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"false", "0", "no", "off", ""}:
        return False
    if text in {"true", "1", "yes", "on"}:
        return True
    return default


def _split_csv(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").replace("，", ",").split(",") if part.strip()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MuvyoSubscribeRouter(_PluginBase):
    plugin_name = "Muvyo 完结订阅分流"
    plugin_desc = "监听 MoviePilot 新增订阅，对已完结剧集（及可选电影）优先通过外置 Muvyo/MV3 盘搜、115 转存、内封中文字幕门禁与整理生成 STRM，失败无缝回退 PT。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png"
    plugin_version = "0.2.1"
    plugin_author = "lml249"
    author_url = "https://github.com/lml249/muvyo-subscribe-router"
    plugin_config_prefix = "muvyosubscriberouter_"
    plugin_order = 20
    auth_level = 2

    def __init__(self) -> None:
        super().__init__()
        self._enabled: bool = False
        self._mv_base_url: str = ""
        self._mv_api_key: str = ""
        self._mv_auth_scheme: str = "x-api-key"
        self._mv_storage: str = "115-default"
        self._mv_timeout: int = 30
        self._handle_movies: bool = False
        self._skip_best_version: bool = True
        self._skip_version_rules: bool = True
        self._allow_completed_latest_season: bool = True
        self._latest_season_quiet_days: int = 30
        self._user_allowlist: List[str] = []
        self._user_blocklist: List[str] = []
        self._staging_root: str = "/未整理/mp-router"
        self._cloud_organize_root: str = "/已整理"
        self._mv3_strm_root: str = "/strm"
        self._subtitle_policy: str = "auto"
        self._ffprobe_binary: str = ""
        self._ffprobe_timeout: int = 45
        self._ffprobe_concurrency: int = 2
        self._search_channels: List[str] = []
        self._max_candidates: int = 8
        self._max_drill_depth: int = 3
        self._trigger_strm_generate: bool = False
        self._on_subtitle_gate_fail: str = "keep_paused"
        self._on_success_action: str = "complete"
        self._trigger_pt_search_on_fallback: bool = False
        self._notify: bool = True
        self._dry_run: bool = False

        self._lock = threading.Lock()
        self._inflight_sids: set[int] = set()
        self._pending_futures: Dict[Any, Tuple[int, int]] = {}
        self._stopping = threading.Event()
        self._generation: int = 0
        self._executor: Optional[ThreadPoolExecutor] = None

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.stop_service()
        cfg = config or {}
        enabled = _parse_bool(cfg.get("enabled"), False)
        self._mv_base_url = str(cfg.get("mv_base_url") or "").strip().rstrip("/")
        self._mv_api_key = str(cfg.get("mv_api_key") or "").strip()
        scheme = str(cfg.get("mv_auth_scheme") or "x-api-key").strip().lower()
        self._mv_auth_scheme = scheme if scheme in {"x-api-key", "bearer"} else "x-api-key"
        self._mv_storage = str(cfg.get("mv_storage") or "115-default").strip()
        self._mv_timeout = max(5, int(cfg.get("mv_timeout") or 30))
        self._handle_movies = _parse_bool(cfg.get("handle_movies"), False)
        self._skip_best_version = _parse_bool(cfg.get("skip_best_version"), True)
        self._skip_version_rules = _parse_bool(cfg.get("skip_version_rules"), True)
        self._allow_completed_latest_season = _parse_bool(
            cfg.get("allow_completed_latest_season"), True
        )
        raw_quiet = cfg.get("latest_season_quiet_days")
        try:
            quiet_days = int(raw_quiet) if raw_quiet not in (None, "") else 30
        except (TypeError, ValueError, OverflowError):
            quiet_days = 30
        self._latest_season_quiet_days = max(7, min(365, quiet_days))
        self._user_allowlist = _split_csv(cfg.get("user_allowlist"))
        self._user_blocklist = _split_csv(cfg.get("user_blocklist"))
        self._staging_root = str(cfg.get("staging_root") or "/未整理/mp-router").strip()
        self._cloud_organize_root = str(cfg.get("cloud_organize_root") or "/已整理").strip()
        self._mv3_strm_root = str(cfg.get("mv3_strm_root") or "/strm").strip()
        pol = str(cfg.get("subtitle_policy") or "auto").strip().lower()
        self._subtitle_policy = pol if pol in {"auto", "required"} else "auto"
        self._ffprobe_binary = str(cfg.get("ffprobe_binary") or "").strip()
        self._ffprobe_timeout = max(5, int(cfg.get("ffprobe_timeout") or 45))
        self._ffprobe_concurrency = max(1, min(4, int(cfg.get("ffprobe_concurrency") or 2)))
        self._search_channels = _split_csv(cfg.get("search_channels"))
        self._max_candidates = max(1, min(20, int(cfg.get("max_candidates") or 8)))
        self._max_drill_depth = max(1, min(6, int(cfg.get("max_drill_depth") or 3)))
        self._trigger_strm_generate = _parse_bool(cfg.get("trigger_strm_generate"), False)
        fail_act = str(cfg.get("on_subtitle_gate_fail") or "keep_paused").strip().lower()
        self._on_subtitle_gate_fail = fail_act if fail_act in {"keep_paused", "fallback_pt"} else "keep_paused"
        ok_act = str(cfg.get("on_success_action") or "complete").strip().lower()
        self._on_success_action = ok_act if ok_act in {"complete", "pause"} else "complete"
        self._trigger_pt_search_on_fallback = _parse_bool(
            cfg.get("trigger_pt_search_on_fallback"), False
        )
        self._notify = _parse_bool(cfg.get("notify"), True)
        self._dry_run = _parse_bool(cfg.get("dry_run"), False)

        with self._lock:
            self._stopping = threading.Event()
            self._enabled = enabled
            if enabled:
                self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="muvyo-router")

    def get_state(self) -> bool:
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_render_mode(self) -> Tuple[str, Optional[str]]:
        return "vuetify", None

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/health",
                "endpoint": self.api_health,
                "methods": ["GET"],
                "auth": "bear",
                "dependencies": [Depends(get_current_admin)],
                "summary": "检测外置 Muvyo/MV3 连通性与路径配置守卫",
            },
            {
                "path": "/dry-run",
                "endpoint": self.api_dry_run,
                "methods": ["POST"],
                "auth": "bear",
                "dependencies": [Depends(get_current_admin)],
                "summary": "对指定媒体执行只读盘搜与分享预览试跑（不落云盘）",
            },
        ]

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "dry_run", "label": "仅试跑 (Dry-Run)"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify", "label": "发送脱敏通知"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "handle_movies", "label": "支持电影分流"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 5}, "content": [
                                {"component": "VTextField", "props": {"model": "mv_base_url", "label": "Muvyo/MV3 地址", "placeholder": "http://mv3.example.local:7811"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "mv_api_key", "label": "Muvyo API Key", "type": "password"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSelect", "props": {"model": "mv_auth_scheme", "label": "鉴权方式", "items": [
                                    {"title": "X-API-Key", "value": "x-api-key"},
                                    {"title": "Bearer Token", "value": "bearer"},
                                ]}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "staging_root", "label": "115 Staging 根目录", "placeholder": "/未整理/mp-router"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "cloud_organize_root", "label": "云盘整理根（勿追加 /series）", "placeholder": "/已整理"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "mv3_strm_root", "label": "明确 STRM 根（勿填裸 /series）", "placeholder": "/strm"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {"model": "subtitle_policy", "label": "内封中文字幕门禁策略", "items": [
                                    {"title": "auto（外语须内封中字，中文音轨豁免）", "value": "auto"},
                                    {"title": "required（每集强制内封中字）", "value": "required"},
                                ]}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {"model": "on_subtitle_gate_fail", "label": "字幕门禁失败策略（不删 staging）", "items": [
                                    {"title": "保持暂停等待人工复核 (keep_paused)", "value": "keep_paused"},
                                    {"title": "恢复订阅回退 PT (fallback_pt)", "value": "fallback_pt"},
                                ]}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VSelect", "props": {"model": "on_success_action", "label": "分流成功后订阅动作", "items": [
                                    {"title": "写入历史并归档删除订阅 (complete)", "value": "complete"},
                                    {"title": "仅保持暂停状态 (pause)", "value": "pause"},
                                ]}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "skip_best_version", "label": "跳过洗版订阅"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "skip_version_rules", "label": "跳过多版本订阅"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "trigger_strm_generate", "label": "显式触发 STRM 生成"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "trigger_pt_search_on_fallback", "label": "回退 PT 时立即搜索"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VSwitch", "props": {"model": "allow_completed_latest_season", "label": "连载剧最新季静默完播分流"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "latest_season_quiet_days", "label": "最新季静默完播天数 (7-365)", "type": "number", "placeholder": "30"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "user_allowlist", "label": "用户白名单（逗号分隔，空=全部）"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "user_blocklist", "label": "用户黑名单（逗号分隔）"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "mv_storage", "label": "115 存储标识", "placeholder": "115-default"}}
                            ]},
                        ],
                    },
                ],
            }
        ]
        defaults = {
            "enabled": False,
            "mv_base_url": "",
            "mv_api_key": "",
            "mv_auth_scheme": "x-api-key",
            "mv_storage": "115-default",
            "mv_timeout": 30,
            "handle_movies": False,
            "skip_best_version": True,
            "skip_version_rules": True,
            "allow_completed_latest_season": True,
            "latest_season_quiet_days": 30,
            "user_allowlist": "",
            "user_blocklist": "",
            "staging_root": "/未整理/mp-router",
            "cloud_organize_root": "/已整理",
            "mv3_strm_root": "/strm",
            "subtitle_policy": "auto",
            "ffprobe_binary": "",
            "ffprobe_timeout": 45,
            "ffprobe_concurrency": 2,
            "search_channels": "",
            "max_candidates": 8,
            "max_drill_depth": 3,
            "trigger_strm_generate": False,
            "on_subtitle_gate_fail": "keep_paused",
            "on_success_action": "complete",
            "trigger_pt_search_on_fallback": False,
            "notify": True,
            "dry_run": False,
        }
        return form, defaults

    def get_page(self) -> List[Dict[str, Any]]:
        history: List[Dict[str, Any]] = self.get_data("history") or []
        reviews: List[Dict[str, Any]] = self.get_data("manual_reviews") or []
        routed_ok = sum(1 for item in history if item.get("status") == "routed_ok")
        fallback_cnt = sum(1 for item in history if item.get("status") == "fallback_pt")
        passthrough_cnt = sum(1 for item in history if item.get("status") == "passthrough_pt")
        review_cnt = len(reviews)

        rows = []
        for item in history[:30]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(item.get("timestamp") or "")},
                    {"component": "td", "text": str(item.get("title") or "")},
                    {"component": "td", "text": str(item.get("status") or "")},
                    {"component": "td", "text": str(item.get("reason") or "")},
                ],
            })

        review_rows = []
        for item in reviews[:30]:
            review_rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": str(item.get("timestamp") or "")},
                    {"component": "td", "text": f"#{item.get('subscribe_id', '')} {item.get('title', '')} (S{int(item.get('season') or 0):02d})"},
                    {"component": "td", "text": str(item.get("tmdb_id") or "")},
                    {"component": "td", "text": str(item.get("staging_path") or "-")},
                    {"component": "td", "text": str(item.get("reason") or "")},
                ],
            })

        manual_review_section: Dict[str, Any] = {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mt-4"},
            "content": [
                {"component": "VCardTitle", "text": "待人工复核列表（Staging 保留，未自动删除）"},
                {
                    "component": "VCardText",
                    "content": [
                        {
                            "component": "VAlert",
                            "props": {"type": "success", "variant": "tonal", "text": "当前无待人工复核条目。"},
                        }
                    ] if not review_rows else [
                        {
                            "component": "VTable",
                            "content": [
                                {
                                    "component": "thead",
                                    "content": [{
                                        "component": "tr",
                                        "content": [
                                            {"component": "th", "text": "时间"},
                                            {"component": "th", "text": "订阅 / 标题"},
                                            {"component": "th", "text": "TMDB ID"},
                                            {"component": "th", "text": "Staging 路径"},
                                            {"component": "th", "text": "阻断原因"},
                                        ],
                                    }],
                                },
                                {"component": "tbody", "content": review_rows},
                            ],
                        }
                    ],
                },
            ],
        }

        return [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VAlert", "props": {"type": "success", "variant": "tonal", "text": f"分流成功：{routed_ok}"}}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": f"回退 PT：{fallback_cnt}"}}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VAlert", "props": {"type": "secondary", "variant": "tonal", "text": f"直接放行 PT：{passthrough_cnt}"}}
                    ]},
                    {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                        {"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": f"待人工复核 (staging)：{review_cnt}"}}
                    ]},
                ],
            },
            manual_review_section,
            {
                "component": "VCard",
                "props": {"variant": "outlined", "class": "mt-4"},
                "content": [
                    {"component": "VCardTitle", "text": "最近处理历史"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VTable",
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [{
                                            "component": "tr",
                                            "content": [
                                                {"component": "th", "text": "时间"},
                                                {"component": "th", "text": "标题"},
                                                {"component": "th", "text": "结果"},
                                                {"component": "th", "text": "原因"},
                                            ],
                                        }],
                                    },
                                    {"component": "tbody", "content": rows},
                                ],
                            }
                        ],
                    },
                ],
            },
        ]

    def stop_service(self) -> None:
        with self._lock:
            self._stopping.set()
            self._enabled = False
            self._generation += 1
            executor = self._executor
            self._executor = None
            pending_items = list(self._pending_futures.items())
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        # Invoke cancel()/cancelled() outside self._lock because Future.cancel()
        # synchronously invokes done callbacks (which acquire self._lock).
        cancelled_entries: List[Tuple[Any, int, int]] = []
        for fut, (sid, tmdb_id) in pending_items:
            is_cancelled = False
            if hasattr(fut, "cancel"):
                try:
                    if fut.cancel():
                        is_cancelled = True
                except Exception:
                    pass
            if not is_cancelled and hasattr(fut, "cancelled"):
                try:
                    if fut.cancelled():
                        is_cancelled = True
                except Exception:
                    pass
            if is_cancelled:
                cancelled_entries.append((fut, sid, tmdb_id))
        cancelled_subs: List[Tuple[int, int]] = []
        with self._lock:
            for fut, sid, tmdb_id in cancelled_entries:
                self._pending_futures.pop(fut, None)
                if sid in self._inflight_sids:
                    self._inflight_sids.discard(sid)
                    cancelled_subs.append((sid, tmdb_id))
        for sid, tmdb_id in cancelled_subs:
            if self._restore_subscribe_to_pt(sid, expected_tmdb_id=tmdb_id, allow_pt_search=False):
                logger.info(
                    f"[MuvyoSubscribeRouter] restored cancelled queued subscribe {sid} to state N on stop_service"
                )

    def _is_stopped(
        self,
        generation: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        if generation is not None and generation != self._generation:
            return True
        return self._stopping.is_set() or not self._enabled

    def _build_client(self) -> MuvyoClient:
        return MuvyoClient(
            base_url=self._mv_base_url,
            api_key=self._mv_api_key,
            auth_scheme=self._mv_auth_scheme,
            timeout=self._mv_timeout,
            storage=self._mv_storage,
        )

    def _build_pipeline_config(self, dry_run_override: Optional[bool] = None) -> RouterPipelineConfig:
        return RouterPipelineConfig(
            staging_root=self._staging_root,
            cloud_organize_root=self._cloud_organize_root,
            mv3_strm_root=self._mv3_strm_root,
            subtitle_policy=self._subtitle_policy,
            ffprobe_binary=self._ffprobe_binary,
            ffprobe_timeout=self._ffprobe_timeout,
            ffprobe_concurrency=self._ffprobe_concurrency,
            search_channels=list(self._search_channels),
            max_candidates=self._max_candidates,
            max_drill_depth=self._max_drill_depth,
            trigger_strm_generate=self._trigger_strm_generate,
            dry_run=self._dry_run if dry_run_override is None else bool(dry_run_override),
        )

    def _user_allowed(self, username: str) -> bool:
        user = str(username or "").strip()
        if self._user_allowlist and user not in self._user_allowlist:
            return False
        if self._user_blocklist and user in self._user_blocklist:
            return False
        return True

    def _record_history(self, entry: Dict[str, Any]) -> None:
        sanitized = redact_sensitive_data(entry, extra_secrets=[self._mv_api_key])
        with self._lock:
            history: List[Dict[str, Any]] = list(self.get_data("history") or [])
            history.insert(0, sanitized)
            self.save_data("history", history[:100])
            if sanitized.get("status") == "manual_review_staging":
                reviews: List[Dict[str, Any]] = list(self.get_data("manual_reviews") or [])
                reviews.insert(0, sanitized)
                self.save_data("manual_reviews", reviews[:50])

    def _send_notice(self, title: str, text: str) -> None:
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.Subscribe,
                title=redact_sensitive_text(title, max_length=120, extra_secrets=[self._mv_api_key]),
                text=redact_sensitive_text(text, max_length=600, extra_secrets=[self._mv_api_key]),
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(
                f"[MuvyoSubscribeRouter] notify failed: {redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )

    @staticmethod
    def _sub_attr(sub: Any, key: str, default: Any = None) -> Any:
        if sub is None:
            return default
        if isinstance(sub, dict):
            return sub.get(key, default)
        return getattr(sub, key, default)

    def _is_subscribe_still_paused(self, oper: Any, subscribe_id: int, expected_tmdb_id: int = 0) -> bool:
        current = oper.get(subscribe_id)
        if current is None:
            logger.info(f"[MuvyoSubscribeRouter] subscribe {subscribe_id} no longer exists; skip mutation")
            return False
        current_state = str(self._sub_attr(current, "state") or "").strip().upper()
        if current_state != "S":
            logger.info(
                f"[MuvyoSubscribeRouter] subscribe {subscribe_id} state drifted to '{current_state}'; skip mutation"
            )
            return False
        if expected_tmdb_id > 0:
            cur_tmdb = int(self._sub_attr(current, "tmdbid") or 0)
            if cur_tmdb and cur_tmdb != expected_tmdb_id:
                logger.info(
                    f"[MuvyoSubscribeRouter] subscribe {subscribe_id} tmdbid drifted ({cur_tmdb} != {expected_tmdb_id}); skip mutation"
                )
                return False
        return True

    def _restore_subscribe_to_pt(
        self,
        subscribe_id: int,
        expected_tmdb_id: int = 0,
        allow_pt_search: Optional[bool] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> bool:
        oper = SubscribeOper()
        try:
            if not self._is_subscribe_still_paused(oper, subscribe_id, expected_tmdb_id=expected_tmdb_id):
                return False
            oper.update(subscribe_id, {"state": "N"})
        except Exception as exc:
            logger.error(
                f"[MuvyoSubscribeRouter] failed to restore subscribe {subscribe_id}: "
                f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )
            return False
        stopped = should_stop() if should_stop is not None else self._is_stopped()
        if self._trigger_pt_search_on_fallback and allow_pt_search is not False and not stopped:
            try:
                SubscribeChain().search(sid=subscribe_id, state="N", manual=False)
            except Exception as exc:
                logger.warning(
                    f"[MuvyoSubscribeRouter] fallback PT search failed: "
                    f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
                )
        return True

    def _complete_subscribe(
        self,
        subscribe_id: int,
        subscribe: Any,
        mediainfo: Any,
        expected_tmdb_id: int = 0,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Tuple[bool, str]:
        if should_stop is not None and should_stop():
            return False, "aborted_stopped"
        if self._on_success_action == "pause":
            return True, "paused"
        oper = SubscribeOper()
        if not self._is_subscribe_still_paused(oper, subscribe_id, expected_tmdb_id=expected_tmdb_id):
            return False, "subscribe_state_drifted"
        if should_stop is not None and should_stop():
            return False, "aborted_stopped"
        try:
            sub_dict = subscribe if isinstance(subscribe, dict) else getattr(subscribe, "__dict__", {})
            history_payload = {
                k: v for k, v in sub_dict.items()
                if not str(k).startswith("_") and k != "id"
            }
            for field in ("name", "description"):
                if isinstance(history_payload.get(field), str) and history_payload[field]:
                    history_payload[field] = redact_sensitive_text(
                        history_payload[field],
                        max_length=0,
                        extra_secrets=[self._mv_api_key],
                    )
            history_payload["date"] = _now_iso()
            hist_res = oper.add_history(**history_payload)
            if hist_res is False:
                logger.error(f"[MuvyoSubscribeRouter] add_history returned False for {subscribe_id}; abort delete")
                return False, "add_history_failed"
        except Exception as exc:
            logger.error(
                f"[MuvyoSubscribeRouter] add_history failed for {subscribe_id}: "
                f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )
            return False, "add_history_failed"

        try:
            del_res = oper.delete(subscribe_id)
            if del_res is False:
                logger.error(f"[MuvyoSubscribeRouter] delete subscribe {subscribe_id} returned False; abort SubscribeComplete")
                return False, "delete_subscribe_failed"
        except Exception as exc:
            logger.error(
                f"[MuvyoSubscribeRouter] delete subscribe {subscribe_id} failed: "
                f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )
            return False, "delete_subscribe_failed"

        try:
            eventmanager.send_event(
                EventType.SubscribeComplete,
                {"subscribe_id": subscribe_id, "subscribe_info": subscribe, "mediainfo": mediainfo},
            )
        except Exception as exc:
            logger.warning(
                f"[MuvyoSubscribeRouter] send SubscribeComplete failed: "
                f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )
        return True, "completed"

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event) -> None:
        if not self._enabled or self._stopping.is_set():
            return
        if not self._mv_base_url:
            return
        data = getattr(event, "event_data", None) or {}
        subscribe_id = int(data.get("subscribe_id") or 0)
        if subscribe_id <= 0:
            return
        username = str(data.get("username") or "").strip()
        if not self._user_allowed(username):
            return

        oper = SubscribeOper()
        subscribe = oper.get(subscribe_id)
        if subscribe is None:
            return
        mediainfo = data.get("mediainfo")

        def _tmdb_fallback(tid: int, kind: str) -> Any:
            try:
                mtype = MediaType.MOVIE if kind == "movie" else MediaType.TV
                return TmdbChain().tmdb_info(tmdbid=tid, mtype=mtype)
            except Exception:
                return None

        expected_tmdb_id = int(self._sub_attr(subscribe, "tmdbid") or 0)
        decision = evaluate_subscription_completion(
            subscribe,
            mediainfo,
            handle_movies=self._handle_movies,
            skip_best_version=self._skip_best_version,
            skip_version_rules=self._skip_version_rules,
            allow_completed_latest_season=self._allow_completed_latest_season,
            latest_season_quiet_days=self._latest_season_quiet_days,
            tmdb_fallback_fetcher=_tmdb_fallback,
        )
        safe_title = redact_sensitive_text(decision.title, extra_secrets=[self._mv_api_key])
        safe_reason = redact_sensitive_text(decision.reason, extra_secrets=[self._mv_api_key])
        if not decision.eligible:
            logger.info(
                f"[MuvyoSubscribeRouter] passthrough subscribe #{subscribe_id} "
                f"({safe_title} S{decision.season:02d}): {safe_reason}"
            )
            self._record_history({
                "timestamp": _now_iso(),
                "subscribe_id": subscribe_id,
                "tmdb_id": expected_tmdb_id,
                "title": decision.title,
                "season": decision.season,
                "media_kind": decision.media_kind,
                "status": "passthrough_pt",
                "reason": decision.reason,
            })
            return

        with self._lock:
            if subscribe_id in self._inflight_sids:
                return
            self._inflight_sids.add(subscribe_id)

        logger.info(
            f"[MuvyoSubscribeRouter] routing subscribe #{subscribe_id} "
            f"({safe_title} S{decision.season:02d}): {safe_reason}, "
            f"expected_eps={decision.expected_episodes}"
        )
        # Synchronously pause subscription first to prevent MP PT search race condition
        try:
            oper.update(subscribe_id, {"state": "S"})
        except Exception as exc:
            with self._lock:
                self._inflight_sids.discard(subscribe_id)
            logger.error(
                f"[MuvyoSubscribeRouter] pause subscribe {subscribe_id} failed: "
                f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )
            return

        with self._lock:
            executor = self._executor
            gen = self._generation
            gen_stop_event = self._stopping
        if executor is None or self._is_stopped(generation=gen, stop_event=gen_stop_event):
            try:
                self._restore_subscribe_to_pt(
                    subscribe_id, expected_tmdb_id=expected_tmdb_id, allow_pt_search=False
                )
            finally:
                with self._lock:
                    self._inflight_sids.discard(subscribe_id)
            return

        try:
            future = executor.submit(
                self._process_subscription_worker,
                subscribe_id,
                subscribe,
                mediainfo,
                decision,
                gen,
                gen_stop_event,
            )
        except Exception as exc:
            logger.error(
                f"[MuvyoSubscribeRouter] submit worker failed: "
                f"{redact_exception(exc, extra_secrets=[self._mv_api_key])}"
            )
            try:
                self._restore_subscribe_to_pt(
                    subscribe_id,
                    expected_tmdb_id=expected_tmdb_id,
                    allow_pt_search=False if self._is_stopped(generation=gen, stop_event=gen_stop_event) else None,
                )
            finally:
                with self._lock:
                    self._inflight_sids.discard(subscribe_id)
            return

        if future is not None:
            with self._lock:
                stale_or_stopped = (
                    self._is_stopped(generation=gen, stop_event=gen_stop_event)
                    or (self._executor is not executor)
                )
                if not stale_or_stopped:
                    self._pending_futures[future] = (subscribe_id, expected_tmdb_id)
            already_cancelled = False
            if hasattr(future, "cancelled"):
                try:
                    already_cancelled = bool(future.cancelled())
                except Exception:
                    already_cancelled = False
            if stale_or_stopped or already_cancelled:
                is_cancelled = already_cancelled
                if not is_cancelled and hasattr(future, "cancel"):
                    try:
                        if future.cancel():
                            is_cancelled = True
                    except Exception:
                        pass
                if not is_cancelled and hasattr(future, "cancelled"):
                    try:
                        if future.cancelled():
                            is_cancelled = True
                    except Exception:
                        pass
                if is_cancelled:
                    should_restore = False
                    with self._lock:
                        self._pending_futures.pop(future, None)
                        if subscribe_id in self._inflight_sids:
                            self._inflight_sids.discard(subscribe_id)
                            should_restore = True
                    if should_restore:
                        self._restore_subscribe_to_pt(
                            subscribe_id, expected_tmdb_id=expected_tmdb_id, allow_pt_search=False
                        )
            elif hasattr(future, "add_done_callback"):
                def _cleanup_future(done_fut: Any) -> None:
                    should_restore = False
                    done_cancelled = False
                    if hasattr(done_fut, "cancelled"):
                        try:
                            done_cancelled = bool(done_fut.cancelled())
                        except Exception:
                            done_cancelled = False
                    with self._lock:
                        self._pending_futures.pop(done_fut, None)
                        if done_cancelled and subscribe_id in self._inflight_sids:
                            self._inflight_sids.discard(subscribe_id)
                            should_restore = True
                    if should_restore:
                        if self._restore_subscribe_to_pt(
                            subscribe_id, expected_tmdb_id=expected_tmdb_id, allow_pt_search=False
                        ):
                            logger.info(
                                f"[MuvyoSubscribeRouter] restored cancelled future subscribe {subscribe_id} to state N"
                            )

                future.add_done_callback(_cleanup_future)

    def _process_subscription_worker(
        self,
        subscribe_id: int,
        subscribe: Any,
        mediainfo: Any,
        decision: CompletionDecision,
        generation: Optional[int] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        tmdb_id = int(self._sub_attr(subscribe, "tmdbid") or 0)
        worker_stopped = lambda: self._is_stopped(generation=generation, stop_event=stop_event)
        try:
            if worker_stopped():
                self._restore_subscribe_to_pt(
                    subscribe_id,
                    expected_tmdb_id=tmdb_id,
                    allow_pt_search=False,
                    should_stop=worker_stopped,
                )
                self._record_history({
                    "timestamp": _now_iso(),
                    "subscribe_id": subscribe_id,
                    "tmdb_id": tmdb_id,
                    "title": decision.title,
                    "season": decision.season,
                    "status": "aborted_stopped",
                    "reason": "plugin_stopped_before_worker_start",
                })
                return

            if not self._is_subscribe_still_paused(SubscribeOper(), subscribe_id, expected_tmdb_id=tmdb_id):
                self._record_history({
                    "timestamp": _now_iso(),
                    "subscribe_id": subscribe_id,
                    "tmdb_id": tmdb_id,
                    "title": decision.title,
                    "season": decision.season,
                    "status": "skipped_state_drifted",
                    "reason": "subscribe_state_changed_before_worker_start",
                })
                return

            client = self._build_client()
            cfg = self._build_pipeline_config()
            result = run_muvyo_routing_pipeline(
                client,
                decision,
                tmdb_id=tmdb_id,
                config=cfg,
                run_id=f"sub{subscribe_id}_{int(time.time())}",
                should_stop=worker_stopped,
            )
            record = {
                "timestamp": _now_iso(),
                "subscribe_id": subscribe_id,
                "tmdb_id": tmdb_id,
                "title": decision.title,
                "season": decision.season,
                "media_kind": decision.media_kind,
                **result.to_sanitized_dict(extra_secrets=[self._mv_api_key]),
            }
            self._record_history(record)

            if result.status == "aborted_stopped":
                self._restore_subscribe_to_pt(
                    subscribe_id,
                    expected_tmdb_id=tmdb_id,
                    allow_pt_search=False,
                    should_stop=worker_stopped,
                )
                return

            if result.status == "routed_ok":
                if worker_stopped():
                    return
                ok_complete, comp_reason = self._complete_subscribe(
                    subscribe_id,
                    subscribe,
                    mediainfo,
                    expected_tmdb_id=tmdb_id,
                    should_stop=worker_stopped,
                )
                if not ok_complete and comp_reason == "aborted_stopped":
                    return
                if ok_complete:
                    self._send_notice(
                        f"Muvyo 分流成功：{decision.title}",
                        f"订阅 #{subscribe_id} 已完成 115 转存、内封中文字幕门禁与云盘整理（共 {result.video_count} 个视频）。",
                    )
                else:
                    self._send_notice(
                        f"Muvyo 分流已整理但订阅收口异常：{decision.title}",
                        f"订阅 #{subscribe_id} 云盘整理已完成，但订阅收口未完成（{comp_reason}），请人工核查订阅状态。",
                    )
            elif result.status == "manual_review_staging":
                if self._on_subtitle_gate_fail == "fallback_pt":
                    self._restore_subscribe_to_pt(
                        subscribe_id,
                        expected_tmdb_id=tmdb_id,
                        allow_pt_search=False if worker_stopped() else None,
                        should_stop=worker_stopped,
                    )
                self._send_notice(
                    f"Muvyo 待人工复核：{decision.title}",
                    f"订阅 #{subscribe_id} 停留在 staging（{result.staging_path}），原因：{result.reason}。已保留云盘文件，未自动删除。",
                )
            elif result.status == "dry_run_matched":
                self._restore_subscribe_to_pt(
                    subscribe_id,
                    expected_tmdb_id=tmdb_id,
                    allow_pt_search=False if worker_stopped() else None,
                    should_stop=worker_stopped,
                )
            else:
                self._restore_subscribe_to_pt(
                    subscribe_id,
                    expected_tmdb_id=tmdb_id,
                    allow_pt_search=False if worker_stopped() else None,
                    should_stop=worker_stopped,
                )
                self._send_notice(
                    f"Muvyo 回退 PT：{decision.title}",
                    f"订阅 #{subscribe_id} 未找到合适云盘源或执行失败（{result.reason}），已恢复为正常 PT 订阅。",
                )
        except Exception as exc:
            err = redact_exception(exc, extra_secrets=[self._mv_api_key])
            logger.error(f"[MuvyoSubscribeRouter] worker error for subscribe {subscribe_id}: {err}")
            self._restore_subscribe_to_pt(
                subscribe_id,
                expected_tmdb_id=tmdb_id,
                allow_pt_search=False if worker_stopped() else None,
                should_stop=worker_stopped,
            )
            self._record_history({
                "timestamp": _now_iso(),
                "subscribe_id": subscribe_id,
                "tmdb_id": tmdb_id,
                "title": decision.title,
                "season": decision.season,
                "status": "fallback_pt",
                "reason": f"unexpected_worker_error:{err}",
            })
        finally:
            with self._lock:
                self._inflight_sids.discard(subscribe_id)

    def api_health(self) -> Dict[str, Any]:
        path_errors = validate_router_paths(
            self._staging_root, self._cloud_organize_root, self._mv3_strm_root
        )
        if not self._mv_base_url:
            return redact_sensitive_data({
                "ok": False,
                "configured": False,
                "path_errors": path_errors,
                "error": "mv_base_url_not_configured",
            })
        hc = self._build_client().health_check()
        return redact_sensitive_data({
            "ok": bool(hc.get("ok")) and not path_errors,
            "configured": True,
            "health": hc,
            "path_errors": path_errors,
        })

    def api_dry_run(self, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        title = str(body.get("title") or "").strip()
        tmdb_id = int(body.get("tmdb_id") or 0)
        media_kind = str(body.get("media_kind") or "tv").strip().lower()
        season = int(body.get("season") or (1 if media_kind == "tv" else 0))
        expected_episodes = int(body.get("expected_episodes") or (1 if media_kind == "movie" else 0))
        if not title or tmdb_id <= 0:
            return {"ok": False, "error": "title_and_tmdb_id_required"}
        decision = CompletionDecision(
            eligible=True,
            reason="manual_dry_run",
            media_kind=media_kind if media_kind in {"tv", "movie"} else "tv",
            season=season,
            expected_episodes=expected_episodes,
            title=title,
            original_title=str(body.get("original_title") or "").strip(),
            year=str(body.get("year") or "").strip(),
        )
        res = run_muvyo_routing_pipeline(
            self._build_client(),
            decision,
            tmdb_id=tmdb_id,
            config=self._build_pipeline_config(dry_run_override=True),
        )
        return redact_sensitive_data(
            {"ok": res.status == "dry_run_matched", **res.to_sanitized_dict(extra_secrets=[self._mv_api_key])},
            extra_secrets=[self._mv_api_key],
        )

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from muvyosubscriberouter.client import MuvyoClient, MuvyoClientError
from muvyosubscriberouter.redaction import redact_sensitive_data, redact_sensitive_text


class TestRedactionAndClient(unittest.TestCase):
    def test_redaction_strips_query_headers_and_nested_keys(self) -> None:
        raw = (
            "GET http://mv3.example.local:7811/redirect?path=/a.mkv&pickcode=PC999&token=TOK123\n"
            "Authorization: Bearer TOPSECRET\n"
            "share_code=SH777 receive_code=RC888 api_key=AK555"
        )
        cleaned = redact_sensitive_text(raw)
        for secret in ("PC999", "TOK123", "TOPSECRET", "SH777", "RC888", "AK555"):
            self.assertNotIn(secret, cleaned)

        nested = {
            "ok": True,
            "mv_api_key": "KEY_RAW",
            "inner": [{"pickcode": "PC_RAW", "url": "http://mv3.example.local/x?password=PW1"}],
        }
        redacted = redact_sensitive_data(nested)
        self.assertEqual(redacted["mv_api_key"], "[REDACTED]")
        self.assertEqual(redacted["inner"][0]["pickcode"], "[REDACTED]")
        self.assertNotIn("PW1", redacted["inner"][0]["url"])

    def test_redaction_strips_inline_bearer_multiline_cookie_and_share_paths(self) -> None:
        raw = (
            "title Bearer inline_jwt.abc123.xyz "
            "and Authorization: Bearer inline_auth_999 ok\n"
            "Cookie: UID=u100; CID=c200; SEID=s300\n"
            "payload cookie=a=1; b=2; c=3 tail_text\n"
            "inline Cookie: k1=v1; k2=v2; k3=v3 end_text\n"
            "links https://share.example.local/s/demo_code123 "
            "https://115.com/s/sw3abc123?password=pwd999&receive_code=rc888 "
            "https://example.local/share_code/sc_path_777?x=1"
        )
        cleaned = redact_sensitive_text(raw)
        for secret in (
            "inline_jwt.abc123.xyz",
            "inline_auth_999",
            "u100",
            "c200",
            "s300",
            "a=1",
            "b=2",
            "c=3",
            "k1=v1",
            "k2=v2",
            "k3=v3",
            "demo_code123",
            "sw3abc123",
            "pwd999",
            "rc888",
            "sc_path_777",
        ):
            self.assertNotIn(secret, cleaned)
        self.assertIn("Bearer [REDACTED]", cleaned)
        self.assertIn("Authorization: [REDACTED]", cleaned)
        self.assertIn("Cookie: [REDACTED]", cleaned)
        self.assertIn("cookie=[REDACTED] tail_text", cleaned)
        self.assertIn("Cookie: [REDACTED] end_text", cleaned)
        self.assertIn("https://share.example.local/s/[REDACTED]", cleaned)
        self.assertIn("https://115.com/s/[REDACTED]", cleaned)
        self.assertIn("https://example.local/share_code/[REDACTED]", cleaned)
        self.assertNotIn("password=", cleaned)
        self.assertNotIn("receive_code=", cleaned)

    def test_redaction_strips_quoted_cookies_bare_share_urls_and_multiline_tokens(self) -> None:
        raw = (
            'inline Cookie: a="v1"; b=v2 tail_cookie\n'
            'inline cookie=sid="abc;123"; uid=456 end_cookie\n'
            "bare share.example.local/s/demo_bare\n"
            "bare2 115.com/s/sw3demo123\n"
            "bare3 share.example.local/s/demo123?password=abc\n"
            "bare4 example.local/share_code/bare_sc777?x=1\n"
            "password:\nSECRET_NEXT_LINE\n"
            "token=\nSECRET_TOKEN_LINE\n"
            "Authorization:\n[REDACTED]\n"
        )
        cleaned = redact_sensitive_text(raw)
        for secret in (
            "v1",
            "b=v2",
            "abc;123",
            "uid=456",
            "demo_bare",
            "sw3demo123",
            "demo123",
            "password=abc",
            "bare_sc777",
            "SECRET_NEXT_LINE",
            "SECRET_TOKEN_LINE",
        ):
            self.assertNotIn(secret, cleaned)
        self.assertIn("Cookie: [REDACTED] tail_cookie", cleaned)
        self.assertIn("cookie=[REDACTED] end_cookie", cleaned)
        self.assertIn("share.example.local/s/[REDACTED]", cleaned)
        self.assertIn("115.com/s/[REDACTED]", cleaned)
        self.assertIn("example.local/share_code/[REDACTED]", cleaned)
        self.assertIn("password:\n[REDACTED]", cleaned)
        self.assertIn("token=\n[REDACTED]", cleaned)
        self.assertIn("Authorization:\n[REDACTED]", cleaned)

    def test_redaction_strips_unquoted_keys_with_quoted_values(self) -> None:
        raw = (
            'token="RAW_Q_TOK" '
            "password='RAW_Q_PWD' "
            'api_key: "RAW_Q_KEY" '
            '{token: RAW_BRACE_TOK, next: "ok"}'
        )
        cleaned = redact_sensitive_text(raw)
        for secret in ("RAW_Q_TOK", "RAW_Q_PWD", "RAW_Q_KEY", "RAW_BRACE_TOK"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("token=[REDACTED]", cleaned)
        self.assertIn("password=[REDACTED]", cleaned)
        self.assertIn("api_key: [REDACTED]", cleaned)
        self.assertIn('{token: [REDACTED], next: "ok"}', cleaned)

    def test_redaction_strips_escaped_quotes_and_multiline_quoted_values(self) -> None:
        raw = (
            'token="a\\"b_secret" '
            "password='c\\'d_secret' "
            'token="line1\nline2_secret" '
            "secret='m1\nm2_secret' "
            'inline Cookie: sid="x\\"y\nz_secret"; uid=789_secret tail'
        )
        cleaned = redact_sensitive_text(raw)
        for secret in ("a\\\"b_secret", "b_secret", "c\\'d_secret", "d_secret", "line1", "line2_secret", "m1", "m2_secret", "z_secret", "789_secret"):
            self.assertNotIn(secret, cleaned)
        self.assertEqual(cleaned.count("token=[REDACTED]"), 2)
        self.assertIn("password=[REDACTED]", cleaned)
        self.assertIn("secret=[REDACTED]", cleaned)
        self.assertIn("Cookie: [REDACTED] tail", cleaned)

    def test_client_auth_headers_and_browse_fallback(self) -> None:
        calls: List[Tuple[str, str, Dict[str, str]]] = []

        def _transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            calls.append((method, url, headers))
            if "/api/v1/files/cloud/browse" in url:
                return 404, {}, b'{"detail":"not found"}'
            if "/api/v1/files/115/browse" in url:
                return 200, {}, json.dumps({"success": True, "data": {"items": [{"name": "Ep01.mkv", "file_id": "f1"}]}}).encode()
            return 200, {}, b'{"success": true, "data": {}}'

        client = MuvyoClient(
            "http://mv3.example.local:7811",
            api_key="demo-key-123",
            auth_scheme="bearer",
            transport=_transport,
        )
        res = client.browse_cloud_folder(folder_id="100")
        self.assertTrue(res["ok"])
        self.assertEqual(res["item_count"], 1)
        self.assertEqual(calls[0][2].get("Authorization"), "Bearer demo-key-123")
        self.assertIn("/api/v1/files/cloud/browse", calls[0][1])
        self.assertIn("/api/v1/files/115/browse", calls[1][1])

    def test_client_sanitizes_network_exception(self) -> None:
        def _boom(method, url, headers, body, timeout):
            raise OSError("failed url http://mv3.example.local/redirect?path=/a&pickcode=LEAKME")

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_boom)
        with self.assertRaises(MuvyoClientError) as ctx:
            client.search_resources("测试")
        self.assertNotIn("LEAKME", str(ctx.exception))

    def test_client_rejects_newline_api_key_and_scrubs_raw_key_from_errors(self) -> None:
        with self.assertRaises(MuvyoClientError) as ctx:
            MuvyoClient("http://mv3.example.local:7811", api_key="SUPER_SECRET_KEY\r\nX-Inject: 1")
        self.assertNotIn("SUPER_SECRET_KEY", str(ctx.exception))

        def _header_err(method, url, headers, body, timeout):
            raise ValueError("Invalid header value b'RAW_HEADER_SECRET_999'")

        client = MuvyoClient("http://mv3.example.local:7811", api_key="RAW_HEADER_SECRET_999", transport=_header_err)
        with self.assertRaises(MuvyoClientError) as ctx2:
            client.search_resources("测试")
        self.assertNotIn("RAW_HEADER_SECRET_999", str(ctx2.exception))

    def test_client_115_native_fn_fid_folder_reuse(self) -> None:
        def _transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            if "/api/v1/files/115/browse" in url and "cid=0" in url:
                return 200, {}, json.dumps({"success": True, "data": [{"fn": "未整理", "fid": "dir_unorg"}]}).encode()
            if "/api/v1/files/115/browse" in url and "cid=dir_unorg" in url:
                return 200, {}, json.dumps({"success": True, "data": [{"fn": "mp-router", "fid": "dir_router"}]}).encode()
            return 200, {}, b'{"success": true, "data": {}}'

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport)
        res = client.ensure_115_folder("/未整理/mp-router")
        self.assertTrue(res["ok"])
        self.assertEqual(res["final_folder_id"], "dir_router")
        self.assertEqual([s["action"] for s in res["steps"]], ["reused", "reused"])

    def test_organize_transfer_rejects_skipped_or_failed_inner_outcome(self) -> None:
        def _transport_skipped(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            return 200, {}, json.dumps({
                "success": True,
                "data": {
                    "success": 1,
                    "failed": 0,
                    "skipped": 1,
                    "items": [
                        {"old_path": "/a/e1.mkv", "new_path": "/已整理/series/Show/Season 01/e1.mkv", "status": "ok"},
                        {"old_path": "/a/e2.mkv", "new_path": "", "status": "skipped: already exists"},
                    ],
                },
            }).encode()

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport_skipped)
        res = client.organize_transfer(
            files=[{"name": "e1.mkv", "source_path": "/a/e1.mkv"}, {"name": "e2.mkv", "source_path": "/a/e2.mkv"}],
            target_dir="/已整理",
            strm_dir="/strm",
            tmdb_id=1001,
        )
        self.assertFalse(res["ok"])
        self.assertIn("organize_skipped_items", res["blockers"])
        self.assertIn("organize_success_count_below_expected", res["blockers"])
        self.assertEqual(res["resolved_target_dirs"], ["/已整理/series/Show/Season 01"])

    def test_search_resources_merges_multi_channel_dict_and_skips_non_dict_lists(self) -> None:
        def _transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            payload = {
                "success": True,
                "message": "ok",
                "data": {
                    "addon:demo.telegram": [
                        {"title": "示例剧 S04", "share_url": "https://share.example.local/s/demo_tg1"},
                    ],
                    "addon:demo.pansou": [
                        {
                            "title": "示例剧 S04 4K",
                            "share_url": "https://share.example.local/s/demo_ps1",
                            "channel": "custom_pansou",
                        },
                    ],
                    "moviepilot": [],
                    "mp_fallback": False,
                    "enabled": ["addon:demo.telegram", "addon:demo.pansou"],
                    "labels": {"addon:demo.telegram": "TG"},
                },
            }
            return 200, {}, json.dumps(payload).encode()

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport)
        res = client.search_resources("示例剧")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result_count"], 2)
        self.assertEqual(res["items"][0]["title"], "示例剧 S04")
        self.assertEqual(res["items"][0]["channel"], "addon:demo.telegram")
        self.assertEqual(res["items"][1]["title"], "示例剧 S04 4K")
        self.assertEqual(res["items"][1]["channel"], "custom_pansou")

    def test_browse_share_defaults_cid_zero_and_parses_nested_data_list(self) -> None:
        captured_bodies: List[Dict[str, object]] = []

        def _transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            captured_bodies.append(json.loads((body or b"{}").decode("utf-8")))
            payload = {
                "success": True,
                "data": {
                    "state": True,
                    "data": {
                        "count": 2,
                        "list": [
                            {"n": "Season 4", "cid": "dir_s4", "pc": "pc_dir_s4", "fc": 0},
                            {"n": "Show.S04E01.mkv", "fid": "f_ep1", "cid": "dir_s4", "pc": "pc_ep1", "fc": 1},
                        ],
                    },
                },
            }
            return 200, {}, json.dumps(payload).encode()

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport)
        res_default = client.browse_share("demo_share_code")
        self.assertTrue(res_default["ok"])
        self.assertEqual(res_default["item_count"], 2)
        self.assertEqual(captured_bodies[0]["cid"], "0")

        res_sub = client.browse_share("demo_share_code", cid="dir_s4")
        self.assertTrue(res_sub["ok"])
        self.assertEqual(captured_bodies[1]["cid"], "dir_s4")

    def test_find_child_folder_skips_same_name_files(self) -> None:
        def _transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            if "/api/v1/files/115/browse" in url and "cid=0" in url:
                rows = [
                    {"name": "mp-router", "file_id": "file_bare"},
                    {"n": "mp-router", "fid": "file_fc1", "cid": "0", "pc": "pc_f1", "fc": 1},
                    {"name": "mp-router", "file_id": "file_explicit", "is_dir": False},
                    {"n": "mp-router", "cid": "dir_real_123", "pc": "pc_d1", "fc": 0},
                ]
                return 200, {}, json.dumps({"success": True, "data": {"list": rows}}).encode()
            return 200, {}, b'{"success": true, "data": {}}'

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport)
        res = client.ensure_115_folder("/mp-router")
        self.assertTrue(res["ok"])
        self.assertEqual(res["final_folder_id"], "dir_real_123")
        self.assertEqual(res["steps"][0]["action"], "reused")

    def test_organize_transfer_extracts_scoped_resolved_strm_dirs(self) -> None:
        def _transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            return 200, {}, json.dumps({
                "success": True,
                "data": {
                    "success": 2,
                    "failed": 0,
                    "skipped": 0,
                    "strm_dir": "/strm/anime/DemoShow (1995)/Season 01",
                    "items": [
                        {
                            "old_path": "/mv_staging/e1.mkv",
                            "new_path": "/已整理/anime/DemoShow (1995)/Season 01/e1.mkv",
                            "strm_path": "/strm/anime/DemoShow (1995)/Season 01/e1.strm",
                            "status": "ok",
                        },
                        {
                            "old_path": "/mv_staging/e2.mkv",
                            "new_path": "/已整理/anime/DemoShow (1995)/Season 01/e2.mkv",
                            "strm_dir": "/strm/anime/DemoShow (1995)/Season 01",
                            "status": "ok",
                        },
                        {
                            "old_path": "/mv_staging/bad.mkv",
                            "new_path": "/已整理/anime/DemoShow (1995)/Season 01/bad.mkv",
                            "strm_path": "/other/escape/bad.strm",
                            "strm_dir": "/strm/../escape",
                            "status": "ok",
                        },
                    ],
                },
            }).encode()

        client = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport)
        res = client.organize_transfer(
            files=[
                {"name": "e1.mkv", "source_path": "/mv_staging/e1.mkv"},
                {"name": "e2.mkv", "source_path": "/mv_staging/e2.mkv"},
            ],
            target_dir="/已整理",
            strm_dir="/strm",
            tmdb_id=890,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["resolved_strm_dirs"], ["/strm/anime/DemoShow (1995)/Season 01"])
        self.assertEqual(res["resolved_target_dirs"], ["/已整理/anime/DemoShow (1995)/Season 01"])

    def test_organize_transfer_filters_pure_category_strm_dirs_and_prefers_item_paths(self) -> None:
        # 1) Items have file-level strm_path while top-level strm_dir is multi-level category /strm/series/anime
        def _transport_with_items(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            return 200, {}, json.dumps({
                "success": True,
                "data": {
                    "success": 1,
                    "failed": 0,
                    "skipped": 0,
                    "strm_dir": "/strm/series/anime",
                    "items": [
                        {
                            "old_path": "/mv_staging/e1.mkv",
                            "new_path": "/已整理/series/anime/DemoShow/Season 01/e1.mkv",
                            "strm_path": "/strm/series/anime/DemoShow/Season 01/e1.strm",
                            "status": "ok",
                        }
                    ],
                },
            }).encode()

        client1 = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport_with_items)
        res1 = client1.organize_transfer(
            files=[{"name": "e1.mkv", "source_path": "/mv_staging/e1.mkv"}],
            target_dir="/已整理",
            strm_dir="/strm",
            tmdb_id=890,
        )
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["resolved_strm_dirs"], ["/strm/series/anime/DemoShow/Season 01"])

        # 2) Top-level strm_dir is pure category (/strm/anime) and items lack strm_path -> resolved_strm_dirs stays empty
        def _transport_top_cat_only(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: int):
            return 200, {}, json.dumps({
                "success": True,
                "data": {
                    "success": 1,
                    "failed": 0,
                    "skipped": 0,
                    "strm_dir": "/strm/anime",
                    "items": [
                        {
                            "old_path": "/mv_staging/e1.mkv",
                            "new_path": "/已整理/anime/DemoShow/Season 01/e1.mkv",
                            "status": "ok",
                        }
                    ],
                },
            }).encode()

        client2 = MuvyoClient("http://mv3.example.local:7811", api_key="k", transport=_transport_top_cat_only)
        res2 = client2.organize_transfer(
            files=[{"name": "e1.mkv", "source_path": "/mv_staging/e1.mkv"}],
            target_dir="/已整理",
            strm_dir="/strm",
            tmdb_id=890,
        )
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["resolved_strm_dirs"], [])
        self.assertEqual(res2["resolved_target_dirs"], ["/已整理/anime/DemoShow/Season 01"])


if __name__ == "__main__":
    unittest.main()

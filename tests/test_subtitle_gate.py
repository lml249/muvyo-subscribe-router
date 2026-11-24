from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGINS_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2"
if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from muvyosubscriberouter.subtitle_gate import evaluate_embedded_subtitle_gate


class TestSubtitleGate(unittest.TestCase):
    def test_auto_policy_allows_chinese_audio_without_zh_sub(self) -> None:
        targets = [
            {"name": "Ep01.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/a&pickcode=secret1"},
        ]

        def _runner(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "zho", "title": "国语", "handler_name": ""}],
                "subtitle_streams": [],
            }

        res = evaluate_embedded_subtitle_gate(targets, policy="auto", probe_runner=_runner)
        self.assertTrue(res["ok"])
        self.assertEqual(res["exempt_chinese_audio_count"], 1)
        self.assertNotIn("secret1", str(res))

    def test_auto_policy_blocks_foreign_audio_without_embedded_zh_sub(self) -> None:
        targets = [
            {"name": "Ep01.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/a&pickcode=secret2"},
        ]

        def _runner(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "eng", "title": "English", "handler_name": ""}],
                "subtitle_streams": [{"language": "eng", "title": "English", "handler_name": ""}],
            }

        res = evaluate_embedded_subtitle_gate(targets, policy="auto", probe_runner=_runner)
        self.assertFalse(res["ok"])
        self.assertIn("embedded_chinese_subtitle_missing", res["blockers"])
        self.assertNotIn("secret2", str(res))

    def test_required_policy_blocks_chinese_audio_without_embedded_zh_sub(self) -> None:
        targets = [
            {"name": "Movie.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/m&pickcode=secret3"},
        ]

        def _runner(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "chi", "title": "粤语", "handler_name": ""}],
                "subtitle_streams": [],
            }

        res = evaluate_embedded_subtitle_gate(targets, policy="required", probe_runner=_runner)
        self.assertFalse(res["ok"])
        self.assertIn("embedded_chinese_subtitle_missing", res["blockers"])

    def test_required_policy_passes_when_embedded_zh_sub_present(self) -> None:
        targets = [
            {"name": "Ep01.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/a&pickcode=secret4"},
        ]

        def _runner(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "jpn", "title": "Japanese", "handler_name": ""}],
                "subtitle_streams": [{"language": "chs", "title": "简体中文", "handler_name": ""}],
            }

        res = evaluate_embedded_subtitle_gate(targets, policy="required", probe_runner=_runner)
        self.assertTrue(res["ok"])
        self.assertEqual(res["passed_count"], 1)

    def test_ffprobe_missing_blocks_gate(self) -> None:
        targets = [{"name": "Ep01.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/a&pickcode=s"}]
        with patch("muvyosubscriberouter.subtitle_gate.shutil.which", return_value=None):
            res = evaluate_embedded_subtitle_gate(targets, policy="auto", probe_binary="")
        self.assertFalse(res["ok"])
        self.assertIn("ffprobe_missing", res["blockers"])

    def test_japanese_kanji_title_never_passes_as_chinese_audio_or_subtitle(self) -> None:
        targets = [
            {"name": "Anime.S01E01.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/j&pickcode=sjp"},
        ]

        def _runner_jpn(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "jpn", "title": "日本語", "handler_name": "日本語音声"}],
                "subtitle_streams": [{"language": "jpn", "title": "日本語", "handler_name": ""}],
            }

        res_auto = evaluate_embedded_subtitle_gate(targets, policy="auto", probe_runner=_runner_jpn)
        self.assertFalse(res_auto["ok"])
        self.assertIn("embedded_chinese_subtitle_missing", res_auto["blockers"])

        res_req = evaluate_embedded_subtitle_gate(targets, policy="required", probe_runner=_runner_jpn)
        self.assertFalse(res_req["ok"])
        self.assertIn("embedded_chinese_subtitle_missing", res_req["blockers"])

    def test_bilingual_zh_jp_subtitle_with_explicit_chinese_marker_passes(self) -> None:
        targets = [
            {"name": "Anime.S01E01.mkv", "episode": 1, "probe_url": "http://mv3.example.local/redirect?path=/j&pickcode=sbi"},
        ]

        def _runner_bilingual(tool: str, url: str, timeout: int):
            return {
                "ok": True,
                "audio_streams": [{"language": "jpn", "title": "日本語", "handler_name": ""}],
                "subtitle_streams": [{"language": "und", "title": "简日双语", "handler_name": ""}],
            }

        res = evaluate_embedded_subtitle_gate(targets, policy="required", probe_runner=_runner_bilingual)
        self.assertTrue(res["ok"])
        self.assertEqual(res["passed_count"], 1)


if __name__ == "__main__":
    unittest.main()

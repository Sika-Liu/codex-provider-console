import unittest
from pathlib import Path


class HealthPresentationTests(unittest.TestCase):
    def test_successful_live_request_uses_a_compact_summary_and_limited_detail(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn('detail = f"{endpoint} 返回 HTTP {status}{retry_note}"', source)
        self.assertIn('body[:2000]', source)
        self.assertIn('add("真实请求", "pass" if real_request["ok"] else "warning", detail, diagnostic_detail)', source)

    def test_health_page_groups_checks_and_collapses_diagnostic_output(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn("function healthCategory(name)", source)
        self.assertIn("运行环境','Codex 配置','供应商连接", source)
        self.assertIn("查看诊断详情", source)
        self.assertIn("复制诊断详情", source)
        self.assertIn("health-check ${item.status} ${problem?'':'compact'}", source)


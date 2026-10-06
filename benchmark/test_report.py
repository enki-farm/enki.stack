import unittest
from pathlib import Path

from benchmark.report import make_report


class ReportFormattingTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "metadata": {
                "run_id": "demo-run",
                "target_url": "https://example.test/endpoint",
                "model_config": "default",
                "vus": 20,
                "duration": "1m",
            },
            "summary": {
                "metrics": {
                    "data_sent": {"type": "counter", "values": {"count": 1200}},
                    "data_received": {"type": "counter", "values": {"count": 2400}},
                    "http_reqs": {"type": "counter", "values": {"count": 42, "rate": 1.5}},
                    "http_req_duration": {"type": "trend", "values": {"p(95)": 123.4, "med": 45.1}},
                    "http_req_failed": {"type": "rate", "values": {"rate": 0.0}},
                }
            },
        }

    def test_hides_target_url_and_uses_raw_report_at_bottom(self):
        html = make_report(self.data, Path("demo.json"))
        self.assertNotIn("target_url", html)
        self.assertNotIn("https://example.test/endpoint", html)
        self.assertIn("Raw report", html)
        self.assertLess(html.rfind("Raw report"), html.rfind("</html>"))
        self.assertRegex(html, r"requests|Requests")
        self.assertRegex(html, r"data sent|Data sent")
        self.assertRegex(html, r"data received|Data received")


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path


class PanelToastTests(unittest.TestCase):
    def test_operation_notices_use_a_top_floating_three_second_toast(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn('id="panel-toast" role="status" aria-live="polite"', source)
        self.assertIn("position:fixed;z-index:1401;top:16px", source)
        self.assertIn("transform:translate(-50%,0)", source)
        self.assertIn("function note(text,_where)", source)
        self.assertIn("},3000)", source)
        self.assertIn("toast.classList.add('leaving')", source)


if __name__ == "__main__":
    unittest.main()

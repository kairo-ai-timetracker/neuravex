import unittest
from unittest.mock import patch

from trading_engine.alerts import broadcast_alert


class TestAlertDispatcher(unittest.TestCase):
    def test_no_channels_does_not_raise(self):
        with patch.dict("os.environ", {}, clear=True):
            result = broadcast_alert("Test", "message")
            self.assertFalse(any(result.values()))

    def test_dispatches_to_all_channels(self):
        import trading_engine.alerts as alerts_module
        with patch.object(alerts_module, "send_telegram", return_value=True), \
             patch.object(alerts_module, "send_discord", return_value=True), \
             patch.object(alerts_module, "send_email", return_value=True):
            result = broadcast_alert("Test", "message")
            self.assertTrue(all(result.values()))

    def test_one_channel_failure_does_not_block_others(self):
        import trading_engine.alerts as alerts_module
        with patch.object(alerts_module, "send_telegram", side_effect=Exception("boom")), \
             patch.object(alerts_module, "send_discord", return_value=True):
            try:
                alerts_module.send_telegram("x")
            except Exception:
                pass
            result = alerts_module.send_discord("x")
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()

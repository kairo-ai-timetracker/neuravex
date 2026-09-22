import json
import unittest

import numpy as np

from backend.app.db.sql_data_store import _json_safe


class TestJsonSafe(unittest.TestCase):
    def test_converts_numpy_bool(self):
        result = _json_safe({"crossed_down": np.bool_(False)})
        self.assertIs(result["crossed_down"], False)
        self.assertIsInstance(result["crossed_down"], bool)
        self.assertNotIsInstance(result["crossed_down"], np.bool_)

    def test_converts_numpy_float_and_int(self):
        result = _json_safe({"adx": np.float64(48.13), "count": np.int64(7)})
        self.assertIsInstance(result["adx"], float)
        self.assertIsInstance(result["count"], int)

    def test_converts_bare_numpy_scalar_not_in_a_dict(self):
        # This is exactly the case that slipped through before: a plain
        # numeric column value (AIDecision.final_score), not something
        # nested inside a JSON dict.
        result = _json_safe(np.float64(0.896857231856005))
        self.assertIsInstance(result, float)
        self.assertNotIsInstance(result, np.floating)

    def test_recurses_into_nested_dicts_and_lists(self):
        result = _json_safe({"outer": {"inner": np.bool_(True)}, "items": [np.float64(1.5), np.bool_(False)]})
        self.assertIsInstance(result["outer"]["inner"], bool)
        self.assertIsInstance(result["items"][0], float)
        self.assertIsInstance(result["items"][1], bool)

    def test_leaves_native_python_types_untouched(self):
        original = {"a": 1, "b": 2.5, "c": True, "d": "text", "e": None}
        result = _json_safe(original)
        self.assertEqual(result, original)

    def test_actually_json_serializable_after_sanitizing(self):
        raw = {"adx": np.float64(48.13), "crossed_down": np.bool_(False), "nested": {"x": np.int64(3)}}
        json.dumps(_json_safe(raw))  # must not raise


if __name__ == "__main__":
    unittest.main()

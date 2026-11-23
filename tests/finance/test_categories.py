from __future__ import annotations

import unittest

from jarvis.finance.categories import (
    CATEGORY_BY_KEY,
    CATEGORY_DEFS,
    bucket,
    category_select_options,
    label,
    normalize_category,
)


class CategoriesTests(unittest.TestCase):
    def test_category_defs_exist(self):
        self.assertGreater(len(CATEGORY_DEFS), 0)
        self.assertIn("food_basic", CATEGORY_BY_KEY)

    def test_normalize_category(self):
        self.assertEqual(normalize_category("food"), "food_basic")
        self.assertEqual(normalize_category("UNKNOWN_xyz"), "uncategorized")
        self.assertEqual(normalize_category(None), "uncategorized")

    def test_label_and_bucket(self):
        self.assertIn("Продукты", label("food"))
        self.assertEqual(bucket("food"), "Еда")

    def test_category_select_options(self):
        html = category_select_options("food_basic")
        self.assertIn("<optgroup label='Еда'>", html)
        self.assertIn("selected", html)


if __name__ == "__main__":
    unittest.main()

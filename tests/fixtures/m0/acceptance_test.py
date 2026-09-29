"""Fixed synthetic acceptance tests. Execute only in GitHub Actions."""
import unittest

from arithmetic import total_points


class TotalPointsAcceptance(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(total_points([]), 0)

    def test_single_item(self):
        self.assertEqual(total_points([7]), 7)

    def test_last_item_is_included(self):
        self.assertEqual(total_points([2, 3]), 5)

    def test_negative_score(self):
        self.assertEqual(total_points([9, -4, 2]), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)

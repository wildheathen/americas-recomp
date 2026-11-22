"""Unit tests for the candidate logic of rage9_score_blas (synthetic data only)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rage9_score_blas as sb


class TestEmbeddings(unittest.TestCase):
    def test_ordered_subsequences(self):
        self.assertEqual(sb.embeddings([5, 7], [5, 7, 5, 7], 64), [[0, 1], [0, 3], [2, 3]])
        self.assertEqual(sb.embeddings([152, 84], [44, 84, 152], 64), [])

    def test_unordered_allows_any_order_without_reuse(self):
        self.assertEqual(sb.embeddings([152, 84], [44, 84, 152], 64, ordered=False), [[2, 1]])
        self.assertEqual(sb.embeddings([3, 3], [3], 64, ordered=False), [])

    def test_cap(self):
        self.assertEqual(len(sb.embeddings([1], [1] * 10, 4)), 4)


class TestShaderRule(unittest.TestCase):
    def test_listed_shaders_are_left_out(self):
        skip = sb.parse_shader_hashes("0xAAAA0001, bbbb0002")
        self.assertEqual(skip, frozenset({0xAAAA0001, 0xBBBB0002}))
        model = [{"shader_hash": h} for h in (0x11111111, 0xAAAA0001, 0x22222222, 0xBBBB0002, None)]
        self.assertEqual(sb.rule_used(model, skip), [0, 2, 4])

    def test_no_hashes_means_no_rule(self):
        self.assertEqual(sb.parse_shader_hashes(""), frozenset())
        self.assertEqual(sb.parse_shader_hashes(None), frozenset())


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
import unittest

from tag_outside_labels import TAG, tag_line


class TagLine(unittest.TestCase):
    def test_adds_to_existing_tags(self):
        line = "n1 v2 dV c3 t2020-01-01T00:00:00Z i4 uName Tname=Riga,place=city x24.1 y56.9"
        self.assertEqual(tag_line(line), f"n1 v2 dV c3 t2020-01-01T00:00:00Z i4 uName Tname=Riga,place=city,{TAG} x24.1 y56.9")

    def test_empty_tags(self):
        self.assertEqual(tag_line("n1 v1 T x1 y2"), f"n1 v1 T{TAG} x1 y2")

    def test_user_named_with_a_capital_t_is_left_alone(self):
        line = "n1 uTom Tname=A x1 y2"
        self.assertEqual(tag_line(line), f"n1 uTom Tname=A,{TAG} x1 y2")

    def test_no_tags_field_fails(self):
        with self.assertRaises(ValueError):
            tag_line("n1 v1 x1 y2")


if __name__ == "__main__":
    unittest.main()

"""Tests: Selection-range parsing and filters should reject invalid input rather than partially changing the queue."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_convenience import (parse_section_ranges, job_category, estimate_audio_duration,
                                     part_export_basename, readable_m4b_basename, part_number_from_name)

class ConvenienceTests(unittest.TestCase):
    """Regression cases for convenience behavior."""
    def test_selection_ranges_spine_order_dedup(self):
        """Verify selection ranges spine order dedup behavior."""
        self.assertEqual(parse_section_ranges('8-10, 9, 2', [1,2,3,8,9,10]), [2,8,9,10])

    def test_invalid_and_missing_sections_do_not_partially_apply(self):
        """Verify invalid and missing sections do not partially apply behavior."""
        for expression in ('4-2', '1,,2', '1-900000', '5-6', '4-x', ''):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    parse_section_ranges(expression, [1,2,3,4])

    def test_queue_filter_and_estimated_duration(self):
        """Verify queue filter and estimated duration behavior."""
        self.assertEqual(job_category('interrupted'), 'Needs attention')
        self.assertEqual(job_category('queued'), 'Pending / active')
        self.assertEqual(job_category('complete'), 'Completed')
        self.assertEqual(estimate_audio_duration(130), 60)

    def test_m4b_filenames_are_book_title_and_part_only(self):
        self.assertEqual(part_export_basename('A New Player in the Game', 9, range(123, 138)),
                         'A New Player in the Game - Part(9)')
        self.assertEqual(part_export_basename('A New Player in the Game', 1, [5]),
                         'A New Player in the Game - Part(1)')
        self.assertEqual(part_export_basename('Novel', 2, [1, 4, 8]), 'Novel - Part(2)')
        for name, part in (
            ('Novel - Part(12)', 12),
            ('Novel - Part(12) (2).m4b', 12),
            ('Novel - Part 09 (Sections 123-137)', 9),
            ('A_New_Player_in_the_Game_part_01_sections_123-137_32d13cf858be.m4b', 1),
        ):
            self.assertEqual(part_number_from_name(name), part)

    def test_duplicate_export_uses_readable_copy_number(self):
        original = part_export_basename('Novel', 1, [1, 2])
        self.assertEqual(part_export_basename('Novel', 1, [1, 2], {original}),
                         'Novel - Part(1) (2)')
        self.assertEqual(part_export_basename('Novel', 1, [1, 2],
                         {original, original + ' (2)'}),
                         'Novel - Part(1) (3)')
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / (original + '.m4b')).write_bytes(b'existing recording')
            self.assertEqual(part_export_basename('Novel', 1, [1, 2], folder=folder),
                             original + ' (2)')
            (folder / (original + ' (2).partial.m4b')).write_bytes(b'in-progress export')
            self.assertEqual(part_export_basename('Novel', 1, [1, 2], folder=folder),
                             original + ' (3)')
            self.assertEqual((folder / (original + '.m4b')).read_bytes(), b'existing recording')

    def test_unsafe_characters_length_and_legacy_names(self):
        name = part_export_basename('A/B: "Title" <with> ? characters*', 1, range(10, 20))
        self.assertEqual(name, 'A B Title with characters - Part(1)')
        long_name = part_export_basename('This is an exceptionally long book title ' * 8,
                                         100, range(1000, 1016))
        self.assertLessEqual(len(long_name), 130)
        self.assertTrue(long_name.endswith(' - Part(100)'))
        legacy = 'A_New_Player_in_the_Game_part_01_sections_123-137_32d13cf858be'
        self.assertEqual(readable_m4b_basename(legacy), legacy)
        with self.assertRaises(ValueError):
            part_export_basename('Novel', 1, [])

if __name__ == '__main__':
    unittest.main()

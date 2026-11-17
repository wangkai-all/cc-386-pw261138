"""Conflict triage grading (``difference_profile``).

The grading is an operator aid: it must escalate genuinely different raw
values, but it must not flag harmless spelling variants (zero-width characters,
width/case, whitespace, accents, punctuation) as substantive.
"""
from __future__ import annotations

import unittest

from app.link_service import difference_profile


class DifferenceProfileTest(unittest.TestCase):
    def test_single_raw_value_is_none(self):
        self.assertEqual(difference_profile(["ACME GmbH"], "acme gmbh")["severity"], "none")

    def test_empty_key_is_its_own_severity(self):
        profile = difference_profile([" ", "　", "​"], "")
        self.assertEqual(profile["severity"], "empty_key")
        self.assertEqual(profile["types"], ["blank"])

    def test_invisible_characters_are_variant(self):
        profile = difference_profile(["北京华为技术有限公司​", "北京华为技术有限公司"],
                                     "北京华为技术有限公司")
        self.assertEqual(profile["severity"], "variant")
        self.assertIn("invisible", profile["types"])

    def test_fullwidth_case_and_whitespace_are_variant(self):
        profile = difference_profile(["ＡＣＭＥ　ＧｍｂＨ", "ACME  GmbH", "ACME GmbH"], "acme gmbh")
        self.assertEqual(profile["severity"], "variant")
        self.assertIn("case_or_width", profile["types"])
        self.assertIn("whitespace", profile["types"])

    def test_diacritics_and_punctuation_are_variant(self):
        self.assertEqual(
            difference_profile(["Müller GmbH", "Muller GmbH"], "muller gmbh")["severity"], "variant")
        self.assertEqual(
            difference_profile(["ACME, Inc.", "ACME Inc"], "acme inc")["severity"], "variant")

    def test_ss_and_double_s_are_variant(self):
        # casefold turns ß into ss; this is a spelling variant, not a different entity.
        profile = difference_profile(["Straße​ GmbH", "STRASSE  GMBH"], "strasse gmbh")
        self.assertEqual(profile["severity"], "variant")

    def test_genuinely_different_names_are_substantive(self):
        profile = difference_profile(["ACME GmbH", "ACME Limited"], "acme gmbh")
        self.assertEqual(profile["severity"], "substantive")
        self.assertEqual(profile["worst_member"], "ACME Limited")
        self.assertIn("characters", profile["types"])
        self.assertEqual(profile["pairs"][-1]["explained_by"], "characters")

    def test_worst_member_points_at_the_misleading_spelling(self):
        profile = difference_profile(
            ["ACME GmbH", "ACME  GmbH", "Beta Corp"], "acme gmbh")
        self.assertEqual(profile["severity"], "substantive")
        self.assertEqual(profile["worst_member"], "Beta Corp")

    def test_worst_member_is_none_for_variants(self):
        profile = difference_profile(["ACME GmbH", "ＡＣＭＥ ＧｍｂＨ"], "acme gmbh")
        self.assertEqual(profile["severity"], "variant")
        self.assertIsNone(profile["worst_member"])


if __name__ == "__main__":
    unittest.main()

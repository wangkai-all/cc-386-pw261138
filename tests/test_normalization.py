"""Unit tests for the normalization engine (no network)."""
from __future__ import annotations

import unittest

from app import normalization as N


STEPS = N.validate_steps([
    {"op": "unicode_nfkc"},
    {"op": "strip_format"},
    {"op": "trim"},
    {"op": "collapse_ws"},
    {"op": "casefold"},
])


class CodePointTests(unittest.TestCase):
    def test_fullwidth_and_ideographic_space(self):
        result = N.normalize("ＡＣＭＥ　ＧｍｂＨ", STEPS)
        self.assertEqual(result["key"], "acme gmbh")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["raw"], "ＡＣＭＥ　ＧｍｂＨ")
        self.assertIn("U+FF21", {c["codepoint"] for c in result["raw_codepoints"]})
        self.assertIn("U+3000", {c["codepoint"] for c in result["raw_codepoints"]})

    def test_zero_width_removed_and_evidenced(self):
        result = N.normalize("ACME​ GmbH", STEPS)
        self.assertEqual(result["key"], "acme gmbh")
        step = result["steps"][1]
        self.assertTrue(step["changed"])
        self.assertEqual(step["op"], "strip_format")
        self.assertEqual(step["changes"][0]["before_codepoints"][0]["codepoint"], "U+200B")

    def test_evidence_keeps_escaped_original(self):
        result = N.normalize("  a\tb ", STEPS)
        self.assertEqual(result["key"], "a b")
        self.assertEqual(result["raw_escaped"], "  a\\tb ")
        self.assertTrue(all("input_escaped" in s for s in result["steps"]))

    def test_empty_key_flagged(self):
        result = N.normalize("   ", STEPS)
        self.assertEqual(result["key"], "")
        self.assertTrue(result["empty"])
        self.assertEqual(result["status"], "empty_key")

    def test_deterministic_for_same_input(self):
        a = N.normalize("Müller GmbH", STEPS)
        b = N.normalize("Müller GmbH", STEPS)
        self.assertEqual(a, b)

    def test_long_input_not_truncated_in_key(self):
        raw = "x" * 1000
        result = N.normalize(raw, STEPS)
        self.assertEqual(len(result["key"]), 1000)
        self.assertTrue(result["raw_truncated"] is False)


class RuleValidationTests(unittest.TestCase):
    def test_rejects_unknown_op(self):
        with self.assertRaises(N.RuleError):
            N.validate_steps([{"op": "explode"}])

    def test_rejects_empty_steps(self):
        with self.assertRaises(N.RuleError):
            N.validate_steps([])

    def test_rejects_bad_regex(self):
        with self.assertRaises(N.RuleError):
            N.validate_steps([{"op": "regex_replace", "pattern": "(unclosed"}])

    def test_rejects_extra_params(self):
        with self.assertRaises(N.RuleError):
            N.validate_steps([{"op": "trim", "aggressive": True}])

    def test_hash_stable_and_order_sensitive(self):
        a = N.validate_steps([{"op": "trim"}, {"op": "lower"}])
        b = N.validate_steps([{"op": "trim"}, {"op": "lower"}])
        c = N.validate_steps([{"op": "lower"}, {"op": "trim"}])
        self.assertEqual(N.steps_hash(a), N.steps_hash(b))
        self.assertNotEqual(N.steps_hash(a), N.steps_hash(c))

    def test_hash_ignores_notes(self):
        a = N.validate_steps([{"op": "trim", "note": "one"}])
        b = N.validate_steps([{"op": "trim", "note": "two"}])
        self.assertEqual(N.steps_hash(a), N.steps_hash(b))

    def test_replace_map_only_matches_whole_value(self):
        steps = N.validate_steps([{"op": "replace_map", "map": {"ACME GmbH": "ACME"}}])
        self.assertEqual(N.normalize("ACME GmbH", steps)["key"], "ACME")
        self.assertEqual(N.normalize("ACME GmbH & Co", steps)["key"], "ACME GmbH & Co")

    def test_regex_flags_and_replacement(self):
        steps = N.validate_steps([
            {"op": "regex_replace", "pattern": r"\s*GmbH\s*$", "replacement": "", "flags": "i"}
        ])
        self.assertEqual(N.normalize("Acme  gmbh", steps)["key"], "Acme")

    def test_smoke_test_catches_all_empty(self):
        steps = N.validate_steps([{"op": "regex_replace", "pattern": ".*", "replacement": ""}])
        with self.assertRaises(N.RuleError):
            N.smoke_test(steps)

    def test_strip_diacritics_casefold_order(self):
        steps = N.validate_steps([{"op": "unicode_nfkd"}, {"op": "strip_diacritics"}, {"op": "casefold"}])
        self.assertEqual(N.normalize("MÜLLER", steps)["key"], "muller")
        self.assertEqual(N.normalize("Müller", steps)["key"], "muller")
        # dotless i does not lose its identity under plain NFKD
        self.assertEqual(N.normalize("Diyarbakır", steps)["key"], "diyarbakır")


if __name__ == "__main__":
    unittest.main()

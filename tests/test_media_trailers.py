#!/usr/bin/env python3
"""Offline tests for v1.26.0 media-message trailers (no network).

Run: python3 tests/test_media_trailers.py  (or python3 -m unittest discover tests)

Covers: trailer parsing for every media kind, validation drops (bad
coords, bad quiz index, short polls, malformed venues), bare vs
colon-form trailers, the 5-action cap, strip_trailer_syntax removal,
demo image generation, and fire_actions failure paths (unknown kinds,
[react:] with no message id). No network calls, no state files touched.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ron_media_trailers as mt
import ron_daemon


class TestParseDice(unittest.TestCase):
    def test_bare(self):
        self.assertEqual(mt.parse_media_trailers("Roll! [dice]"),
                         [{"kind": "dice", "emoji": "🎲"}])

    def test_emoji(self):
        self.assertEqual(mt.parse_media_trailers("Pick [dice: 🎯] one"),
                         [{"kind": "dice", "emoji": "🎯"}])

    def test_bad_emoji_falls_back(self):
        self.assertEqual(mt.parse_media_trailers("[dice: 🚀]")[0]["emoji"],
                         "🎲")

    def test_no_double_fire(self):
        # [dice: 🎯] must not also trigger the bare-[dice] branch
        self.assertEqual(len(mt.parse_media_trailers("x [dice: 🎯] y")), 1)


class TestParsePollQuiz(unittest.TestCase):
    def test_poll(self):
        a = mt.parse_media_trailers("[poll: Best? | Python | Go]")[0]
        self.assertEqual(a["kind"], "poll")
        self.assertEqual(a["question"], "Best?")
        self.assertEqual(a["options"], ["Python", "Go"])

    def test_pollm(self):
        a = mt.parse_media_trailers("[pollm: Top? | A | B | C]")[0]
        self.assertEqual(a["kind"], "pollm")
        self.assertEqual(len(a["options"]), 3)

    def test_poll_too_few_options_dropped(self):
        self.assertEqual(mt.parse_media_trailers("[poll: Q? | Only]"), [])

    def test_poll_caps_at_ten(self):
        body = "[poll: Q? | " + " | ".join(f"o{i}" for i in range(12)) + "]"
        self.assertEqual(len(mt.parse_media_trailers(body)[0]["options"]),
                         10)

    def test_quiz_with_explanation(self):
        a = mt.parse_media_trailers(
            "[quiz: 2+2? | 3 | 4 | 5 | 2 | Because math]")[0]
        self.assertEqual(a["kind"], "quiz")
        self.assertEqual(a["correct_option_id"], 1)  # 1-based -> 0-based
        self.assertEqual(a["explanation"], "Because math")

    def test_quiz_without_explanation(self):
        a = mt.parse_media_trailers("[quiz: Cap? | Paris | London | 1]")[0]
        self.assertEqual(a["correct_option_id"], 0)
        self.assertEqual(a["explanation"], "")

    def test_quiz_bad_index_dropped(self):
        self.assertEqual(mt.parse_media_trailers("[quiz: Q? | A | B | 9]"),
                         [])
        self.assertEqual(mt.parse_media_trailers("[quiz: Q? | A | B]"), [])

    def test_quiz_missing_index_dropped(self):
        self.assertEqual(
            mt.parse_media_trailers("[quiz: Q? | A | B | nope]"), [])


class TestParseGeo(unittest.TestCase):
    def test_location(self):
        a = mt.parse_media_trailers("Here [location: 12.9716, 77.5946]")[0]
        self.assertEqual((a["kind"], a["lat"], a["lon"]),
                         ("location", 12.9716, 77.5946))

    def test_location_bad_coords_dropped(self):
        self.assertEqual(mt.parse_media_trailers("[location: 999, 0]"), [])
        self.assertEqual(mt.parse_media_trailers("[location: abc]"), [])

    def test_livelocation(self):
        a = mt.parse_media_trailers("[livelocation: 28.61, 77.20]")[0]
        self.assertEqual(a["kind"], "livelocation")

    def test_venue(self):
        a = mt.parse_media_trailers(
            "[venue: 12.97, 77.59 | Cubbon Park | Bengaluru]")[0]
        self.assertEqual((a["title"], a["address"]),
                         ("Cubbon Park", "Bengaluru"))

    def test_venue_missing_parts_dropped(self):
        self.assertEqual(mt.parse_media_trailers("[venue: 12.97, 77.59]"),
                         [])
        self.assertEqual(
            mt.parse_media_trailers("[venue: 1, 2 | Title]"), [])


class TestParseMisc(unittest.TestCase):
    def test_contact_always_fake(self):
        a = mt.parse_media_trailers("Call [contact: Support Desk]")[0]
        self.assertEqual(a["kind"], "contact")
        self.assertEqual(a["phone"], "+15551234567")
        self.assertIn("demo", a["last_name"].lower())

    def test_spoilerphoto(self):
        a = mt.parse_media_trailers("Peek [spoilerphoto: my desk]")[0]
        self.assertEqual((a["kind"], a["caption"]),
                         ("spoilerphoto", "my desk"))

    def test_bare_spoilerphoto(self):
        a = mt.parse_media_trailers("Peek [spoilerphoto]")[0]
        self.assertEqual(a["kind"], "spoilerphoto")

    def test_mediagroup(self):
        a = mt.parse_media_trailers("Pics [mediagroup: trip]")[0]
        self.assertEqual((a["kind"], a["caption"]), ("mediagroup", "trip"))

    def test_react(self):
        a = mt.parse_media_trailers("Nice [react: 🔥]")[0]
        self.assertEqual((a["kind"], a["emoji"]), ("react", "🔥"))

    def test_react_bad_emoji_falls_back(self):
        a = mt.parse_media_trailers("[react: xyz]")[0]
        self.assertEqual(a["emoji"], "👍")

    def test_action_cap(self):
        reply = " ".join("[dice: 🎯]" for _ in range(8))
        self.assertEqual(len(mt.parse_media_trailers(reply)), 5)

    def test_bare_dice_fires_once(self):
        self.assertEqual(len(mt.parse_media_trailers("[dice] [dice]")), 1)

    def test_multiple_kinds_in_order(self):
        kinds = [a["kind"] for a in mt.parse_media_trailers(
            "Hi [dice] there [poll: Q? | A | B] ok [react: 👍]")]
        self.assertEqual(kinds, ["dice", "poll", "react"])


class TestStrip(unittest.TestCase):
    def test_media_trailers_stripped(self):
        clean = ron_daemon.strip_trailer_syntax(
            "Roll! [dice] and vote [poll: Q? | A | B] plus [quiz: Q? | A | B | 1] "
            "pin [location: 1, 2] live [livelocation: 1, 2] "
            "venue [venue: 1, 2 | T | A] call [contact: X] "
            "peek [spoilerphoto: c] pics [mediagroup: c] [react: 👍]")
        for token in ("[dice]", "[poll:", "[quiz:", "[location:",
                      "[livelocation:", "[venue:", "[contact:",
                      "[spoilerphoto:", "[mediagroup:", "[react:"):
            self.assertNotIn(token, clean)
        self.assertIn("Roll!", clean)

    def test_button_trailers_still_stripped(self):
        clean = ron_daemon.strip_trailer_syntax(
            "Hi [buttons: A | B] [copy: L | text]")
        self.assertNotIn("[buttons:", clean)
        self.assertNotIn("[copy:", clean)


class TestImageGen(unittest.TestCase):
    def test_valid_jpeg(self):
        data = mt.demo_image_bytes(0)
        self.assertTrue(data.startswith(b"\xff\xd8\xff"))
        self.assertGreater(len(data), 1000)

    def test_variants_differ(self):
        self.assertNotEqual(mt.demo_image_bytes(1), mt.demo_image_bytes(2))


class TestFireActions(unittest.TestCase):
    def test_unknown_kind_false(self):
        self.assertFalse(mt._fire_one({"kind": "nope"}, 1, None, None, None))

    def test_react_without_msg_id_dropped(self):
        self.assertFalse(mt._fire_one({"kind": "react", "emoji": "👍"},
                                      1, None, None, None))

    def test_fire_actions_never_raises(self):
        n = mt.fire_actions([{"kind": "nope"}], 1, sent_msg_id=None)
        self.assertEqual(n, 0)

    def test_fire_actions_empty(self):
        self.assertEqual(mt.fire_actions([], 1), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

import unittest

from creator.render import plan_shots
from creator.timing import align


class MontageTests(unittest.TestCase):
    def test_cut_moves_to_nearby_beat(self):
        result = plan_shots(2, 120, [{"start": 0}, {"start": 1.8}], [0, 1.67, 2.5])
        self.assertEqual(result[0]["end"], 50 / 30)
        self.assertEqual(result[1], {"start": 50 / 30, "end": 4.0})

    def test_dry_narration_cuts_at_phrase(self):
        result = plan_shots(2, 120, [{"start": 0}, {"start": 1.8}], [])
        self.assertEqual(result[0]["end"], 1.8)

    def test_rejects_different_transcript(self):
        with self.assertRaisesRegex(ValueError, "divergente"):
            align([{"text": "Une orange roule"}], {"transcription": [
                {"text": "Bonjour", "offsets": {"from": 0, "to": 900}}]}, 1.0)

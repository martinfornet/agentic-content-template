import tempfile
import unittest
from pathlib import Path

from creator.workflow import CreatorWorkflow


class CreatorWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workflow = CreatorWorkflow(self.root / "state")
        self.footage = self.root / "footage.mp4"
        self.audio = self.root / "voice.wav"
        self.video = self.root / "assembled.mp4"
        self.footage.write_bytes(b"fake-footage")
        self.audio.write_bytes(b"fake-audio")
        self.video.write_bytes(b"fake-video")
        self.project = self.workflow.create_proposal(
            idea="Model-generated idea", shooting_brief="Film a demonstration",
            script="Narration supplied by the model", trend_notes="Optional trend only")
        self.project_id = self.project["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_flow_records_separate_approvals_and_hashes(self):
        with self.assertRaisesRegex(ValueError, "Étape requise"):
            self.workflow.record_voice(self.project_id, self.audio)
        self.workflow.approve_footage(self.project_id, [self.footage], actor="creator")
        self.workflow.record_voice(self.project_id, self.audio)
        voice_approved = self.workflow.approve_voice(self.project_id, actor="creator")
        self.assertEqual(voice_approved["stage"], "voice_approved")
        self.assertEqual(voice_approved["voice"]["approved_by"], "creator")
        self.workflow.record_assembly(self.project_id, self.video)
        ready = self.workflow.approve_export(self.project_id, actor="creator")
        self.assertEqual(ready["stage"], "export_approved")
        self.assertEqual(ready["assembly"]["voice_sha256"], ready["voice"]["sha256"])

    def test_changed_audio_cannot_reuse_approval(self):
        self.workflow.approve_footage(self.project_id, [self.footage], actor="creator")
        self.workflow.record_voice(self.project_id, self.audio)
        self.audio.write_bytes(b"regenerated-audio")
        with self.assertRaisesRegex(ValueError, "a changé"):
            self.workflow.approve_voice(self.project_id, actor="creator")

    def test_missing_footage_is_rejected_without_advancing(self):
        with self.assertRaisesRegex(ValueError, "introuvable"):
            self.workflow.approve_footage(self.project_id, [self.root / "missing.mp4"], actor="creator")
        self.assertEqual(self.workflow.read(self.project_id)["stage"], "proposed")

    def test_trend_notes_are_optional_and_separate(self):
        self.assertEqual(self.project["proposal"]["trend_notes"], "Optional trend only")
        self.assertEqual(self.project["proposal"]["script"], "Narration supplied by the model")


if __name__ == "__main__":
    unittest.main()

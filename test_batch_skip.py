import os
import tempfile
import unittest
from unittest.mock import patch


class BatchSkipTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{self.temp_dir.name}/test.db"

        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": database_url,
                "UPLOAD_FOLDER": self.temp_dir.name,
            },
        ):
            from app import create_app

            self.app = create_app()

        from app.extensions import db
        from app.models import SoundClip, SoundList, Team

        self.db = db
        self.context = self.app.app_context()
        self.context.push()

        sound_list = SoundList(name="Test list")
        team = Team(name="Test team")
        db.session.add_all([sound_list, team])
        db.session.flush()

        skipped_clip = SoundClip(
            list_id=sound_list.id,
            title="Skipped",
            filename="skipped.mp3",
            original_name="skipped.mp3",
        )
        correct_clip = SoundClip(
            list_id=sound_list.id,
            title="Correct",
            filename="correct.mp3",
            original_name="correct.mp3",
        )
        omitted_clip = SoundClip(
            list_id=sound_list.id,
            title="Omitted",
            filename="omitted.mp3",
            original_name="omitted.mp3",
        )
        db.session.add_all([skipped_clip, correct_clip, omitted_clip])
        db.session.commit()

        self.list_id = sound_list.id
        self.team_id = team.id
        self.skipped_clip_id = skipped_clip.id
        self.correct_clip_id = correct_clip.id
        self.omitted_clip_id = omitted_clip.id

    def tearDown(self):
        self.db.session.remove()
        self.db.drop_all()
        self.context.pop()
        self.temp_dir.cleanup()

    def test_skip_is_incorrect_and_missing_result_is_untouched(self):
        from app.models import Answer

        response = self.app.test_client().post(
            "/answers/batch",
            data={
                "team_id": self.team_id,
                "list_id": self.list_id,
                f"clip_{self.skipped_clip_id}_result": "skip",
                f"clip_{self.correct_clip_id}_result": "correct",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Answer.query.filter_by(clip_id=self.skipped_clip_id).one().is_correct
        )
        self.assertTrue(
            Answer.query.filter_by(clip_id=self.correct_clip_id).one().is_correct
        )
        self.assertIsNone(
            Answer.query.filter_by(clip_id=self.omitted_clip_id).one_or_none()
        )

    def test_batch_form_has_cycle_and_skip_confirmation(self):
        response = self.app.test_client().get(f"/lists/{self.list_id}")
        content = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="next-cycle-button"', content)
        self.assertIn("function startNextCycle()", content)
        self.assertIn("still skipped. Mark", content)


if __name__ == "__main__":
    unittest.main()

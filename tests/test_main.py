import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import MediaInfo, check_compatibility, creation_time, discover_jobs


class DiscoverJobsTests(unittest.TestCase):
    def test_discovers_directories_recursively_and_excludes_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            first = root / "first.mp4"
            second = nested / "second.MP4"
            output = nested / "video.mp4"
            ignored = nested / "notes.txt"
            for path in (first, second, output, ignored):
                path.touch()

            jobs = discover_jobs(root)

            self.assertEqual([job.directory for job in jobs], [root, nested])
            self.assertEqual(jobs[0].files, [first])
            self.assertEqual(jobs[1].files, [second])
            self.assertEqual(jobs[1].output, output)

    def test_sorts_files_by_creation_time_then_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            newest = root / "newest.mp4"
            same_time_b = root / "b.mp4"
            same_time_a = root / "a.mp4"
            for path in (newest, same_time_b, same_time_a):
                path.touch()

            timestamps = {
                newest: 20.0,
                same_time_b: 10.0,
                same_time_a: 10.0,
            }
            with patch("main.creation_time", side_effect=timestamps.__getitem__):
                jobs = discover_jobs(root)

            self.assertEqual(jobs[0].files, [same_time_a, same_time_b, newest])

    def test_creation_time_returns_a_number(self) -> None:
        with tempfile.NamedTemporaryFile() as file:
            self.assertIsInstance(creation_time(Path(file.name)), float)


class CompatibilityTests(unittest.TestCase):
    def test_average_fps_difference_does_not_block_merge(self) -> None:
        first = MediaInfo(
            path=Path("1.mp4"),
            video_codec="h264",
            width=1920,
            height=1080,
            fps=57.455,
            audio_codec="aac",
            sample_rate=48000,
            channels=2,
            duration=60.0,
            size=1,
        )
        second = MediaInfo(
            path=Path("2.mp4"),
            video_codec="h264",
            width=1920,
            height=1080,
            fps=60.0,
            audio_codec="aac",
            sample_rate=48000,
            channels=2,
            duration=60.0,
            size=1,
        )

        self.assertEqual(check_compatibility([first, second]), [])


if __name__ == "__main__":
    unittest.main()

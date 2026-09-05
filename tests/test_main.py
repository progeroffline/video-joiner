import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from main import (
    MediaInfo,
    build_ffmpeg_command,
    check_compatibility,
    creation_time,
    delete_sources,
    discover_jobs,
    requeue_invalid_outputs,
    run_ffmpeg_concat,
    select_jobs,
)


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


class FfmpegCommandTests(unittest.TestCase):
    def test_maps_video_and_optional_audio_but_not_data_streams(self) -> None:
        command = build_ffmpeg_command(Path("files.txt"), Path("video.mp4"))
        mapped_streams = [
            command[index + 1]
            for index, argument in enumerate(command)
            if argument == "-map"
        ]

        self.assertEqual(mapped_streams, ["0:v", "0:a?"])

    def test_audio_recovery_copies_video_and_transcodes_audio(self) -> None:
        command = build_ffmpeg_command(
            Path("files.txt"), Path("video.mp4"), transcode_audio=True
        )

        self.assertIn("+discardcorrupt+genpts", command)
        self.assertIn("ignore_err", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_retries_with_audio_transcoding_after_aac_error(self) -> None:
        attempts = [
            (1, "ADTS error", ["Non-monotonic DTS"], True),
            (0, "recovered", [], False),
        ]
        with patch("main.execute_ffmpeg", side_effect=attempts) as execute:
            warnings, audio_transcoded = run_ffmpeg_concat(
                Path("files.txt"), Path("video.mp4"), 60.0
            )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(warnings, ["Non-monotonic DTS"])
        self.assertTrue(audio_transcoded)


class SelectJobsTests(unittest.TestCase):
    def test_skips_directory_with_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "part.mp4"
            output = root / "video.mp4"
            source.touch()
            output.touch()
            jobs = discover_jobs(root)

            pending, skipped = select_jobs(jobs, force=False)

            self.assertEqual(pending, [])
            self.assertEqual(skipped, jobs)

    def test_force_includes_directory_with_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "part.mp4").touch()
            (root / "video.mp4").touch()
            jobs = discover_jobs(root)

            pending, skipped = select_jobs(jobs, force=True)

            self.assertEqual(pending, jobs)
            self.assertEqual(skipped, [])

    def test_requeues_job_when_existing_output_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "part.mp4").touch()
            (root / "video.mp4").touch()
            jobs = discover_jobs(root)
            pending, completed = select_jobs(jobs, force=False)

            with patch("main.probe_file", side_effect=RuntimeError("moov atom not found")):
                pending, completed, invalid = requeue_invalid_outputs(pending, completed)

            self.assertEqual(pending, jobs)
            self.assertEqual(completed, [])
            self.assertEqual(invalid[0][0], jobs[0])
            self.assertIn("moov atom not found", invalid[0][1])

    def test_keeps_job_completed_when_existing_output_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "part.mp4").touch()
            (root / "video.mp4").touch()
            jobs = discover_jobs(root)
            pending, completed = select_jobs(jobs, force=False)

            with patch("main.probe_file", return_value=object()):
                pending, completed, invalid = requeue_invalid_outputs(pending, completed)

            self.assertEqual(pending, [])
            self.assertEqual(completed, jobs)
            self.assertEqual(invalid, [])


class DeleteSourcesTests(unittest.TestCase):
    def test_deletes_sources_and_preserves_output_and_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.mp4"
            second = root / "second.MP4"
            output = root / "video.mp4"
            other = root / "notes.txt"
            for path in (first, second, output, other):
                path.touch()

            deleted = delete_sources([first, second])

            self.assertEqual(deleted, 2)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue(output.exists())
            self.assertTrue(other.exists())

    def test_refuses_to_delete_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "video.mp4"
            output.touch()

            with self.assertRaises(ValueError):
                delete_sources([output])

            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()

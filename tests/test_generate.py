from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate  # noqa: E402


class ProjectsRootTests(unittest.TestCase):
    """Where a composed project lands must not depend on one person's laptop."""

    def test_the_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = generate._projects_root({generate.PROJECTS_ENV: temp})

            self.assertEqual(root, Path(temp))

    def test_a_home_relative_setting_is_expanded(self) -> None:
        root = generate._projects_root({generate.PROJECTS_ENV: "~/songs"})

        self.assertEqual(root, Path.home() / "songs")
        self.assertTrue(root.is_absolute())

    def test_an_empty_or_blank_setting_is_ignored(self) -> None:
        for value in ("", "   "):
            root = generate._projects_root({generate.PROJECTS_ENV: value})

            self.assertIn(root, (generate.DRIVE_PROJECTS, generate.REPO_PROJECTS))

    def test_the_drive_is_used_when_it_is_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            mounted = Path(temp) / "ACE-Step" / "projects"
            mounted.parent.mkdir(parents=True)
            original = generate.DRIVE_PROJECTS
            generate.DRIVE_PROJECTS = mounted
            try:
                self.assertEqual(generate._projects_root({}), mounted)
            finally:
                generate.DRIVE_PROJECTS = original

    def test_an_unmounted_drive_falls_back_rather_than_failing(self) -> None:
        """An unplugged disk changes where files land, never whether it works."""

        original = generate.DRIVE_PROJECTS
        generate.DRIVE_PROJECTS = Path("/Volumes/definitely-not-mounted-xyz/projects")
        try:
            self.assertEqual(generate._projects_root({}), generate.REPO_PROJECTS)
        finally:
            generate.DRIVE_PROJECTS = original


class ListeningCopyTests(unittest.TestCase):
    def test_a_missing_encoder_reports_rather_than_raises(self) -> None:
        """The WAV is the deliverable; the listening copy is a convenience."""

        original = generate.subprocess.run

        def refuse(*args, **kwargs):
            raise FileNotFoundError("no encoder here")

        generate.subprocess.run = refuse
        try:
            with tempfile.TemporaryDirectory() as temp:
                source = Path(temp) / "a.wav"
                source.write_bytes(b"RIFF")

                self.assertIsNone(generate._to_mp3(source, Path(temp) / "out.mp3"))
        finally:
            generate.subprocess.run = original


if __name__ == "__main__":
    unittest.main()

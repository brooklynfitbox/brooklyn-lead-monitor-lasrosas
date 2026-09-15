"""Tests for argument parsing and exit codes."""

from __future__ import annotations

import unittest

from lead_monitor.cli import EXIT_MISCONFIGURED, build_parser, main


class TestParser(unittest.TestCase):
    def test_every_command_is_wired_to_a_function(self) -> None:
        parser = build_parser()
        for command in ("run", "discover", "test-notify", "status", "init-db"):
            args = parser.parse_args([command])
            self.assertTrue(callable(args.func), command)

    def test_a_command_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_discover_takes_an_output_directory(self) -> None:
        self.assertEqual(build_parser().parse_args(["discover"]).output, "discovery")
        self.assertEqual(
            build_parser().parse_args(["discover", "--output", "/tmp/x"]).output, "/tmp/x"
        )


class TestExitCodes(unittest.TestCase):
    def test_missing_configuration_exits_with_its_own_code(self) -> None:
        """Distinct from a run failure, so CI can tell them apart."""
        import os

        prefixes = ("PORTAL_", "SMTP_", "MAIL_")
        preserved = {k: v for k, v in os.environ.items() if k.startswith(prefixes)}
        for key in preserved:
            del os.environ[key]
        try:
            with self.assertRaises(SystemExit) as ctx:
                main(["status"])
            self.assertEqual(ctx.exception.code, EXIT_MISCONFIGURED)
        finally:
            os.environ.update(preserved)


if __name__ == "__main__":
    unittest.main()

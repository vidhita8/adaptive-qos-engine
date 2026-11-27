"""Focused tests for run_config() process tracking and cleanup.

These tests verify the cleanup contract without running a live testbed.
They mock subprocess.Popen so no real processes are started.
"""

import threading
import time
import signal
import unittest
from unittest.mock import MagicMock, patch, call

from experiments.datasets.collect import _terminate_procs, _run_iperf3_rounds


# ---------------------------------------------------------------------------
# _terminate_procs
# ---------------------------------------------------------------------------

class TerminateProcsTests(unittest.TestCase):

    def _make_mock_proc(
        self,
        pid: int,
        *,
        already_dead: bool = False,
    ) -> MagicMock:
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = 0 if already_dead else None
        proc.wait.return_value = 0
        return proc

    def test_already_dead_process_group_is_ignored(self):
        """A missing process group is harmless, even if its Popen is already dead."""
        proc = self._make_mock_proc(9999, already_dead=True)

        with patch("experiments.datasets.collect.os.killpg",
                   side_effect=ProcessLookupError):
            failures = _terminate_procs([proc], label="test")

        self.assertEqual(failures, [])
        proc.wait.assert_called_once_with(timeout=5)

    def test_live_process_group_receives_sigterm_and_is_waited(self):
        proc = self._make_mock_proc(9999)

        def fake_killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch(
            "experiments.datasets.collect.os.killpg",
            side_effect=fake_killpg,
        ) as killpg:
            failures = _terminate_procs([proc], label="test")

        self.assertEqual(failures, [])
        killpg.assert_any_call(9999, signal.SIGTERM)
        proc.wait.assert_called_once_with(timeout=5)

    def test_process_group_that_survives_sigterm_receives_sigkill(self):
        proc = self._make_mock_proc(9999)
        group_exists = [True]

        def fake_killpg(pgid, sig):
            if sig == 0:
                if not group_exists[0]:
                    raise ProcessLookupError
                return

            if sig == signal.SIGKILL:
                group_exists[0] = False

        with (
            patch(
                "experiments.datasets.collect.os.killpg",
                side_effect=fake_killpg,
            ) as killpg,
            patch(
                "experiments.datasets.collect.time.monotonic",
                side_effect=[0, 0, 6],
            ),
            patch("experiments.datasets.collect.time.sleep"),
        ):
            failures = _terminate_procs([proc], label="test")

        self.assertEqual(failures, [])
        killpg.assert_any_call(9999, signal.SIGTERM)
        killpg.assert_any_call(9999, signal.SIGKILL)
        proc.wait.assert_called_once_with(timeout=5)

    def test_multiple_process_groups_all_receive_sigterm(self):
        procs = [
            self._make_mock_proc(9991),
            self._make_mock_proc(9992),
            self._make_mock_proc(9993),
            self._make_mock_proc(9994),
        ]

        def fake_killpg(pgid, sig):
            if sig == 0:
                raise ProcessLookupError

        with patch(
            "experiments.datasets.collect.os.killpg",
            side_effect=fake_killpg,
        ) as killpg:
            failures = _terminate_procs(procs, label="test")

        self.assertEqual(failures, [])
        for proc in procs:
            killpg.assert_any_call(proc.pid, signal.SIGTERM)
            proc.wait.assert_called_once_with(timeout=5)

    def test_empty_list_is_no_op(self):
        with patch("experiments.datasets.collect.os.killpg") as killpg:
            failures = _terminate_procs([], label="test")

        self.assertEqual(failures, [])
        killpg.assert_not_called()

    def test_signal_oserror_is_reported_and_cleanup_continues(self):
        bad = self._make_mock_proc(9998)
        good = self._make_mock_proc(9999)

        def fake_killpg(pgid, sig):
            if pgid == bad.pid and sig == signal.SIGTERM:
                raise PermissionError("operation not permitted")
            if sig == 0:
                raise ProcessLookupError

        with patch(
            "experiments.datasets.collect.os.killpg",
            side_effect=fake_killpg,
        ) as killpg:
            failures = _terminate_procs([bad, good], label="test")

        self.assertTrue(any("SIGTERM failed" in item for item in failures))
        killpg.assert_any_call(good.pid, signal.SIGTERM)
        bad.wait.assert_called_once_with(timeout=5)
        good.wait.assert_called_once_with(timeout=5)

def subprocess_timeout():
    """Return a subprocess.TimeoutExpired instance for use in side_effect."""
    import subprocess
    return subprocess.TimeoutExpired(cmd="iperf3", timeout=5)


# ---------------------------------------------------------------------------
# _run_iperf3_rounds — stop_event and all_procs tracking
# ---------------------------------------------------------------------------

class RunIperf3RoundsTests(unittest.TestCase):

    def _minimal_stream(self, *, rounds=1, start_delay_s=0, gap_s=0,
                        streams=1, round_duration_s=1, direction="upload",
                        bitrate=None):
        stream = MagicMock()
        stream.id = "bulk-s1"
        stream.rounds = rounds
        stream.start_delay_s = start_delay_s
        stream.gap_s = gap_s
        stream.streams = streams
        stream.round_duration_s = round_duration_s
        stream.direction = direction
        stream.bitrate = bitrate
        return stream

    def test_popen_is_appended_to_all_procs(self):
        """Every iperf3 client Popen must be registered in all_procs."""
        import subprocess
        all_procs: list = []
        lock = threading.Lock()
        stop_event = threading.Event()
        errors: list = []

        fake_proc = MagicMock()
        fake_proc.wait.return_value = 0

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = pathlib.Path(tmpdir) / "iperf3.log"
            with patch("subprocess.Popen", return_value=fake_proc):
                _run_iperf3_rounds(
                    "qos-bulk", 40000,
                    self._minimal_stream(rounds=2),
                    log_path, errors, all_procs, lock, stop_event,
                )

        self.assertEqual(errors, [])
        self.assertEqual(len(all_procs), 2)  # one per round
        self.assertIn(fake_proc, all_procs)

    def test_stop_event_aborts_before_first_round(self):
        """If stop_event is set before start, no Popen is created."""
        import subprocess
        all_procs: list = []
        lock = threading.Lock()
        stop_event = threading.Event()
        stop_event.set()   # already signalled
        errors: list = []

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = pathlib.Path(tmpdir) / "iperf3.log"
            with patch("subprocess.Popen") as mock_popen:
                _run_iperf3_rounds(
                    "qos-bulk", 40000,
                    self._minimal_stream(rounds=3, start_delay_s=1),
                    log_path, errors, all_procs, lock, stop_event,
                )
                mock_popen.assert_not_called()

        self.assertEqual(all_procs, [])

    def test_stop_event_aborts_between_rounds(self):
        """stop_event set during a gap prevents the next round from starting."""
        import subprocess
        all_procs: list = []
        lock = threading.Lock()
        stop_event = threading.Event()
        errors: list = []

        fake_proc = MagicMock()
        fake_proc.wait.return_value = 0

        call_count = [0]
        original_popen = subprocess.Popen

        def counting_popen(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Set stop_event after the first Popen is created.
                stop_event.set()
            return fake_proc

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = pathlib.Path(tmpdir) / "iperf3.log"
            with patch("subprocess.Popen", side_effect=counting_popen):
                _run_iperf3_rounds(
                    "qos-bulk", 40000,
                    self._minimal_stream(rounds=3, gap_s=0.001),
                    log_path, errors, all_procs, lock, stop_event,
                )

        # Only the first round should have fired.
        self.assertEqual(call_count[0], 1)

    def test_failed_round_records_error_and_exits(self):
        """A non-zero iperf3 exit code is added to errors and no further rounds run."""
        import subprocess
        all_procs: list = []
        lock = threading.Lock()
        stop_event = threading.Event()
        errors: list = []

        fake_proc = MagicMock()
        fake_proc.wait.return_value = 1   # simulate failure

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = pathlib.Path(tmpdir) / "iperf3.log"
            with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
                _run_iperf3_rounds(
                    "qos-bulk", 40000,
                    self._minimal_stream(rounds=3),
                    log_path, errors, all_procs, lock, stop_event,
                )
                # Only one Popen called — stopped after first failure
                self.assertEqual(mock_popen.call_count, 1)

        self.assertEqual(len(errors), 1)
        self.assertIn("round 1 failed", errors[0])

    def test_all_procs_registered_even_after_failure(self):
        """The failed-round Popen is still in all_procs (cleanup must terminate it)."""
        import subprocess
        all_procs: list = []
        lock = threading.Lock()
        stop_event = threading.Event()
        errors: list = []

        fake_proc = MagicMock()
        fake_proc.wait.return_value = 1

        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = pathlib.Path(tmpdir) / "iperf3.log"
            with patch("subprocess.Popen", return_value=fake_proc):
                _run_iperf3_rounds(
                    "qos-bulk", 40000,
                    self._minimal_stream(rounds=1),
                    log_path, errors, all_procs, lock, stop_event,
                )

        self.assertEqual(len(all_procs), 1)
        self.assertIs(all_procs[0], fake_proc)


if __name__ == "__main__":
    unittest.main()


class Iperf3WorkerExceptionTests(unittest.TestCase):

    def test_popen_exception_is_recorded_in_errors(self):
        """A Popen failure in the worker must be reported to the main thread."""
        import tempfile
        from pathlib import Path

        stream = MagicMock()
        stream.id = "bulk-test"
        stream.rounds = 1
        stream.start_delay_s = 0
        stream.gap_s = 0
        stream.streams = 1
        stream.round_duration_s = 1
        stream.direction = "upload"
        stream.bitrate = None

        errors = []
        all_procs = []
        lock = threading.Lock()
        stop_event = threading.Event()

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "iperf3.log"

            with patch(
                "experiments.datasets.collect.subprocess.Popen",
                side_effect=OSError("simulated Popen failure"),
            ):
                # The wrapper should catch the exception instead of letting
                # it escape the worker thread.
                _run_iperf3_rounds(
                    "qos-bulk",
                    40000,
                    stream,
                    log_path,
                    errors,
                    all_procs,
                    lock,
                    stop_event,
                )

        self.assertEqual(len(errors), 1)
        self.assertIn("bulk-test worker failed", errors[0])
        self.assertIn("OSError", errors[0])
        self.assertIn("simulated Popen failure", errors[0])
        self.assertEqual(all_procs, [])

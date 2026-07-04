import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from kerkov.core.process.session_registry import (
    DEFAULT_JOB_TTL_S,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_PENDING_OUTPUT_CHARS,
    DEFAULT_TAIL_CHARS,
    FinishedSession,
    ProcessSession,
    SessionRegistry,
    _cap_pending_buffer,
    _tail,
    _trim_with_cap,
    get_session_registry,
)


def _create_session(
    session_id: str = "abcd1234",
    command: str = "echo hello",
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    pending_max_output_chars: int = DEFAULT_PENDING_OUTPUT_CHARS,
    **kwargs,
) -> ProcessSession:
    return ProcessSession(
        id=session_id,
        command=command,
        started_at=time.time(),
        max_output_chars=max_output_chars,
        pending_max_output_chars=pending_max_output_chars,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _trim_with_cap
# ---------------------------------------------------------------------------
class TestTrimWithCap(unittest.TestCase):

    def test_returns_text_when_under_cap(self):
        assert _trim_with_cap("hello", 10) == "hello"

    def test_returns_text_when_exactly_at_cap(self):
        assert _trim_with_cap("hello", 5) == "hello"

    def test_trims_to_last_n_chars(self):
        assert _trim_with_cap("abcdefghij", 3) == "hij"

    def test_empty_string(self):
        assert _trim_with_cap("", 5) == ""

    def test_cap_of_zero(self):
        assert _trim_with_cap("abc", 0) == ""

    def test_cap_of_one(self):
        assert _trim_with_cap("abc", 1) == "c"


# ---------------------------------------------------------------------------
# _tail
# ---------------------------------------------------------------------------
class TestTail(unittest.TestCase):

    def test_returns_text_when_under_default(self):
        assert _tail("short") == "short"

    def test_trims_to_default_tail_chars(self):
        text = "x" * (DEFAULT_TAIL_CHARS + 500)
        result = _tail(text)
        assert len(result) == DEFAULT_TAIL_CHARS
        assert result == text[-DEFAULT_TAIL_CHARS:]

    def test_custom_max(self):
        assert _tail("abcdef", 3) == "def"

    def test_empty_string(self):
        assert _tail("") == ""


# ---------------------------------------------------------------------------
# _cap_pending_buffer
# ---------------------------------------------------------------------------
class TestCapPendingBuffer(unittest.TestCase):

    def test_noop_when_under_cap(self):
        buf = ["hello"]
        result = _cap_pending_buffer(buf, 5, 10)
        assert result == 5
        assert buf == ["hello"]

    def test_noop_when_exactly_at_cap(self):
        buf = ["12345"]
        result = _cap_pending_buffer(buf, 5, 5)
        assert result == 5
        assert buf == ["12345"]

    def test_drops_oldest_chunks(self):
        buf = ["aaa", "bbb", "ccc"]
        result = _cap_pending_buffer(buf, 9, 6)
        assert result == 6
        assert "ccc" in buf

    def test_trims_first_chunk_when_still_over(self):
        buf = ["aaabbb", "ccc"]
        result = _cap_pending_buffer(buf, 9, 7)
        assert result == 7
        # First chunk trimmed by 2 chars, second chunk intact
        assert buf[-1] == "ccc"
        assert sum(len(c) for c in buf) == 7

    def test_single_chunk_exceeding_cap_keeps_tail(self):
        buf = ["abcdefghij"]
        result = _cap_pending_buffer(buf, 10, 4)
        assert result == 4
        assert buf == ["ghij"]

    def test_empty_buffer(self):
        buf = []
        result = _cap_pending_buffer(buf, 0, 10)
        assert result == 0
        assert buf == []


# ---------------------------------------------------------------------------
# ProcessSession dataclass
# ---------------------------------------------------------------------------
class TestProcessSession(unittest.TestCase):

    def test_default_values(self):
        s = _create_session()
        assert s.exited is False
        assert s.exit_code is None
        assert s.backgrounded is False
        assert s.pending_stdout == []
        assert s.pending_stderr == []
        assert s.aggregated == ""
        assert s.tail == ""
        assert s.truncated is False
        assert s.total_output_chars == 0
        assert s.stdin is None

    def test_custom_values(self):
        s = _create_session(cwd="/tmp", pid=42)
        assert s.cwd == "/tmp"
        assert s.pid == 42


# ---------------------------------------------------------------------------
# SessionRegistry — session lifecycle
# ---------------------------------------------------------------------------
class TestSessionRegistryLifecycle(unittest.TestCase):

    def setUp(self):
        self.reg = SessionRegistry(job_ttl_s=60)

    def test_create_session_id_returns_8_hex_chars(self):
        sid = self.reg.create_session_id()
        assert len(sid) == 8
        int(sid, 16)  # Should not raise — valid hex

    def test_create_session_id_unique(self):
        ids = {self.reg.create_session_id() for _ in range(50)}
        assert len(ids) == 50

    def test_add_and_get_session(self):
        s = _create_session()
        self.reg.add_session(s)
        assert self.reg.get_session(s.id) is s

    def test_get_session_returns_none_for_unknown_id(self):
        assert self.reg.get_session("nonexistent") is None

    def test_get_finished_session_returns_none_for_unknown_id(self):
        assert self.reg.get_finished_session("nonexistent") is None

    def test_delete_session_removes_running(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.delete_session(s.id)
        assert self.reg.get_session(s.id) is None

    def test_delete_session_removes_finished(self):
        s = _create_session()
        s.backgrounded = True
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")
        assert self.reg.get_finished_session(s.id) is not None
        self.reg.delete_session(s.id)
        assert self.reg.get_finished_session(s.id) is None

    def test_delete_nonexistent_is_noop(self):
        self.reg.delete_session("ghost")  # Should not raise


# ---------------------------------------------------------------------------
# SessionRegistry — mark_exited
# ---------------------------------------------------------------------------
class TestSessionRegistryMarkExited(unittest.TestCase):

    def setUp(self):
        self.reg = SessionRegistry(job_ttl_s=60)

    def test_moves_backgrounded_session_to_finished(self):
        s = _create_session()
        s.backgrounded = True
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        assert self.reg.get_session(s.id) is None
        finished = self.reg.get_finished_session(s.id)
        assert finished is not None
        assert finished.exit_code == 0
        assert finished.status == "completed"

    def test_non_backgrounded_session_not_kept_in_finished(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=1, exit_signal=None, status="failed")

        assert self.reg.get_session(s.id) is None
        assert self.reg.get_finished_session(s.id) is None

    def test_sets_exit_code_and_signal_on_session(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=None, exit_signal=9, status="killed")
        assert s.exited is True
        assert s.exit_code is None
        assert s.exit_signal == 9

    def test_closes_stdin_on_exit(self):
        mock_stdin = MagicMock()
        s = _create_session()
        s.stdin = mock_stdin
        s.backgrounded = True
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        mock_stdin.close.assert_called_once()
        assert s.stdin is None

    def test_stdin_close_error_suppressed(self):
        mock_stdin = MagicMock()
        mock_stdin.close.side_effect = OSError("broken pipe")
        s = _create_session()
        s.stdin = mock_stdin
        s.backgrounded = True
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")
        # Should not raise
        assert s.stdin is None

    def test_stdin_closed_outside_lock(self):
        lock_held_during_close = []

        class SlowStdin:
            def close(self_inner):
                # Check if the registry lock is held during close
                acquired = self.reg._lock.acquire(blocking=False)
                if acquired:
                    lock_held_during_close.append(False)
                    self.reg._lock.release()
                else:
                    lock_held_during_close.append(True)

        s = _create_session()
        s.stdin = SlowStdin()
        s.backgrounded = True
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        assert len(lock_held_during_close) == 1
        assert (
            lock_held_during_close[0] is False
        ), "stdin.close() should be called outside the lock"

    def test_finished_session_captures_aggregated_and_tail(self):
        s = _create_session()
        s.backgrounded = True
        s.aggregated = "full output here"
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        finished = self.reg.get_finished_session(s.id)
        assert finished.aggregated == "full output here"
        assert finished.tail == "full output here"


# ---------------------------------------------------------------------------
# SessionRegistry — mark_backgrounded
# ---------------------------------------------------------------------------
class TestSessionRegistryBackgrounding(unittest.TestCase):

    def setUp(self):
        self.reg = SessionRegistry(job_ttl_s=60)

    def test_mark_backgrounded(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.mark_backgrounded(s)
        assert s.backgrounded is True

    def test_list_running_sessions_only_returns_backgrounded(self):
        s1 = _create_session(session_id="aaaa0001")
        s2 = _create_session(session_id="aaaa0002")
        self.reg.add_session(s1)
        self.reg.add_session(s2)
        self.reg.mark_backgrounded(s1)

        running = self.reg.list_running_sessions()
        assert len(running) == 1
        assert running[0].id == "aaaa0001"

    def test_list_finished_sessions(self):
        s = _create_session()
        s.backgrounded = True
        self.reg.add_session(s)
        self.reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        finished = self.reg.list_finished_sessions()
        assert len(finished) == 1
        assert finished[0].id == s.id


# ---------------------------------------------------------------------------
# SessionRegistry — append_output
# ---------------------------------------------------------------------------
class TestSessionRegistryAppendOutput(unittest.TestCase):

    def setUp(self):
        self.reg = SessionRegistry(job_ttl_s=60)

    def test_appends_stdout_chunk(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "hello ")
        self.reg.append_output(s, "stdout", "world")

        assert s.pending_stdout == ["hello ", "world"]
        assert s.pending_stdout_chars == 11
        assert s.aggregated == "hello world"
        assert s.total_output_chars == 11

    def test_appends_stderr_chunk(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stderr", "error!")

        assert s.pending_stderr == ["error!"]
        assert s.pending_stderr_chars == 6
        assert s.aggregated == "error!"

    def test_truncates_when_pending_exceeds_cap(self):
        s = _create_session(pending_max_output_chars=10)
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "a" * 6)
        self.reg.append_output(s, "stdout", "b" * 6)

        assert s.truncated is True
        assert s.pending_stdout_chars <= 10

    def test_truncates_when_aggregated_exceeds_cap(self):
        s = _create_session(max_output_chars=10)
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "a" * 15)

        assert s.truncated is True
        assert len(s.aggregated) == 10
        assert s.aggregated == "a" * 10

    def test_tail_updated_on_append(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "x" * 3000)

        assert len(s.tail) == DEFAULT_TAIL_CHARS

    def test_empty_chunk(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "")

        assert s.pending_stdout == [""]
        assert s.total_output_chars == 0
        assert s.aggregated == ""


# ---------------------------------------------------------------------------
# SessionRegistry — drain_session
# ---------------------------------------------------------------------------
class TestSessionRegistryDrain(unittest.TestCase):

    def setUp(self):
        self.reg = SessionRegistry(job_ttl_s=60)

    def test_drain_returns_and_clears_buffers(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "out1")
        self.reg.append_output(s, "stderr", "err1")

        stdout, stderr = self.reg.drain_session(s)
        assert stdout == "out1"
        assert stderr == "err1"
        assert s.pending_stdout == []
        assert s.pending_stderr == []
        assert s.pending_stdout_chars == 0
        assert s.pending_stderr_chars == 0

    def test_drain_empty_session(self):
        s = _create_session()
        self.reg.add_session(s)
        stdout, stderr = self.reg.drain_session(s)
        assert stdout == ""
        assert stderr == ""

    def test_drain_is_idempotent(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "data")

        self.reg.drain_session(s)
        stdout, stderr = self.reg.drain_session(s)
        assert stdout == ""
        assert stderr == ""

    def test_drain_does_not_affect_aggregated(self):
        s = _create_session()
        self.reg.add_session(s)
        self.reg.append_output(s, "stdout", "persistent")

        self.reg.drain_session(s)
        assert s.aggregated == "persistent"


# ---------------------------------------------------------------------------
# SessionRegistry — sweeper / pruning
# ---------------------------------------------------------------------------
class TestSessionRegistryPruning(unittest.TestCase):

    def test_prune_removes_expired_finished_sessions(self):
        reg = SessionRegistry(job_ttl_s=0)  # Expire immediately
        s = _create_session()
        s.backgrounded = True
        reg.add_session(s)
        reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        assert reg.get_finished_session(s.id) is not None
        reg._prune_finished()
        assert reg.get_finished_session(s.id) is None

    def test_prune_keeps_recent_sessions(self):
        reg = SessionRegistry(job_ttl_s=3600)
        s = _create_session()
        s.backgrounded = True
        reg.add_session(s)
        reg.mark_exited(s, exit_code=0, exit_signal=None, status="completed")

        reg._prune_finished()
        assert reg.get_finished_session(s.id) is not None

    @patch("kerkov.core.process.session_registry.threading.Timer")
    def test_start_sweeper_creates_daemon_timer(self, mock_timer_cls):
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        reg = SessionRegistry(job_ttl_s=60)
        s = _create_session()
        reg.add_session(s)

        mock_timer_cls.assert_called_once()
        assert mock_timer.daemon is True
        mock_timer.start.assert_called_once()

    @patch("kerkov.core.process.session_registry.threading.Timer")
    def test_start_sweeper_not_duplicated(self, mock_timer_cls):
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        reg = SessionRegistry(job_ttl_s=60)
        s1 = _create_session(session_id="aaaa0001")
        s2 = _create_session(session_id="aaaa0002")
        reg.add_session(s1)
        reg.add_session(s2)

        # Only one timer created despite two add_session calls
        mock_timer_cls.assert_called_once()


# ---------------------------------------------------------------------------
# SessionRegistry — _reap_stale_running
# ---------------------------------------------------------------------------
class TestSessionRegistryReapStale(unittest.TestCase):

    def test_reaps_exited_running_session(self):
        reg = SessionRegistry(job_ttl_s=60)
        s = _create_session()
        s.backgrounded = True
        reg.add_session(s)

        # Simulate adapter marking exited without going through mark_exited
        s.exited = True
        s.exit_code = 0

        assert reg.get_session(s.id) is not None
        reg._reap_stale_running()
        assert reg.get_session(s.id) is None
        finished = reg.get_finished_session(s.id)
        assert finished is not None
        assert finished.status == "completed"

    def test_reaps_failed_session_with_nonzero_exit(self):
        reg = SessionRegistry(job_ttl_s=60)
        s = _create_session()
        s.backgrounded = True
        reg.add_session(s)

        s.exited = True
        s.exit_code = 1

        reg._reap_stale_running()
        finished = reg.get_finished_session(s.id)
        assert finished is not None
        assert finished.status == "failed"

    def test_does_not_reap_still_running_session(self):
        reg = SessionRegistry(job_ttl_s=60)
        s = _create_session()
        reg.add_session(s)

        reg._reap_stale_running()
        assert reg.get_session(s.id) is not None

    def test_reap_noop_when_no_sessions(self):
        reg = SessionRegistry(job_ttl_s=60)
        reg._reap_stale_running()  # Should not raise


# ---------------------------------------------------------------------------
# get_session_registry singleton
# ---------------------------------------------------------------------------
class TestGetSessionRegistry(unittest.TestCase):

    def tearDown(self):
        # Reset the singleton between tests
        import kerkov.core.process.session_registry as mod

        mod._registry = None

    def test_returns_session_registry_instance(self):
        reg = get_session_registry()
        assert isinstance(reg, SessionRegistry)

    def test_returns_same_instance(self):
        r1 = get_session_registry()
        r2 = get_session_registry()
        assert r1 is r2

    def test_thread_safe_singleton(self):
        import kerkov.core.process.session_registry as mod

        mod._registry = None

        results = []
        barrier = threading.Barrier(4)

        def get_reg():
            barrier.wait()
            results.append(get_session_registry())

        threads = [threading.Thread(target=get_reg) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# ---------------------------------------------------------------------------
# FinishedSession dataclass
# ---------------------------------------------------------------------------
class TestFinishedSession(unittest.TestCase):

    def test_default_values(self):
        f = FinishedSession(id="test", command="ls", started_at=1.0, ended_at=2.0)
        assert f.status == "completed"
        assert f.exit_code is None
        assert f.aggregated == ""
        assert f.truncated is False

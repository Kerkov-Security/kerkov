"""Tests for the live agent event bus + row state (issue #148, Stage 1)."""

import unittest

from kerkov.agents.agent_events import (
    AgentEvent,
    AgentEventBus,
    AgentEventKind,
    AgentRowState,
    build_rows,
)


def _event(kind: AgentEventKind, run_id: str = "a1", detail: str = "") -> AgentEvent:
    return AgentEvent(
        agent_run_id=run_id, kind=kind, agent_type="recon", detail=detail
    )


class AgentEventBusTest(unittest.TestCase):
    def test_emit_fans_out_to_all_listeners(self):
        bus = AgentEventBus()
        seen_a = []
        seen_b = []
        bus.subscribe(seen_a.append)
        bus.subscribe(seen_b.append)

        evt = _event(AgentEventKind.STARTED)
        bus.emit(evt)

        self.assertEqual(seen_a, [evt])
        self.assertEqual(seen_b, [evt])

    def test_unsubscribe_stops_delivery(self):
        bus = AgentEventBus()
        seen = []
        unsub = bus.subscribe(seen.append)

        bus.emit(_event(AgentEventKind.STARTED))
        unsub()
        bus.emit(_event(AgentEventKind.TURN_END))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].kind, AgentEventKind.STARTED)

    def test_raising_listener_does_not_break_others_or_bus(self):
        bus = AgentEventBus()
        good = []

        def _boom(_evt):
            raise RuntimeError("render blew up")

        bus.subscribe(_boom)
        bus.subscribe(good.append)

        # Must not raise — a broken live view can't crash the agent loop.
        bus.emit(_event(AgentEventKind.TOOL_START, detail="http"))

        self.assertEqual(len(good), 1)


class AgentRowStateTest(unittest.TestCase):
    def _row(self) -> AgentRowState:
        return AgentRowState(agent_run_id="a1", agent_type="recon", task="scan")

    def test_tool_start_sets_current_tool_and_running(self):
        row = self._row()
        row.apply(_event(AgentEventKind.TOOL_START, detail="http"))
        self.assertEqual(row.current_tool, "http")
        self.assertEqual(row.status, "running")

    def test_tool_end_clears_current_tool(self):
        row = self._row()
        row.apply(_event(AgentEventKind.TOOL_START, detail="http"))
        row.apply(_event(AgentEventKind.TOOL_END, detail="http"))
        self.assertIsNone(row.current_tool)

    def test_turn_end_increments_iterations(self):
        row = self._row()
        row.apply(_event(AgentEventKind.TURN_END))
        row.apply(_event(AgentEventKind.TURN_END))
        self.assertEqual(row.iterations, 2)

    def test_finished_settles_reported_status(self):
        row = self._row()
        row.apply(_event(AgentEventKind.FINISHED, detail="completed"))
        self.assertTrue(row.finished)
        self.assertEqual(row.status, "completed")
        self.assertIsNone(row.current_tool)

    def test_error_marks_row_error(self):
        row = self._row()
        row.apply(_event(AgentEventKind.ERROR, detail="boom"))
        self.assertEqual(row.status, "error")
        self.assertEqual(row.last_text, "boom")

    def test_build_rows_orders_by_start_then_id(self):
        r1 = AgentRowState("z", "recon", "t", started_ts=1.0)
        r2 = AgentRowState("a", "recon", "t", started_ts=2.0)
        r3 = AgentRowState("b", "recon", "t", started_ts=1.0)
        rows = build_rows({"z": r1, "a": r2, "b": r3})
        # started_ts 1.0 rows first, tie broken by id: b before z; then a.
        self.assertEqual([r.agent_run_id for r in rows], ["b", "z", "a"])


if __name__ == "__main__":
    unittest.main()

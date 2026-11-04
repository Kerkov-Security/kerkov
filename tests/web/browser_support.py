"""Helpers for driving the dashboard's fake WebSocket in browser tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page

FAKE_SOCKET_SCRIPT = """class TestSocket extends EventTarget {
  static OPEN = 1;
  static CLOSED = 3;
  constructor() {
    super();
    this.readyState = 0;
    this.sent = [];
    window.testSocket = this;
    queueMicrotask(() => {
      this.readyState = TestSocket.OPEN;
      this.dispatchEvent(new Event('open'));
    });
  }
  send(data) {
    const message = JSON.parse(data);
    this.sent.push(message);
    (window.sentEvents ||= []).push(message);
  }
  emit(message) {
    this.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify(message)
    }));
  }
  close() {
    this.readyState = TestSocket.CLOSED;
    this.dispatchEvent(new Event('close'));
  }
}
window.WebSocket = TestSocket;"""


def emit(page: Page, event: str, payload: dict[str, Any]) -> None:
    page.evaluate(
        "message => window.testSocket.emit(message)",
        {"type": event, "payload": payload},
    )


def sent(page: Page, event: str) -> list[dict[str, Any]]:
    return page.evaluate(
        "type => window.testSocket.sent.filter(message => message.type === type)", event
    )


def last_request(page: Page, event: str) -> dict[str, Any]:
    page.wait_for_function(
        "type => window.testSocket.sent.some(message => message.type === type)",
        arg=event,
    )
    return sent(page, event)[-1]

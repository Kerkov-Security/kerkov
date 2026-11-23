"""An `OutputSink` that records what a workflow run narrated, for assertions."""

from contextlib import nullcontext

from kerkov.core.conversation_engine import OutputSink


class RecordingSink(OutputSink):
    """Collects every engine callback instead of rendering it."""

    def __init__(self):
        self.assistant = []
        self.infos = []
        self.warnings = []
        self.errors = []
        self.results = []

    def status(self, label):
        return nullcontext()

    def info(self, msg):
        self.infos.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)

    def assistant_text(self, content):
        self.assistant.append(content)

    def final(self, content):
        self.assistant.append(content)

    def tool_start(self, tool_name, params):
        pass

    def tool_succeeded(self, tool_name, result):
        self.results.append((tool_name, True))

    def tool_failed(self, tool_name, result):
        self.results.append((tool_name, False))

    def token_usage(self, usage):
        pass

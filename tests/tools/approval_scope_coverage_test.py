"""Every shipped tool must answer for its own approval breadth."""

import inspect
import pkgutil
import unittest
from importlib import import_module

from kerkov.core.approval_scope import ApprovalScope
from kerkov.tools.base_tool import BaseTool


def _all_tool_classes():
    """Import every tool module and collect concrete BaseTool subclasses."""
    seen = {}
    for package in ("kerkov.tools", "kerkov.agents"):
        root = import_module(package)
        for info in pkgutil.walk_packages(root.__path__, f"{package}."):
            try:
                module = import_module(info.name)
            except Exception:  # optional native deps are not the subject here
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseTool)
                    and obj is not BaseTool
                    and not inspect.isabstract(obj)
                ):
                    seen[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return seen


class ApprovalScopeCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tools = _all_tool_classes()

    def test_tool_classes_were_discovered(self):
        self.assertGreater(len(self.tools), 10)

    def test_every_tool_declares_its_own_scope(self):
        """A tool inheriting someone else's breadth is a grant nobody chose."""
        inherited = [
            name
            for name, cls in self.tools.items()
            if "approval_scope" not in vars(cls)
        ]
        self.assertEqual(inherited, [])

    def test_declared_scopes_are_well_formed(self):
        for name, cls in self.tools.items():
            with self.subTest(tool=name):
                scope = cls.approval_scope(cls, {})
                self.assertIsInstance(scope, ApprovalScope)
                if scope.broad_params is not None:
                    for key, pattern in scope.broad_params.items():
                        self.assertIsInstance(key, str)
                        self.assertIsInstance(pattern, str)


if __name__ == "__main__":
    unittest.main()

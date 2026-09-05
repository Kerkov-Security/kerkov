"""Include repository-maintained runtime assets in wheels (also from sdists)."""

from pathlib import Path
from shutil import copy2

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithAssets(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        for directory, patterns in {
            "skills": ("**/*.md", "**/*.py"),
            "agents": ("**/*.md",),
            "config": ("http_profiles.yaml",),
        }.items():
            for pattern in patterns:
                for source in (root / directory).glob(pattern):
                    relative = source.relative_to(root)
                    target = Path(self.build_lib) / "kerkov" / "bundled" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copy2(source, target)


setup(cmdclass={"build_py": BuildWithAssets})

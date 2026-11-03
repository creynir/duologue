"""duologue: one conversation, two coding agents. Switch sides whenever you like."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("duologue")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0+unknown"

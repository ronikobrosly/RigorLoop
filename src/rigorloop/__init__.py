"""RigorLoop: statistically-sound agentic build loops over dev/val/test splits."""

from importlib.metadata import PackageNotFoundError, version


def _package_version() -> str:
    try:
        return version("rigorloop")
    except PackageNotFoundError:
        try:
            from rigorloop._version import __version__

            return __version__
        except ImportError:
            return "0.0.0+unknown"


__version__ = _package_version()

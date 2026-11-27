from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("catering-competitor-intelligence")
except PackageNotFoundError:
    __version__ = "0+unknown"

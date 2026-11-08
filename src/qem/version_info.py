def version() -> str:
    try:
        from .version import __version__
    except ImportError:  # not built yet: run `uv sync`
        return "0+unknown"
    return __version__

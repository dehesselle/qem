# SPDX-FileCopyrightText: 2026 René de Hesselle <dehesselle@web.de>
#
# SPDX-License-Identifier: 0BSD


def version() -> str:
    try:
        from .version import __version__
    except ImportError:  # not built yet: run `uv sync`
        return "0+unknown"
    return __version__

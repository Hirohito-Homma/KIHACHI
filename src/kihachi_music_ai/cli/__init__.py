"""The command line, as a package rather than one long module.

``main`` is the only name the outside world uses: the ``kihachi`` entry point,
``python -m kihachi_music_ai``, and every test that drives a command through a
list of strings. It stays importable from here no matter how the commands
underneath are arranged, so moving a command between modules never moves the
import that finds it.

The bodies still live in :mod:`._legacy` while they are being pulled out into
modules of their own (:mod:`.parser`, :mod:`.song`, :mod:`.connection`).
"""

from __future__ import annotations

from ._legacy import build_parser, main

__all__ = ["build_parser", "main"]

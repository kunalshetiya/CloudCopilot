"""Lets tests import ``agent`` without a packaging/install step.

Pytest's default import mode adds a test file's nearest ancestor directory
without an ``__init__.py`` to ``sys.path``. Since ``tests/`` has no
``__init__.py``, that would normally be ``tests/`` itself, not this directory.
The presence of this file makes pytest add this directory too.
"""

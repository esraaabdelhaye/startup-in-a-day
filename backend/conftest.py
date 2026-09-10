"""Makes ``backend/`` importable so tests can ``import parser``.

pytest prepends the directory holding the topmost ``conftest.py`` to
``sys.path``, so this file existing here is what lets the suite be run as
``pytest tests/`` from inside ``backend/``.
"""

"""Static analysis of Spring Boot repositories into execution manifests.

The analyzer lives in :mod:`parser.spring_analyze`.  It is deliberately not
imported here: ``python -m parser.spring_analyze`` executes the submodule after
importing this package, and re-exporting it eagerly makes the interpreter warn
about the module being present in ``sys.modules`` twice.
"""

__all__: "list[str]" = []

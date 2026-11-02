"""Root test configuration shared by every test package."""

# pytester must be registered from the rootdir conftest (pytest deprecation of
# ``pytest_plugins`` in nested conftest files); only tests/pytest_plugin uses it.
pytest_plugins = ["pytester"]

"""Test environment — must run before any EMUNEL module import.

Isolated DB, isolated data root, isolated port range, debug mode so the
placeholder-secret production validation does not fire in tests.
"""

import os

os.environ["EMUNEL_DEBUG"] = "true"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/emunel-it.db"
os.environ["EMUNEL_DATA_ROOT"] = "/tmp/emunel-it-data"
os.environ["EMUNEL_PORT_RANGE_START"] = "19200"
os.environ["EMUNEL_PORT_RANGE_END"] = "19299"
os.environ["EMUNEL_SECRET_KEY"] = "integration-test-secret-key-0123456789abcdef"
os.environ["EMUNEL_JWT_SECRET_KEY"] = "integration-test-jwt-secret-0123456789abcdef"
os.environ["EMUNEL_ADMIN_PASSWORD"] = "integration-admin-pw-0123456789"
os.environ["EMUNEL_SYNC_INTERVAL"] = "2"

"""Pytest configuration for zfs_migration tests."""
import pytest


@pytest.fixture
def log_file(tmp_path):
    """Provide a temporary log file path, patching the module-level LOG_FILE."""
    return tmp_path / "test_migration.log"


@pytest.fixture(autouse=True)
def reset_module_state(log_file):
    """Reset mutable module state before each test."""
    import zfs_migration

    # Reset global state
    zfs_migration.ACTIVE_PROCESSES.clear()
    zfs_migration.SHUTTING_DOWN = False
    zfs_migration.FAILED_JOBS.clear()
    zfs_migration.LOG_FILE = log_file

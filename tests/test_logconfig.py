import logging

from bsetl.logconfig import get_logger, setup_logging


def test_loggers_hang_off_one_package_root():
    """One switch controls the package's output."""
    assert get_logger("bsetl.ingest.crawler").name == "bsetl.ingest.crawler"
    assert get_logger("bsetl").name == "bsetl"


def test_verbosity_selects_the_level():
    setup_logging(quiet=True)
    assert logging.getLogger("bsetl").level == logging.WARNING
    setup_logging(verbosity=0)
    assert logging.getLogger("bsetl").level == logging.INFO
    setup_logging(verbosity=1)
    assert logging.getLogger("bsetl").level == logging.DEBUG


def test_repeated_setup_does_not_stack_handlers():
    for _ in range(3):
        setup_logging()
    assert len(logging.getLogger("bsetl").handlers) == 1


def test_logs_go_to_stderr_so_stdout_stays_parseable():
    """bsetl-ingest prints its run summary as JSON on stdout."""
    import sys
    setup_logging()
    (handler,) = logging.getLogger("bsetl").handlers
    assert handler.stream is sys.stderr

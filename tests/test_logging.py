"""Tests for the kernel logging bridge."""

import pybitcoinkernel as pbk


def test_logging_connection_receives_messages(tmp_path):
    messages = []
    connection = pbk.LoggingConnection(messages.append)
    try:
        pbk.logging_enable_category(pbk.LogCategory.KERNEL)
        pbk.logging_set_level_category(pbk.LogCategory.KERNEL, pbk.LogLevel.DEBUG)
        # Creating a chainstate produces kernel log output.
        with pbk.load_chainstate(pbk.ChainType.REGTEST, tmp_path / "data"):
            pass
    finally:
        pbk.logging_disable_category(pbk.LogCategory.KERNEL)
        connection.close()

    assert messages
    assert all(isinstance(m, str) for m in messages)


def test_logging_set_options():
    pbk.logging_set_options(log_timestamps=False, log_threadnames=False)
    pbk.logging_set_options(log_timestamps=True)

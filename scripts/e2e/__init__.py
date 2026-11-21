"""The live phone driver: acts as the owner in the 1:1 room and asserts on what the relay bot posts back.

`scripts/phone_e2e.py` is the entry point. Everything here takes its I/O as an argument (a `WebexUser`,
a `Clock`), so `tests/test_e2e_driver.py` runs every scenario against a fake relay without a network.
"""

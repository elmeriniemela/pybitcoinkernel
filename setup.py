"""Build configuration for the _bitcoinkernel C extension.

The extension links against libbitcoinkernel. By default it looks for the
library in the project-local ``vendor/`` prefix (see README for how to
build it there); override with:

    BITCOINKERNEL_INCLUDE_DIR  directory containing bitcoinkernel.h
    BITCOINKERNEL_LIB_DIR      directory containing libbitcoinkernel.so
"""

import os

from setuptools import Extension, setup

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR_PREFIX = os.path.join(PROJECT_ROOT, "vendor")

include_dir = os.environ.get(
    "BITCOINKERNEL_INCLUDE_DIR", os.path.join(VENDOR_PREFIX, "include")
)
lib_dir = os.environ.get("BITCOINKERNEL_LIB_DIR", os.path.join(VENDOR_PREFIX, "lib"))

setup(
    ext_modules=[
        Extension(
            name="pybitcoinkernel._bitcoinkernel",
            sources=["src/_bitcoinkernel.c"],
            include_dirs=[include_dir],
            library_dirs=[lib_dir],
            runtime_library_dirs=[lib_dir],
            libraries=["bitcoinkernel"],
        )
    ],
)

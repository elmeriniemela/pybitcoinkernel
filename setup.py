"""Build configuration for the _bitcoinkernel C extension.

The extension links against libbitcoinkernel. The kernel library is
located (in order of precedence):

1. BITCOINKERNEL_INCLUDE_DIR / BITCOINKERNEL_LIB_DIR - directories
   containing bitcoinkernel.h and libbitcoinkernel.so. Used by CI, or
   when linking against a kernel installed elsewhere.
2. BITCOINKERNEL_SOURCE_DIR - a Bitcoin Core source tree; the kernel is
   compiled from it with cmake and bundled into the package.
3. vendor/ - a project-local prebuilt prefix (vendor/include,
   vendor/lib). Convenient for development; see README.
4. external/bitcoin - the pinned git submodule; same as 2. This is what
   a plain `pip install git+https://...` uses: pip checks out the
   submodule, the kernel is compiled from it (several minutes and needs
   cmake, a C++20 compiler, and Boost headers), and libbitcoinkernel is
   bundled into the installed package with an $ORIGIN-relative rpath.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext as _build_ext

PROJECT_ROOT = Path(__file__).resolve().parent
VENDOR_PREFIX = PROJECT_ROOT / "vendor"
SUBMODULE_DIR = PROJECT_ROOT / "external" / "bitcoin"

KERNEL_CMAKE_FLAGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_SHARED_LIBS=ON",
    "-DBUILD_KERNEL_LIB=ON",
    "-DBUILD_DAEMON=OFF",
    "-DBUILD_CLI=OFF",
    "-DBUILD_TX=OFF",
    "-DBUILD_UTIL=OFF",
    "-DBUILD_TESTS=OFF",
    "-DBUILD_BENCH=OFF",
    "-DBUILD_FUZZ_BINARY=OFF",
    "-DENABLE_WALLET=OFF",
    "-DWITH_ZMQ=OFF",
]

NO_KERNEL_HELP = """\
Could not locate or build libbitcoinkernel. Either:
  * set BITCOINKERNEL_INCLUDE_DIR and BITCOINKERNEL_LIB_DIR to a prebuilt
    kernel (bitcoinkernel.h / libbitcoinkernel.so), or
  * set BITCOINKERNEL_SOURCE_DIR to a Bitcoin Core source tree, or
  * provide the vendor/ prefix or the external/bitcoin submodule
    (git submodule update --init) so the kernel can be built from source
    (requires cmake >= 3.22, a C++20 compiler, and Boost headers).
See the README for details.\
"""


class build_ext(_build_ext):
    def build_extension(self, ext):
        if ext.name != "pybitcoinkernel._bitcoinkernel":
            super().build_extension(ext)
            return
        bundled_libs = self._resolve_kernel(ext)
        super().build_extension(ext)
        if bundled_libs:
            dest = Path(self.get_ext_fullpath(ext.name)).resolve().parent
            for lib in bundled_libs:
                print(f"bundling {lib.name} into package", flush=True)
                shutil.copy2(lib, dest / lib.name)

    def _resolve_kernel(self, ext):
        """Point the extension at a kernel library. Returns the library
        files to bundle into the package, or None when linking against a
        prebuilt library that stays external."""
        env_include = os.environ.get("BITCOINKERNEL_INCLUDE_DIR")
        env_lib = os.environ.get("BITCOINKERNEL_LIB_DIR")
        env_source = os.environ.get("BITCOINKERNEL_SOURCE_DIR")

        if env_include or env_lib:
            include_dir = env_include or str(VENDOR_PREFIX / "include")
            lib_dir = env_lib or str(VENDOR_PREFIX / "lib")
            self._use_prebuilt(ext, include_dir, lib_dir)
            return None

        if env_source:
            return self._build_kernel(ext, Path(env_source))

        if any((VENDOR_PREFIX / "lib").glob("libbitcoinkernel.*")):
            self._use_prebuilt(
                ext, str(VENDOR_PREFIX / "include"), str(VENDOR_PREFIX / "lib")
            )
            return None

        if (SUBMODULE_DIR / "CMakeLists.txt").exists():
            return self._build_kernel(ext, SUBMODULE_DIR)

        raise RuntimeError(NO_KERNEL_HELP)

    def _use_prebuilt(self, ext, include_dir, lib_dir):
        print(f"linking against prebuilt libbitcoinkernel in {lib_dir}", flush=True)
        ext.include_dirs.append(include_dir)
        ext.library_dirs.append(lib_dir)
        ext.runtime_library_dirs.append(lib_dir)

    def _build_kernel(self, ext, source_dir):
        source_dir = source_dir.resolve()
        header = source_dir / "src" / "kernel" / "bitcoinkernel.h"
        if not header.exists():
            raise RuntimeError(
                f"{source_dir} does not look like a Bitcoin Core source tree "
                f"({header} not found)"
            )
        if shutil.which("cmake") is None:
            raise RuntimeError(
                "cmake is required to build libbitcoinkernel from source; "
                "install cmake >= 3.22 or provide a prebuilt library "
                "(see the README)"
            )

        build_dir = Path(self.build_temp).resolve() / "bitcoinkernel-build"
        print(f"building libbitcoinkernel from {source_dir}", flush=True)
        print("(this compiles Bitcoin Core's kernel; expect several minutes)", flush=True)
        subprocess.run(
            ["cmake", "-S", str(source_dir), "-B", str(build_dir), *KERNEL_CMAKE_FLAGS],
            check=True,
        )
        subprocess.run(
            [
                "cmake", "--build", str(build_dir),
                "--target", "bitcoinkernel",
                "--parallel", str(os.cpu_count() or 2),
            ],
            check=True,
        )

        lib_dir = build_dir / "lib"
        libs = sorted(lib_dir.glob("libbitcoinkernel.so*")) + sorted(
            lib_dir.glob("libbitcoinkernel*.dylib")
        )
        if not libs:
            raise RuntimeError(f"kernel build produced no library in {lib_dir}")

        ext.include_dirs.append(str(header.parent))
        ext.library_dirs.append(str(lib_dir))
        # The library is bundled next to the extension module, so resolve
        # it relative to the extension at runtime.
        origin = "@loader_path" if sys.platform == "darwin" else "$ORIGIN"
        ext.extra_link_args.append(f"-Wl,-rpath,{origin}")
        return libs


setup(
    cmdclass={"build_ext": build_ext},
    ext_modules=[
        Extension(
            name="pybitcoinkernel._bitcoinkernel",
            sources=["src/_bitcoinkernel.c"],
            libraries=["bitcoinkernel"],
        )
    ],
)

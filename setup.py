"""Build configuration for the _bitcoinkernel C extension.

The extension links against libbitcoinkernel. The kernel library is
located (in order of precedence):

1. BITCOINKERNEL_INCLUDE_DIR / BITCOINKERNEL_LIB_DIR - directories
   containing bitcoinkernel.h and libbitcoinkernel.so, for linking
   against a kernel installed elsewhere.
2. BITCOINKERNEL_SOURCE_DIR - a Bitcoin Core source tree; the kernel is
   compiled from it with cmake and bundled into the package.
3. vendor/build - a project-local cmake build dir of the kernel (see
   README "Developing"). The header is taken from the source tree
   recorded in the build's CMakeCache.txt, so the header and library
   always match. Used by local development and CI.
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
KERNEL_BUILD_DIR = PROJECT_ROOT / "vendor" / "build"
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
    # Multiprocess support would require Cap'n Proto; the kernel lib
    # doesn't need it.
    "-DENABLE_IPC=OFF",
]

NO_KERNEL_HELP = """\
Could not locate or build libbitcoinkernel. Either:
  * set BITCOINKERNEL_INCLUDE_DIR and BITCOINKERNEL_LIB_DIR to a prebuilt
    kernel (bitcoinkernel.h / libbitcoinkernel.so), or
  * set BITCOINKERNEL_SOURCE_DIR to a Bitcoin Core source tree, or
  * provide a kernel cmake build in vendor/build or the external/bitcoin
    submodule (git submodule update --init) so the kernel can be built
    from source (requires cmake >= 3.22, a C++20 compiler, and Boost
    headers).
See the README for details.\
"""


def find_kernel_libs(lib_dir):
    return sorted(lib_dir.glob("libbitcoinkernel.so*")) + sorted(
        lib_dir.glob("libbitcoinkernel*.dylib")
    )


def cmake_source_dir(build_dir):
    """The source tree a cmake build dir was configured from, or None."""
    cache = build_dir / "CMakeCache.txt"
    if not cache.exists():
        return None
    for line in cache.read_text().splitlines():
        if line.startswith("CMAKE_HOME_DIRECTORY"):
            return Path(line.split("=", 1)[1].strip())
    return None


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
            if not (env_include and env_lib):
                raise RuntimeError(
                    "set both BITCOINKERNEL_INCLUDE_DIR and BITCOINKERNEL_LIB_DIR "
                    "(or neither)"
                )
            self._use_prebuilt(ext, env_include, env_lib)
            return None

        if env_source:
            return self._build_kernel(ext, Path(env_source))

        if find_kernel_libs(KERNEL_BUILD_DIR / "lib"):
            self._use_build_dir(ext, KERNEL_BUILD_DIR)
            return None

        if (SUBMODULE_DIR / "CMakeLists.txt").exists():
            return self._build_kernel(ext, SUBMODULE_DIR)

        raise RuntimeError(NO_KERNEL_HELP)

    def _use_prebuilt(self, ext, include_dir, lib_dir):
        print(f"linking against prebuilt libbitcoinkernel in {lib_dir}", flush=True)
        ext.include_dirs.append(include_dir)
        ext.library_dirs.append(lib_dir)
        ext.runtime_library_dirs.append(lib_dir)

    def _use_build_dir(self, ext, build_dir):
        """Link against a kernel cmake build dir. The header comes from
        the source tree the build was configured from (recorded in its
        CMakeCache.txt), so the pair cannot mismatch."""
        source_dir = cmake_source_dir(build_dir)
        if source_dir is None:
            raise RuntimeError(
                f"{build_dir} has a kernel library but no readable "
                f"CMakeCache.txt; rebuild it (see README) or remove it"
            )
        header = source_dir / "src" / "kernel" / "bitcoinkernel.h"
        if not header.exists():
            raise RuntimeError(
                f"{build_dir} was configured from {source_dir}, but "
                f"{header} does not exist; rebuild vendor/build (see README)"
            )
        self._use_prebuilt(ext, str(header.parent), str(build_dir / "lib"))

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
        # A cache generated for a different source tree makes cmake bail
        # out; start over in that case.
        cached_source = cmake_source_dir(build_dir)
        if cached_source is not None and cached_source != source_dir:
            print(f"discarding stale kernel build cache in {build_dir}", flush=True)
            shutil.rmtree(build_dir)
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

        libs = find_kernel_libs(build_dir / "lib")
        if not libs:
            raise RuntimeError(f"kernel build produced no library in {build_dir / 'lib'}")

        ext.include_dirs.append(str(header.parent))
        ext.library_dirs.append(str(build_dir / "lib"))
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

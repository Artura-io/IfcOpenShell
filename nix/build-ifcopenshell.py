#!/usr/bin/python
###############################################################################
#                                                                             #
# This file is part of IfcOpenShell.                                          #
#                                                                             #
# IfcOpenShell is free software: you can redistribute it and/or modify        #
# it under the terms of the Lesser GNU General Public License as published by #
# the Free Software Foundation, either version 3.0 of the License, or         #
# (at your option) any later version.                                         #
#                                                                             #
# IfcOpenShell is distributed in the hope that it will be useful,             #
# but WITHOUT ANY WARRANTY; without even the implied warranty of              #
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the                #
# Lesser GNU General Public License for more details.                         #
#                                                                             #
# You should have received a copy of the Lesser GNU General Public License    #
# along with this program. If not, see <http://www.gnu.org/licenses/>.        #
#                                                                             #
###############################################################################

import logging
import requests
import os
import re
import sys
import glob
import subprocess as sp
import shutil
import tarfile
import multiprocessing
import platform
import threading
import sysconfig
from datetime import datetime
import functools

import time
from urllib.request import urlretrieve
from collections.abc import Generator, Sequence
from pathlib import Path


from typing import Union, Literal

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
logger.addHandler(ch)

PROJECT_NAME = "IfcOpenShell"
USE_CURRENT_PYTHON_VERSION = True


JSON_VERSION = "3.11.3"
OCE_VERSION = "0.18.3"
OCCT_VERSION = "7.8.1"
BOOST_VERSION = "1.86.0"
EIGEN_VERSION = "3.4.0"
PCRE_VERSION = "8.41"
PCRE2_VERSION = "10.32"
LIBXML2_VERSION = "2.13.8"
SWIG_VERSION = "4.1.0"
OPENCOLLADA_VERSION = "v1.6.68"
HDF5_VERSION = "1.13.1"

GMP_VERSION = "6.3.0"
MPFR_VERSION = "3.1.6"  # latest is 4.1.0
CGAL_VERSION = "v5.6.3"
USD_VERSION = "23.05"
TBB_VERSION = "2021.9.0"
ROCKSDB_VERSION = "9.11.2"
ZSTD_VERSION = "1.5.7"
# binaries
cp = "cp"
bash = "bash"
git = "git"
bunzip2 = "bunzip2"
tar = "tar"
cc = "cc"
cplusplus = "c++"
autoconf = "autoconf"
automake = "automake"
make = "ninja"
date = "date"
curl = "curl"
wget = "wget"
strip = "strip"
xz = "xz"  # Used implicitly for `tar -xf *.tar.xz`.
brew = "brew"

explicit_targets = ["IfcOpenShell-Python", "IfcConvert"]

# Helper function for coloured printing

NO_COLOR = "\033[0m"  # <ref>http://stackoverflow.com/questions/5947742/how-to-change-the-output-color-of-echo-in-linux</ref>
BLACK_ON_WHITE = "\033[0;30;107m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"


def cecho(message, color=NO_COLOR):
    """Logs message `message` in color `color`."""
    logger.info(f"{color}{message}\033[0m")


def which(cmd: str) -> Union[str, None]:
    PATH = os.getenv("PATH")
    assert PATH
    for path in PATH.split(":"):
        if os.path.exists(path) and cmd in os.listdir(path):
            return cmd
    return None


# Set defaults for missing empty environment variables

USE_OCCT = True

IFCOS_NUM_BUILD_PROCS = 4

CMAKE_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "cmake"))

build_dir = os.path.join(os.path.dirname(__file__), "..", "build")


arch = platform.machine()
DEPS_DIR = Path(build_dir) / platform.system() / arch

if not os.path.exists(DEPS_DIR):
    os.makedirs(DEPS_DIR)

BUILD_CFG = "Release"


# Print build configuration information

cecho(
    f"""This script fetches and builds {PROJECT_NAME} and its dependencies
""",
    BLACK_ON_WHITE,
)
cecho(
    """Script configuration:

""",
    GREEN,
)
cecho(f"""* USE_OCCT               = {USE_OCCT}""", MAGENTA)
if USE_OCCT:
    cecho(" - Compiling against official Open Cascade")
else:
    cecho(" - Compiling against Open Cascade Community Edition")
cecho(f"* Dependency Directory   = {DEPS_DIR}", MAGENTA)
cecho(f" - The directory where {PROJECT_NAME} dependencies are installed.")
cecho(f"* Build Config Type      = {BUILD_CFG}", MAGENTA)
cecho(
    """ - The used build configuration type for the dependencies.
   Defaults to RelWithDebInfo if not specified."""
)

cecho(f"* IFCOS_NUM_BUILD_PROCS  = {IFCOS_NUM_BUILD_PROCS}", MAGENTA)
cecho(
    """ - How many compiler processes may be run in parallel.
"""
)

dependency_tree: "dict[str, tuple[str, ...]]" = {
    "IfcParse": ("boost", "libxml2", "hdf5", "rocksdb"),
    "IfcGeom": ("IfcParse", "occ", "json", "cgal", "eigen", "OpenCOLLADA"),
    "IfcConvert": ("IfcGeom",),
    "OpenCOLLADA": ("libxml2", "pcre"),
    "IfcGeomServer": ("IfcGeom",),
    "IfcOpenShell-Python": ("python", "swig", "IfcGeom"),
    "swig": ("pcre2",),
    "boost": (),
    "libxml2": (),
    "python": (),
    "occ": ("freetype",),
    "pcre": (),
    "pcre2": (),
    "json": (),
    "hdf5": (),
    "cgal": (),
    "eigen": (),
    "freetype": (),
    "rocksdb": ("zstd",),
    "zstd": (),
    # 'usd': ('boost', 'oneTBB')
}


def gather_dependencies(dep: str) -> "Generator[str]":
    yield dep
    for d in dependency_tree[dep]:
        for x in gather_dependencies(d):
            yield x


OFF_ON = ["OFF", "ON"]
BUILD_STATIC = True
ENABLE_FLAG = "--enable-static"
DISABLE_FLAG = "--disable-shared"
LINK_TYPE = "static"
LINK_TYPE_UCFIRST = LINK_TYPE.capitalize()
LIBRARY_EXT = "a"
PIC = "-fPIC"

logger.info(OCCT_VERSION)

targets = {dep for target in explicit_targets for dep in gather_dependencies(target)}


# Check that required tools are in PATH
yacc = "yacc"  # Used during swig building process, installed with `bison` on Debian / `byacc` on Red Hat.
missing_commands: "list[str]" = []
required_commands = [
    git,
    bunzip2,
    tar,
    cc,
    cplusplus,
    autoconf,
    automake,
    make,
    "patch",
    "cmake",
    #    yacc,
    xz,
]
for cmd in required_commands:
    if which(cmd) is None:
        missing_commands.append(cmd)

if missing_commands:
    raise ValueError(
        f"Required tools not installed or not added to PATH: {', '.join(missing_commands)}"
    )


# identifiers for the download tool (could be less memory consuming as ints, but are more verbose as strings)
download_tool_default = download_tool_py = "py"
download_tool_git = "git"

# Create log directory and file

log_dir = os.path.join(DEPS_DIR, "logs")
if not os.path.exists(log_dir):
    os.makedirs(log_dir)
LOG_FILE = (
    os.path.join(log_dir, sp.check_output([date, "+%Y%m%d"], encoding="utf-8").strip())
    + ".log"
)
if not os.path.exists(LOG_FILE):
    open(LOG_FILE, "w").close()
logger.info(f"using command log file '{LOG_FILE}'")


def run(
    cmds: "Sequence[str]", cwd: "Union[str, None]" = None, can_fail: bool = False
) -> str:
    """
    Wraps `subprocess.Popen.communicate()` and logs the command being executed,
    sets up logging `stderr` to `LOG_FILE` (in append mode) and returns stdout
    with leading and trailing whitespace removed.
    """

    def timestamp() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[
            :-3
        ]  # same format as logging

    def stream_reader(pipe, collector: "list[str]", log_file) -> None:
        for line in iter(pipe.readline, ""):
            log_file.write(f"{timestamp()} {line}")
            log_file.flush()
            collector.append(line)
        pipe.close()

    logger.debug(f"running command {' '.join(cmds)} in directory {cwd}")
    stdout: list[str] = []
    stderr: list[str] = []

    # Ensure both live logs available in the log file
    # and the putput.
    with open(LOG_FILE, "a", encoding="utf-8") as log_file_handle:
        proc = sp.Popen(cmds, cwd=cwd, stdout=sp.PIPE, stderr=sp.PIPE, encoding="utf-8")
        assert proc.stdout and proc.stderr

        t_out = threading.Thread(
            target=stream_reader, args=(proc.stdout, stdout, log_file_handle)
        )
        t_err = threading.Thread(
            target=stream_reader, args=(proc.stderr, stderr, log_file_handle)
        )
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        proc.wait()

    logger.debug(f"command returned {proc.returncode}")

    if proc.returncode != 0 and not can_fail:
        print("-" * 70)
        print("".join(stderr))
        print("-" * 70)
        raise RuntimeError(
            f"Command `{' '.join(cmds)}` returned exit code {proc.returncode}"
        )

    return "".join(stdout).strip()


BOOST_VERSION_UNDERSCORE = BOOST_VERSION.replace(".", "_")

BOOST_LOCATION = (
    f"https://github.com/boostorg/boost/releases/download/boost-{BOOST_VERSION}/"
)

# Helper functions


def run_autoconf(arg1: str, configure_args: "list[str]", cwd: str) -> None:
    configure_path = os.path.realpath(os.path.join(cwd, "..", "configure"))
    if not os.path.exists(configure_path):
        run(
            [bash, "./autogen.sh"], cwd=os.path.realpath(os.path.join(cwd, ".."))
        )  # only run autogen.sh in the directory it is located and use cwd to achieve that in order to not mess up things
    # Using `sh` over `bash` fixes issues with building swig
    prefix = os.path.realpath(f"{DEPS_DIR}/install/{arg1}")

    run(
        [
            "/bin/sh",
            "../configure",
            *configure_args,
            f"--prefix={prefix}",
        ],
        cwd=cwd,
    )


def run_cmake(
    arg1,
    cmake_args: "list[str]",
    cmake_dir: Union[str, None] = None,
    cwd: Union[str, None] = None,
):
    if cmake_dir is None:
        P = ".."
    else:
        P = cmake_dir

    run(
        [
            "cmake",
            P,
            *cmake_args,
            "-G",
            "Ninja",
            f"-DCMAKE_BUILD_TYPE={BUILD_CFG}",
            f"-DBUILD_SHARED_LIBS={OFF_ON[not BUILD_STATIC]}",
            f"-DCMAKE_CXX_FLAGS='{os.environ['CXXFLAGS']}'",
            f"-DCMAKE_C_FLAGS='{os.environ['CFLAGS']}'",
            f"-DCMAKE_SHARED_LINKER_FLAGS={os.environ['LDFLAGS']}",
        ],
        cwd=cwd,
    )


def git_clone_or_pull_repository(
    clone_url: str, target_dir: str, revision: Union[str, None] = None
) -> None:
    """Lazily clones the `git` repository denoted by `clone_url` into
    the `target_dir` or pulls latest changes if the `target_dir` exists (naively assumes
    that a working clone exists there) and optionally checks out a revision
    `revision` after cloning or in the existing clone if `revision` is not
    `None`."""
    if not os.path.exists(target_dir):
        logger.info(f"cloning '{clone_url}' into '{target_dir}'")
        run([git, "clone", "--recursive", clone_url, target_dir])
    else:
        logger.info(f"directory '{target_dir}' already cloned. Pulling latest changes.")
        run([git, "-C", target_dir, "fetch", "--all", "--tags", "--force"])

    # detect whether we are on a branch and pull
    if run([git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=target_dir) != "HEAD":
        run([git, "pull", clone_url], cwd=target_dir)

    if revision != None:
        run([git, "reset", "--hard"], cwd=target_dir)
        run([git, "fetch", "--all"], cwd=target_dir)
        run([git, "checkout", revision], cwd=target_dir)


def build_dependency(
    name: str,
    mode: Literal[
        "cmake",
        "autoconf",
        "ctest",
        "bjam",
    ],
    build_tool_args: "list[str]",
    download_url: str,
    download_name: str,
    download_tool: Literal["py", "git", "local"] = download_tool_default,
    revision: "Union[str, None]" = None,
    patch: "Union[str, list[str], None]" = None,
    shell=None,
    pre_compile_subs: "Sequence[tuple[str, str, str]]" = (),
    additional_files: "Union[dict[str, str], None]" = None,
    no_append_name=False,
    cmake_dir=None,
    **kwargs,
) -> None:
    """Handles building of dependencies with different tools (which are
    distinguished with the `mode` argument. `build_tool_args` is expected to be
    a list which is necessary in order to not mess up quoting of compiler and
    linker flags.

    :param pre_compile_subs: A sequence of ``(fn, before, after)``
    :param additional_files: Mapping path->url.
    :param kwargs: Additional ``mode`` related kwargs.
    """
    check_dir = os.path.join(DEPS_DIR, "install", name)
    if os.path.exists(check_dir):
        logger.info(f"Found existing {name}, skipping")
        return
    build_dir = os.path.join(DEPS_DIR, "build")
    if not os.path.exists(build_dir):
        os.makedirs(build_dir)

    logger.info(f"\rFetching {name}...   ")

    if download_tool == download_tool_py:
        if no_append_name:
            url = download_url
        else:
            url = os.path.join(download_url, download_name)

        download_path = os.path.join(build_dir, download_name)
        if not os.path.exists(download_path):
            for _ in range(3):
                try:
                    with requests.get(url, stream=True) as r:
                        r.raw.read = functools.partial(r.raw.read, decode_content=True)
                        with open(os.path.join(build_dir, download_path), "wb") as f:
                            shutil.copyfileobj(r.raw, f)
                    break
                except ConnectionError as e:
                    print(e, "... retrying...")
                    time.sleep(30.0)
                    continue
        else:
            logger.info(
                f"Download '{download_path}' already exists, assuming it's an undamaged download and that it has been extracted if possible, skipping"
            )
    elif download_tool == download_tool_git:
        logger.info(f"\rChecking {name}...   ")
        git_clone_or_pull_repository(
            download_url,
            target_dir=os.path.join(build_dir, download_name),
            revision=revision,
        )
    elif download_tool == "local":
        logger.info(f"Copying local file {download_name}")
        shutil.copyfile(os.path.join(download_url, download_name), os.path.join(build_dir, download_name))
    else:
        raise ValueError(f"download tool '{download_tool}' is not supported")
    download_dir = os.path.join(build_dir, download_name)

    if os.path.isdir(download_dir):
        extract_dir_name = download_name
        extract_dir = os.path.join(build_dir, extract_dir_name)
    else:
        download_tarfile_path = os.path.join(build_dir, download_name)
        if download_name.endswith(".tar.gz") or download_name.endswith(".tgz"):
            compr = "gz"
        elif download_name.endswith(".tar.bz2"):
            compr = "bz2"
        elif download_name.endswith(".tar.xz"):
            compr = "xz"
        else:
            raise RuntimeError("fix source for new download type")
        download_tarfile = tarfile.open(name=download_tarfile_path, mode=f"r:{compr}")
        # tarfile seriously doesn't have a function to retrieve the root directory more easily
        extract_dir_name = os.path.commonprefix(
            [x for x in download_tarfile.getnames() if x != "."]
        )
        # run([tar, "--exclude=\"*/*\"", "-tf", download_name], cwd=build_dir).strip() no longer works
        if extract_dir_name is None:
            extract_dir_name = run(
                [
                    bash,
                    "-c",
                    f"tar -tf {download_name} 2> /dev/null | head -n 1 | cut -f1 -d /",
                ],
                cwd=build_dir,
            )
        extract_dir = os.path.join(build_dir, extract_dir_name)
        if not os.path.exists(extract_dir):
            run([tar, "-xf", download_name], cwd=build_dir)

    if additional_files:
        for path, url in additional_files.items():
            if not os.path.exists(path):
                urlretrieve(url, os.path.join(extract_dir, path))

    if patch is not None:
        if isinstance(patch, str):
            patch = [patch]
        for p in patch:
            patch_abs = os.path.abspath(os.path.join(os.path.dirname(__file__), p))
            if os.path.exists(patch_abs):
                try:
                    run(
                        ["patch", "-p1", "--batch", "--forward", "-i", patch_abs],
                        cwd=extract_dir,
                    )
                except Exception as e:
                    # Assert that the patch has already been applied
                    run(
                        [
                            "patch",
                            "-p1",
                            "--batch",
                            "--reverse",
                            "--dry-run",
                            "-i",
                            patch_abs,
                        ],
                        cwd=extract_dir,
                    )

    if shell is not None:
        sp.run(shell, shell=True, check=True, cwd=extract_dir)

    if mode == "ctest":
        try:
            run(
                [
                    "ctest",
                    "-S",
                    "HDF5config.cmake,BUILD_GENERATOR=Unix",
                    "-C",
                    BUILD_CFG,
                    "-V",
                    "-O",
                    "hdf5.log",
                ],
                cwd=extract_dir,
            )
        except Exception as e:
            print("-" * 70)
            print(open(os.path.join(extract_dir, "hdf5.log")))
            print("-" * 70)
            raise e
        run(
            [tar, "-xf", kwargs["ctest_result"] + ".tar.gz"],
            cwd=os.path.join(extract_dir, "build"),
        )
        shutil.copytree(
            os.path.join(
                extract_dir,
                "build",
                kwargs["ctest_result"],
                kwargs["ctest_result_path"],
            ),
            os.path.join(DEPS_DIR, "install", name),
        )
    elif mode != "bjam":
        extract_build_dir = os.path.join(
            extract_dir, *([cmake_dir] if cmake_dir else []), "build"
        )
        if os.path.exists(extract_build_dir):
            shutil.rmtree(extract_build_dir)
        os.makedirs(extract_build_dir)

        logger.info(f"\rConfiguring {name}...")
        if mode == "autoconf":
            run_autoconf(name, build_tool_args, cwd=extract_build_dir)
        elif mode == "cmake":
            run_cmake(name, build_tool_args, cwd=extract_build_dir)
        else:
            raise ValueError()
        for fn, before, after in pre_compile_subs:
            with open(os.path.join(extract_dir, fn), "r") as f:
                s = f.read()
            s = s.replace(before, after)
            with open(os.path.join(extract_dir, fn), "w") as f:
                f.write(s)
        logger.info(f"\rBuilding {name}...   ")
        if mode == "autoconf":
            run(
                ["make", f"-j{IFCOS_NUM_BUILD_PROCS}", "VERBOSE=1"],
                cwd=extract_build_dir,
            )
        else:
            run([make], cwd=extract_build_dir)
        logger.info(f"\rInstalling {name}... ")
        if mode == "autoconf":
            run(["make", "install"], cwd=extract_build_dir)
        else:
            run([make, "install"], cwd=extract_build_dir)
        logger.info(f"\rInstalled {name}     \n")
    else:  # bjam
        logger.info(f"\rConfiguring {name}...")
        run([bash, "./bootstrap.sh"], cwd=extract_dir)
        logger.info(f"\rBuilding {name}...   ")
        run(
            ["./b2", f"-j{IFCOS_NUM_BUILD_PROCS}"] + build_tool_args,
            cwd=extract_dir,
            can_fail=False,
        )
        logger.info(f"\rInstalling {name}... ")
        shutil.copytree(
            os.path.join(extract_dir, "boost"),
            os.path.join(DEPS_DIR, "install", f"boost-{BOOST_VERSION}", "boost"),
        )
        logger.info(f"\rInstalled {name}     \n")


cecho("Collecting dependencies:", GREEN)

# Set compiler flags for 32bit builds on 64bit system
# TODO: This is untested

ADDITIONAL_ARGS = []

if platform.system() == "Darwin":
    ADDITIONAL_ARGS = [f"-mmacosx-version-min={TOOLSET}"] + ADDITIONAL_ARGS

# If the linker supports GC sections, set it up to reduce binary file size
# -fPIC is required for the shared libraries to work

compiler_flags = "CFLAGS", "CXXFLAGS", "LDFLAGS"

CXXFLAGS = os.environ.get("CXXFLAGS", "")
CFLAGS = os.environ.get("CFLAGS", "")
LDFLAGS = os.environ.get("LDFLAGS", "")

ADDITIONAL_ARGS_STR = " ".join(ADDITIONAL_ARGS)

if (
    sp.call([bash, "-c", "ld --gc-sections 2>&1 | grep -- --gc-sections &> /dev/null"])
    != 0
):
    CXXFLAGS_MINIMAL = f"{CXXFLAGS} {PIC} {ADDITIONAL_ARGS_STR}"
    CFLAGS_MINIMAL = f"{CFLAGS} {PIC} {ADDITIONAL_ARGS_STR}"
    if BUILD_STATIC:
        CXXFLAGS = f"{CXXFLAGS} {PIC} -fdata-sections -ffunction-sections -fvisibility=hidden -fvisibility-inlines-hidden {ADDITIONAL_ARGS_STR}"
        CFLAGS = f"{CFLAGS}   {PIC} -fdata-sections -ffunction-sections -fvisibility=hidden {ADDITIONAL_ARGS_STR}"
    else:
        CXXFLAGS = CXXFLAGS_MINIMAL
        CFLAGS = CFLAGS_MINIMAL
    LDFLAGS = f"{LDFLAGS}  -Wl,--gc-sections {ADDITIONAL_ARGS_STR}"
else:
    CXXFLAGS_MINIMAL = f"{CXXFLAGS} {PIC} {ADDITIONAL_ARGS_STR}"
    CFLAGS_MINIMAL = f"{CFLAGS}   {PIC} {ADDITIONAL_ARGS_STR}"
    if BUILD_STATIC:
        CXXFLAGS = f"{CXXFLAGS} {PIC} -fvisibility=hidden -fvisibility-inlines-hidden {ADDITIONAL_ARGS_STR}"
        CFLAGS = f"{CFLAGS}   {PIC} -fvisibility=hidden -fvisibility-inlines-hidden {ADDITIONAL_ARGS_STR}"
    else:
        CXXFLAGS = CXXFLAGS_MINIMAL
        CFLAGS = CFLAGS_MINIMAL
    LDFLAGS = f"{LDFLAGS} {ADDITIONAL_ARGS_STR}"

os.environ["CXXFLAGS"] = CXXFLAGS
os.environ["CPPFLAGS"] = CXXFLAGS
os.environ["CFLAGS"] = CFLAGS
os.environ["LDFLAGS"] = LDFLAGS

# Some dependencies need a more recent CMake version than most distros provide
# @tfk: this is no longer needed
# build_dependency(name="cmake-%s" % (CMAKE_VERSION,), mode="autoconf", build_tool_args=[], download_url="https://cmake.org/files/v%s" % (CMAKE_VERSION_2,), download_name="cmake-%s.tar.gz" % (CMAKE_VERSION,))

if "hdf5" in targets:
    # not supported
    orig = [os.environ[f] for f in compiler_flags]
    for f in compiler_flags:
        os.environ[f] = re.sub(r"-flto(=\w+)?", "", os.environ[f])

    HDF5_UNDERSCORE = "_".join(HDF5_VERSION.split("."))
    HDF5_MAJOR = ".".join(HDF5_VERSION.split(".")[:-1])
    dependency_name = f"hdf5-{HDF5_VERSION}"
    build_dependency(
        name=dependency_name,
        mode="cmake",
        build_tool_args=[
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/{dependency_name}",
            "-DHDF5_ENABLE_Z_LIB_SUPPORT=OFF",
            "-DBUILD_TESTING=OFF",
            "-DHDF5_BUILD_TOOLS=OFF",
            "-DHDF5_BUILD_EXAMPLES=OFF",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DHDF5_BUILD_UTILS=OFF",
            "-DHDF5_BUILD_CPP_LIB=ON",
        ],
        download_url=f"https://github.com/HDFGroup/hdf5/archive/refs/tags/",
        download_name=f"hdf5-{HDF5_UNDERSCORE}.tar.gz",
    )

    for f, o in zip(compiler_flags, orig):
        os.environ[f] = o


if "json" in targets:
    dependency_name = f"json-{JSON_VERSION}"
    build_dependency(
        name=dependency_name,
        mode="cmake",
        build_tool_args=[
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/{dependency_name}",
            "-DJSON_BuildTests=OFF",
        ],
        download_url=f"https://github.com/nlohmann/json/releases/download/v{JSON_VERSION}",
        download_name="json.tar.xz",
    )

if "eigen" in targets:
    dependency_name = f"eigen-install-{EIGEN_VERSION}"
    build_dependency(
        name=f"{dependency_name}",
        mode="cmake",
        # We add '-install-' in the middle, so it won't be confused with git repo we used previously.
        build_tool_args=[
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/{dependency_name}",
        ],
        download_url=f"https://gitlab.com/libeigen/eigen/-/archive/{EIGEN_VERSION}/",
        download_name=f"eigen-{EIGEN_VERSION}.tar.gz",
    )

if "pcre" in targets:
    # Keep it autoconf as OpenCOLLADA is pretty old and might break
    # if we update it's dependencies for mmore modern cmake.
    build_dependency(
        name=f"pcre-{PCRE_VERSION}",
        mode="autoconf",
        build_tool_args=[DISABLE_FLAG],
        download_url=f"https://downloads.sourceforge.net/project/pcre/pcre/{PCRE_VERSION}/",
        download_name=f"pcre-{PCRE_VERSION}.tar.bz2",
    )

if "pcre2" in targets:
    build_dependency(
        name=f"pcre2-{PCRE2_VERSION}",
        mode="autoconf",
        build_tool_args=[DISABLE_FLAG],
        download_url=f"https://downloads.sourceforge.net/project/pcre/pcre2/{PCRE2_VERSION}/",
        download_name=f"pcre2-{PCRE2_VERSION}.tar.bz2",
    )

# An issue exists with swig-1.3 and python >= 3.2
# Therefore, build a recent copy from source
if "swig" in targets:
    build_dependency(
        name="swig",
        mode="autoconf",
        build_tool_args=[
            "--disable-ccache",
            f"--with-pcre2-prefix={DEPS_DIR}/install/pcre2-{PCRE2_VERSION}",
        ],
        download_url="https://github.com/swig/swig.git",
        download_name="swig",
        download_tool=download_tool_git,
        revision=f"v{SWIG_VERSION}",
    )

if "freetype" in targets:
    build_dependency(
        name=f"freetype",
        mode="cmake",
        build_tool_args=[f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/freetype"],
        download_url="https://github.com/freetype/freetype",
        download_name="freetype2",
        download_tool=download_tool_git,
        revision="VER-2-11-1",
    )

if USE_OCCT and "occ" in targets:
    patches = []
    if OCCT_VERSION < "7.4":
        patches.append("./patches/occt/enable-exception-handling.patch")

    if OCCT_VERSION == "7.7.1":
        patches.append("./patches/occt/no_ExpToCasExe.patch")

    if OCCT_VERSION == "7.7.2":
        patches.append("./patches/occt/no_ExpToCasExe_7_7_2.patch")

    if OCCT_VERSION == "7.8.1":
        patches.append("./patches/occt/no_ExpToCasExe_7_8_1.patch")

    if OCCT_VERSION == "7.9.1":
        patches.append("./patches/occt/no_ExpToCasExe_7_9_1.patch")

    build_dependency(
        name=f"occt-{OCCT_VERSION}",
        mode="cmake",
        build_tool_args=[
            f"-DINSTALL_DIR={DEPS_DIR}/install/occt-{OCCT_VERSION}",
            f"-DBUILD_LIBRARY_TYPE={LINK_TYPE_UCFIRST}",
            f"-DBUILD_MODULE_Draw=0",
            f"-DBUILD_RELEASE_DISABLE_EXCEPTIONS=Off",
            # Disable xlib explicitly, as it tries to use it on Desktop Ubuntu, adding unnecessary dependency.
            f"-DUSE_XLIB=OFF",
            # Avoid building 3D Viewer.
            f"-DUSE_FREETYPE=OFF",
            f"-DUSE_OPENGL=OFF",
            f"-DUSE_GLES2=OFF",
            f"-D3RDPARTY_FREETYPE_DIR={DEPS_DIR}/install/freetype",
            f"-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ],
        download_url="https://github.com/Open-Cascade-SAS/OCCT",
        download_name="occt",
        download_tool=download_tool_git,
        patch=patches,
        revision="V" + OCCT_VERSION.replace(".", "_"),
    )


if "libxml2" in targets:
    build_tool_args = [
        "--without-python",
        ENABLE_FLAG,
        DISABLE_FLAG,
        "--without-zlib",
        "--without-iconv",
        "--without-lzma",
    ]
    build_dependency(
        f"libxml2-{LIBXML2_VERSION}",
        "autoconf",
        build_tool_args=build_tool_args,
        download_url=f"https://download.gnome.org/sources/libxml2/{'.'.join(LIBXML2_VERSION.split('.')[0:2])}/",
        download_name=f"libxml2-{LIBXML2_VERSION}.tar.xz",
    )

if "OpenCOLLADA" in targets:
    patches = ["./patches/opencollada/pr622_and_disable_subdirs.patch"]

    # This patch allows static libraries config on Unix,
    # because the config is weird and doesn't allow non-shared libraries on Unix.
    patches.append("./patches/opencollada/allow_static_libraries_config_on_unix.patch")

    build_dependency(
        "OpenCOLLADA",
        "cmake",
        build_tool_args=[
            f"-DLIBXML2_INCLUDE_DIR={DEPS_DIR}/install/libxml2-{LIBXML2_VERSION}/include/libxml2",
            f"-DLIBXML2_LIBRARIES={DEPS_DIR}/install/libxml2-{LIBXML2_VERSION}/lib/libxml2.{LIBRARY_EXT}",
            f"-DPCRE_INCLUDE_DIR={DEPS_DIR}/install/pcre-{PCRE_VERSION}/include",
            f"-DPCRE_PCREPOSIX_LIBRARY={DEPS_DIR}/install/pcre-{PCRE_VERSION}/lib/libpcreposix.{LIBRARY_EXT}",
            f"-DPCRE_PCRE_LIBRARY={DEPS_DIR}/install/pcre-{PCRE_VERSION}/lib/libpcre.{LIBRARY_EXT}",
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/OpenCOLLADA/",
            # OpenCOLLADA is ancient at this point and allows cmake 2.6+, which results in error in cmake 4.
            f"-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ],
        download_url="https://github.com/KhronosGroup/OpenCOLLADA.git",
        download_name="OpenCOLLADA",
        download_tool=download_tool_git,
        patch=patches,
        revision=OPENCOLLADA_VERSION,
    )

if "boost" in targets:
    str_concat = (
        lambda prefix: lambda postfix: ""
        if postfix.strip() == ""
        else "=".join((prefix, postfix.strip()))
    )
    toolset = []

    build_dependency(
        f"boost-{BOOST_VERSION}",
        mode="bjam",
        build_tool_args=[
            f"--stagedir={DEPS_DIR}/install/boost-{BOOST_VERSION}",
            "--with-system",
            "--with-program_options",
            "--with-regex",
            "--with-thread",
            "--with-date_time",
            "--with-iostreams",
            "--with-filesystem",
            f"link={LINK_TYPE}",
            *toolset,
            *map(str_concat("cxxflags"), CXXFLAGS.strip().split(" ")),
            *map(str_concat("linkflags"), LDFLAGS.strip().split(" ")),
            "stage",
            "-s",
            "NO_BZIP2=1",
        ],
        download_url=BOOST_LOCATION,
        # don't remember what this is, but fail on 1.86
        # patch="./patches/boost/boostorg_regex_62.patch",
        download_name=f"boost-{BOOST_VERSION}-b2-nodocs.tar.gz",
    )


if "cgal" in targets:
    gmp_args: "list[str]" = []
    mpfr_args: "list[str]" = []

    OLD_CC = None

    build_dependency(
        name=f"gmp-{GMP_VERSION}",
        mode="autoconf",
        build_tool_args=[ENABLE_FLAG, DISABLE_FLAG, "--with-pic", *gmp_args],
        download_tool="local",
        download_url=f"{os.path.dirname(os.path.abspath(__file__))}/../provision/src/gmp/",
        download_name=f"gmp-{GMP_VERSION}.tar.bz2",
    )

    build_dependency(
        name=f"mpfr-{MPFR_VERSION}",
        mode="autoconf",
        build_tool_args=[
            ENABLE_FLAG,
            DISABLE_FLAG,
            *mpfr_args,
            f"--with-gmp={DEPS_DIR}/install/gmp-{GMP_VERSION}",
        ],
        download_url=f"http://www.mpfr.org/mpfr-{MPFR_VERSION}/",
        download_name=f"mpfr-{MPFR_VERSION}.tar.bz2",
    )

    build_dependency(
        name=f"cgal-{CGAL_VERSION}",
        mode="cmake",
        build_tool_args=[
            f"-DGMP_LIBRARIES={DEPS_DIR}/install/gmp-{GMP_VERSION}/lib/libgmp.{LIBRARY_EXT}",
            f"-DGMP_INCLUDE_DIR={DEPS_DIR}/install/gmp-{GMP_VERSION}/include",
            f"-DMPFR_LIBRARIES={DEPS_DIR}/install/mpfr-{MPFR_VERSION}/lib/libmpfr.{LIBRARY_EXT}",
            f"-DMPFR_INCLUDE_DIR={DEPS_DIR}/install/mpfr-{MPFR_VERSION}/include",
            f"-DBoost_INCLUDE_DIR={DEPS_DIR}/install/boost-{BOOST_VERSION}",
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/cgal-{CGAL_VERSION}/",
            f"-DCGAL_HEADER_ONLY=On",
            f"-DBUILD_SHARED_LIBS=Off",
        ],
        download_url="https://github.com/CGAL/cgal.git",
        download_name="cgal",
        download_tool=download_tool_git,
        revision=CGAL_VERSION,
    )

if "zstd" in targets:
    build_dependency(
        name=f"zstd-{ZSTD_VERSION}",
        mode="cmake",
        build_tool_args=[
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/zstd-{ZSTD_VERSION}",
            f"-DZSTD_BUILD_STATIC=ON",
            f"-DZSTD_BUILD_SHARED=OFF",
            f"-DCMAKE_INSTALL_LIBDIR=lib",
        ],
        cmake_dir="build/cmake/",
        download_url="https://github.com/facebook/zstd",
        download_name="zstd",
        download_tool=download_tool_git,
        revision=f"v{ZSTD_VERSION}",
    )

if "rocksdb" in targets:
    build_dependency(
        name=f"rocksdb-{ROCKSDB_VERSION}",
        mode="cmake",
        build_tool_args=[
            f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/rocksdb-{ROCKSDB_VERSION}",
            f"-DFAIL_ON_WARNINGS=Off",
            f"-DWITH_TESTS=OFF",
            f"-DWITH_TOOLS=OFF",
            f"-DWITH_GFLAGS=OFF",
            f"-DWITH_BENCHMARK_TOOLS=OFF",
            f"-DWITH_CORE_TOOLS=OFF",
            f"-DROCKSDB_BUILD_SHARED=Off",
            f"-DCMAKE_POSITION_INDEPENDENT_CODE=On",
            f"-DUSE_RTTI=On",
            f"-DWITH_ZSTD=On",
            f"-DPORTABLE=1",
            f"-DCMAKE_PREFIX_PATH={DEPS_DIR}/install/zstd-{ZSTD_VERSION}",
        ],
        download_url="https://github.com/facebook/rocksdb",
        download_name="rocksdb",
        download_tool=download_tool_git,
        revision=f"v{ROCKSDB_VERSION}",
    )

cecho("Building IfcOpenShell:", GREEN)

IFCOS_DIR = os.path.join(DEPS_DIR, "build", "ifcopenshell")
if os.environ.get("NO_CLEAN", "").lower() not in {"1", "on", "true"}:
    if os.path.exists(IFCOS_DIR):
        shutil.rmtree(IFCOS_DIR)
os.makedirs(IFCOS_DIR, exist_ok=True)
executables_dir = os.path.join(IFCOS_DIR, "executables")
os.makedirs(executables_dir, exist_ok=True)


cmake_args = [
    "-DCMAKE_CXX_STANDARD=17",
    "-DUSE_MMAP=ON",
    "-DBUILD_EXAMPLES=OFF",
    "-DBUILD_SHARED_LIBS=OFF",
    "-DGLTF_SUPPORT=ON",
    "-DBoost_NO_BOOST_CMAKE=On",
]
"""Default CMake args to use for all CMake configs."""
cmake_args_prefix_path: "list[str]" = [
    f"{DEPS_DIR}/install/boost-{BOOST_VERSION}",
    f"{DEPS_DIR}/install/eigen-install-{EIGEN_VERSION}",
    f"{DEPS_DIR}/install/json-{JSON_VERSION}",
]


def get_cmake_args_prefix_path(additional_paths: "Sequence[str]" = ()) -> "list[str]":
    args_prefix_path = cmake_args_prefix_path.copy()
    args_prefix_path.extend(additional_paths)
    prefix_path = ";".join(args_prefix_path)
    return [f"-DCMAKE_PREFIX_PATH={prefix_path}"]


schemas = os.environ.get("IFCOS_SCHEMAS")
if schemas:
    cmake_args.append(f"-DSCHEMA_VERSIONS={schemas}")

if "cgal" in targets:
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/cgal-{CGAL_VERSION}")
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/gmp-{GMP_VERSION}")
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/mpfr-{MPFR_VERSION}")


if "occ" in targets and USE_OCCT:
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/occt-{OCCT_VERSION}")


if "OpenCOLLADA" in targets:
    # pcre is a dependency of OpenCOLLADA, but since we `find_package`,
    # we don't need to add it explicitly here as cmake will find it from the config.
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/OpenCOLLADA")

if "libxml2" in targets:
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/libxml2-{LIBXML2_VERSION}")


if "hdf5" in targets:
    cmake_args_prefix_path.append(f"{DEPS_DIR}/install/hdf5-{HDF5_VERSION}")

if "rocksdb" in targets:
    cmake_args.extend(
        [
            f"-DWITH_ROCKSDB=On",
            f"-DWITH_ZSTD=On",
        ]
    )
    cmake_args_prefix_path.extend(
        [
            f"{DEPS_DIR}/install/rocksdb-{ROCKSDB_VERSION}",
            f"{DEPS_DIR}/install/zstd-{ZSTD_VERSION}",
        ]
    )

if "IfcOpenShell-Python" in targets:
    # On OSX the actual Python library is not linked against.
    ADDITIONAL_ARGS = ""
    if platform.system() == "Darwin":
        ADDITIONAL_ARGS = "-Wl,-flat_namespace,-undefined,suppress"

    # NOTE: We don't use `CXXFLAGS` for wrappers, so wrapper is compiled with different flags
    # (e.g. ` -fdata-sections` is missing, which is set by default for executables)
    # So cache doesn't match and running build-all.py builds most of ifcopenshell libraries twice.
    os.environ["CPPFLAGS"] = f"{CXXFLAGS_MINIMAL} {ADDITIONAL_ARGS}"
    os.environ["CXXFLAGS"] = f"{CXXFLAGS_MINIMAL} {ADDITIONAL_ARGS}"
    os.environ["CFLAGS"] = f"{CFLAGS_MINIMAL} {ADDITIONAL_ARGS}"
    os.environ["LDFLAGS"] = f"{LDFLAGS} {ADDITIONAL_ARGS}"

    python_dir = os.path.join(IFCOS_DIR, "pythonwrapper")
    os.makedirs(python_dir, exist_ok=True)

    def compile_python_wrapper(
        python_version: str,
        python_library: str,
        python_include: str,
        python_executable: Union[str, None],
    ) -> Union[str, None]:
        """
        :return: Path to module dir if ``python_executable`` was provided, otherwise ``None``.
        """
        logger.info(f"\rConfiguring python {python_version} wrapper...")

        # cache_path = os.path.join(python_dir, "CMakeCache.txt")
        # if os.path.exists(cache_path):
        #     os.remove(cache_path)

        os.environ["PYTHON_LIBRARY_BASENAME"] = os.path.basename(python_library)

        swig_prefix_paths = [f"{DEPS_DIR}/install/swig"]

        run_cmake(
            "",
            cmake_args
            + get_cmake_args_prefix_path(swig_prefix_paths)
            + [
                "-DPYTHON_LIBRARY=" + python_library,
                *(
                    [f"-DPYTHON_EXECUTABLE={python_executable}"]
                    if python_executable
                    else []
                ),
                # *([f"-DPYTHON_MODULE_INSTALL_DIR={os.environ['PYTHONPATH']}/ifcopenshell"] if "wasm" in flags else []),
                "-DPYTHON_INCLUDE_DIR=" + python_include,
                f"-DCMAKE_INSTALL_PREFIX={DEPS_DIR}/install/ifcopenshell/tmp",
                "-DUSERSPACE_PYTHON_PREFIX="
                + ["Off", "On"][
                    os.environ.get("PYTHON_USER_SITE", "").lower()
                    in {"1", "on", "true"}
                ],
            ],
            cmake_dir=CMAKE_DIR,
            cwd=python_dir,
        )

        logger.info(f"\rBuilding python {python_version} wrapper...   ")
        run(
            [make, f"-j{IFCOS_NUM_BUILD_PROCS}", "ifcopenshell_wrapper"], cwd=python_dir
        )
        run([make, "ifcwrap/install/local"], cwd=python_dir)

        if python_executable:
            run([python_executable, "-m", "ensurepip"])
            run(
                [
                    python_executable,
                    "-m",
                    "pip",
                    "install",
                    "--user",
                    "numpy",
                    "typing_extensions",
                ]
            )
            module_dir = run(
                [
                    python_executable,
                    "-c",
                    "import inspect, ifcopenshell; print(inspect.getfile(ifcopenshell))",
                ]
            )
            # Use just the last line is used,
            # because output might contain warning like `No stream support: No module named 'lark'`.
            module_dir = module_dir.strip().splitlines()[-1]
            module_dir = os.path.dirname(module_dir)

            if platform.system() != "Darwin":
                if BUILD_CFG == "Release":
                    # TODO: This symbol name depends on the Python version?
                    so = glob.glob(
                        os.path.join(module_dir, "_ifcopenshell_wrapper*.so")
                    )[0]

                    run(
                        [strip, "-s", "-K", "PyInit__ifcopenshell_wrapper", so],
                        cwd=module_dir,
                    )

            return module_dir

    python_info = sysconfig.get_paths()

    py_path_components = [
        sysconfig.get_config_var("LIBDIR"),
        sysconfig.get_config_var("INSTSONAME"),
    ]

    if sysconfig.get_config_var("multiarchsubdir"):
        py_path_components.insert(
            1, sysconfig.get_config_var("multiarchsubdir").replace("/", "")
        )

    python_lib = os.path.join(*py_path_components)

    module_dir = compile_python_wrapper(
        platform.python_version(),
        python_lib,
        python_info["include"],
        sys.executable,
    )

logger.info("\rBuilt IfcOpenShell...\n\n")

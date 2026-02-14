# SPDX-License-Identifier: LGPL-2.1-or-later

"""Repository rules for pkg-config based external dependencies."""

def _pkg_config_impl(repository_ctx):
    """Repository rule that queries pkg-config for a library."""
    pkg_name = repository_ctx.attr.pkg_name
    if not pkg_name:
        pkg_name = repository_ctx.name

    # Query pkg-config for cflags
    result = repository_ctx.execute(["pkg-config", "--cflags", pkg_name])
    if result.return_code != 0:
        if repository_ctx.attr.optional:
            # Create empty targets for optional dependencies
            repository_ctx.file("BUILD.bazel", """
# SPDX-License-Identifier: LGPL-2.1-or-later
# Optional dependency {pkg_name} not found
package(default_visibility = ["//visibility:public"])

cc_library(
    name = "{name}",
    # Empty library - dependency not available
)
""".format(pkg_name = pkg_name, name = repository_ctx.name))
            return
        else:
            fail("pkg-config failed for %s: %s" % (pkg_name, result.stderr))

    cflags = result.stdout.strip().split()
    includes = []
    defines = []
    copts = []

    for flag in cflags:
        if flag.startswith("-I"):
            includes.append(flag[2:])
        elif flag.startswith("-D"):
            defines.append(flag[2:])
        else:
            copts.append(flag)

    # Query pkg-config for libs
    result = repository_ctx.execute(["pkg-config", "--libs", pkg_name])
    if result.return_code != 0:
        fail("pkg-config --libs failed for %s: %s" % (pkg_name, result.stderr))

    libs = result.stdout.strip().split()
    linkopts = []
    lib_names = []

    for lib in libs:
        if lib.startswith("-l"):
            lib_names.append(lib[2:])
            linkopts.append(lib)
        elif lib.startswith("-L"):
            linkopts.append(lib)
        else:
            linkopts.append(lib)

    # Get version
    result = repository_ctx.execute(["pkg-config", "--modversion", pkg_name])
    version = result.stdout.strip() if result.return_code == 0 else "unknown"

    # Generate BUILD file
    build_content = """
# SPDX-License-Identifier: LGPL-2.1-or-later
# Auto-generated from pkg-config for {pkg_name} version {version}

package(default_visibility = ["//visibility:public"])

cc_library(
    name = "{name}",
    hdrs = glob(["include/**/*.h"]),
    includes = {includes},
    defines = {defines},
    copts = {copts},
    linkopts = {linkopts},
)
""".format(
        pkg_name = pkg_name,
        version = version,
        name = repository_ctx.name,
        includes = repr(includes),
        defines = repr(defines),
        copts = repr(copts),
        linkopts = repr(linkopts),
    )

    repository_ctx.file("BUILD.bazel", build_content)

pkg_config = repository_rule(
    implementation = _pkg_config_impl,
    attrs = {
        "pkg_name": attr.string(
            doc = "The pkg-config package name (defaults to repository name)",
        ),
        "optional": attr.bool(
            default = False,
            doc = "If true, create empty library when package is not found",
        ),
    },
    local = True,
    doc = "Creates a cc_library from pkg-config output.",
)

def _system_library_impl(repository_ctx):
    """Repository rule for system libraries that don't use pkg-config."""
    build_content = """
# SPDX-License-Identifier: LGPL-2.1-or-later
# System library: {name}

package(default_visibility = ["//visibility:public"])

cc_library(
    name = "{name}",
    linkopts = {linkopts},
    defines = {defines},
)
""".format(
        name = repository_ctx.name,
        linkopts = repr(repository_ctx.attr.linkopts),
        defines = repr(repository_ctx.attr.defines),
    )

    repository_ctx.file("BUILD.bazel", build_content)

system_library = repository_rule(
    implementation = _system_library_impl,
    attrs = {
        "linkopts": attr.string_list(
            default = [],
            doc = "Linker options",
        ),
        "defines": attr.string_list(
            default = [],
            doc = "Preprocessor definitions",
        ),
    },
    local = True,
    doc = "Creates a cc_library for a system library.",
)

def systemd_dependencies():
    """Define all external dependencies for systemd."""

    # Standard system libraries
    system_library(
        name = "libm",
        linkopts = ["-lm"],
    )

    system_library(
        name = "librt",
        linkopts = ["-lrt"],
    )

    system_library(
        name = "libdl",
        linkopts = ["-ldl"],
    )

    system_library(
        name = "libpthread",
        linkopts = ["-lpthread"],
    )

    system_library(
        name = "libcap",
        linkopts = ["-lcap"],
    )

    # Required pkg-config dependencies
    pkg_config(
        name = "libmount",
        pkg_name = "mount",
    )

    pkg_config(
        name = "libblkid",
        pkg_name = "blkid",
    )

    pkg_config(
        name = "libkmod",
        pkg_name = "libkmod",
        optional = True,
    )

    # Optional pkg-config dependencies
    pkg_config(
        name = "libseccomp",
        pkg_name = "libseccomp",
        optional = True,
    )

    pkg_config(
        name = "libselinux",
        pkg_name = "libselinux",
        optional = True,
    )

    pkg_config(
        name = "libaudit",
        pkg_name = "audit",
        optional = True,
    )

    pkg_config(
        name = "libacl",
        pkg_name = "libacl",
        optional = True,
    )

    pkg_config(
        name = "liblz4",
        pkg_name = "liblz4",
        optional = True,
    )

    pkg_config(
        name = "liblzma",
        pkg_name = "liblzma",
        optional = True,
    )

    pkg_config(
        name = "libzstd",
        pkg_name = "libzstd",
        optional = True,
    )

    pkg_config(
        name = "libgcrypt",
        pkg_name = "libgcrypt",
        optional = True,
    )

    pkg_config(
        name = "openssl",
        pkg_name = "openssl",
        optional = True,
    )

    pkg_config(
        name = "libcryptsetup",
        pkg_name = "libcryptsetup",
        optional = True,
    )

    pkg_config(
        name = "libbpf",
        pkg_name = "libbpf",
        optional = True,
    )

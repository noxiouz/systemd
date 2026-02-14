# SPDX-License-Identifier: LGPL-2.1-or-later

"""Configuration values for systemd Bazel build."""

# Version information
PROJECT_VERSION = "260"
PROJECT_VERSION_FULL = "260~devel"
VERSION_TAG = "260~devel"
LIBSYSTEMD_VERSION = "0.42.0"
LIBUDEV_VERSION = "1.7.12"

# Installation paths
PREFIX = "/usr"
BINDIR = "/usr/bin"
SBINDIR = "/usr/sbin"
LIBDIR = "/usr/lib"
LIBEXECDIR = "/usr/lib/systemd"
SYSCONFDIR = "/etc"
LOCALSTATEDIR = "/var"
INCLUDEDIR = "/usr/include"
DATADIR = "/usr/share"

# Derived paths
PKGSYSCONFDIR = SYSCONFDIR + "/systemd"
PKGDATADIR = DATADIR + "/systemd"
CATALOGDIR = LIBEXECDIR + "/catalog"
SYSTEMUNITDIR = LIBEXECDIR + "/system"
USERUNITDIR = LIBEXECDIR + "/user"
SYSTEMPRESETDIR = LIBEXECDIR + "/system-preset"
USERPRESETDIR = LIBEXECDIR + "/user-preset"
TMPFILESDIR = PREFIX + "/lib/tmpfiles.d"
SYSUSERSDIR = PREFIX + "/lib/sysusers.d"
SYSCTLDIR = PREFIX + "/lib/sysctl.d"
BINFMTDIR = PREFIX + "/lib/binfmt.d"
MODULESLOADDIR = PREFIX + "/lib/modules-load.d"
UDEVLIBEXECDIR = PREFIX + "/lib/udev"
UDEVRULESDIR = UDEVLIBEXECDIR + "/rules.d"
UDEVHWDBDIR = UDEVLIBEXECDIR + "/hwdb.d"
ENVIRONMENTDIR = PREFIX + "/lib/environment.d"
MODPROBEDIR = PREFIX + "/lib/modprobe.d"
KERNELINSTALLDIR = PREFIX + "/lib/kernel/install.d"
FACTORYDIR = DATADIR + "/factory"
BOOTLIBDIR = LIBEXECDIR + "/boot/efi"
POLKITPOLICYDIR = DATADIR + "/polkit-1/actions"
POLKITRULESDIR = DATADIR + "/polkit-1/rules.d"

# Binary paths
SYSTEMCTL_BINARY_PATH = BINDIR + "/systemctl"
SYSTEMD_BINARY_PATH = LIBEXECDIR + "/systemd"
SYSTEMD_EXECUTOR_BINARY_PATH = LIBEXECDIR + "/systemd-executor"
SYSTEMD_SHUTDOWN_BINARY_PATH = LIBEXECDIR + "/systemd-shutdown"

# Other paths
RANDOM_SEED = LOCALSTATEDIR + "/lib/systemd/random-seed"
RANDOM_SEED_DIR = LOCALSTATEDIR + "/lib/systemd"
CATALOG_DATABASE = LOCALSTATEDIR + "/lib/systemd/catalog/database"
CERTIFICATE_ROOT = SYSCONFDIR + "/ssl"

# Feature defaults
DEFAULT_TIMEOUT_SEC = 90
DEFAULT_USER_TIMEOUT_SEC = 90
UPDATE_HELPER_USER_TIMEOUT_SEC = 90
HIGH_RLIMIT_NOFILE = 524288

# Status defaults
STATUS_UNIT_FORMAT_DEFAULT = "description"
MEMORY_ACCOUNTING_DEFAULT = True
ANSI_OK_COLOR = "ANSI_HIGHLIGHT_GREEN"

# Build mode
BUILD_MODE_DEVELOPER = False

# Feature flags - these match the defines in .bazelrc
SYSTEMD_FEATURES = {
    "HAVE_SECCOMP": False,
    "HAVE_SELINUX": False,
    "HAVE_APPARMOR": False,
    "HAVE_SMACK": False,
    "HAVE_AUDIT": False,
    "HAVE_ACL": False,
    "HAVE_LZ4": False,
    "HAVE_XZ": False,
    "HAVE_ZSTD": False,
    "HAVE_BZIP2": False,
    "HAVE_GCRYPT": False,
    "HAVE_OPENSSL": False,
    "HAVE_QRENCODE": False,
    "HAVE_MICROHTTPD": False,
    "HAVE_GNUTLS": False,
    "HAVE_LIBCURL": False,
    "HAVE_LIBIDN": False,
    "HAVE_LIBIDN2": False,
    "HAVE_LIBIPTC": False,
    "HAVE_PCRE2": False,
    "HAVE_ELFUTILS": False,
    "HAVE_LIBCRYPTSETUP": False,
    "HAVE_P11KIT": False,
    "HAVE_LIBFIDO2": False,
    "HAVE_TPM2": False,
    "HAVE_PWQUALITY": False,
    "HAVE_PASSWDQC": False,
    "HAVE_BLKID": True,
    "HAVE_LIBMOUNT": True,
    "HAVE_KMOD": True,
    "HAVE_PAM": False,
    "HAVE_LIBCAP": True,
    "BPF_FRAMEWORK": False,
    "HAVE_SPLIT_BIN": False,
    "ENABLE_URLIFY": True,
    "ENABLE_FEXECVE": True,
    "LOG_MESSAGE_VERIFICATION": False,
    "FUZZ_USE_SIZE_LIMIT": False,
    "SD_BOOT": False,
    "ENABLE_FIRST_BOOT_FULL_PRESET": True,
    "CREATE_LOG_DIRS": True,
    "BUMP_PROC_SYS_FS_FILE_MAX": True,
    "BUMP_PROC_SYS_FS_NR_OPEN": True,
}

def _config_h_impl(ctx):
    """Generate config.h from template."""
    output = ctx.actions.declare_file("config.h")

    substitutions = {}

    # Add version info
    substitutions["@PROJECT_VERSION@"] = PROJECT_VERSION
    substitutions["@PROJECT_VERSION_STR@"] = PROJECT_VERSION
    substitutions["@PROJECT_VERSION_FULL@"] = PROJECT_VERSION_FULL
    substitutions["@PROJECT_URL@"] = "https://systemd.io/"
    substitutions["@VERSION_TAG@"] = VERSION_TAG
    substitutions["@LIBSYSTEMD_VERSION@"] = LIBSYSTEMD_VERSION
    substitutions["@LIBUDEV_VERSION@"] = LIBUDEV_VERSION

    # Add paths
    substitutions["@PREFIX@"] = PREFIX
    substitutions["@PREFIX_NOSLASH@"] = PREFIX.lstrip("/")
    substitutions["@BINDIR@"] = BINDIR
    substitutions["@SBINDIR@"] = SBINDIR
    substitutions["@LIBDIR@"] = LIBDIR
    substitutions["@LIBEXECDIR@"] = LIBEXECDIR
    substitutions["@SYSCONFDIR@"] = SYSCONFDIR
    substitutions["@INCLUDEDIR@"] = INCLUDEDIR
    substitutions["@DATADIR@"] = DATADIR
    substitutions["@PKGSYSCONFDIR@"] = PKGSYSCONFDIR
    substitutions["@PKGDATADIR@"] = PKGDATADIR
    substitutions["@CATALOGDIR@"] = CATALOGDIR
    substitutions["@SYSTEMUNITDIR@"] = SYSTEMUNITDIR
    substitutions["@USERUNITDIR@"] = USERUNITDIR
    substitutions["@SYSTEMPRESETDIR@"] = SYSTEMPRESETDIR
    substitutions["@USERPRESETDIR@"] = USERPRESETDIR
    substitutions["@TMPFILESDIR@"] = TMPFILESDIR
    substitutions["@SYSUSERSDIR@"] = SYSUSERSDIR
    substitutions["@SYSCTLDIR@"] = SYSCTLDIR
    substitutions["@BINFMTDIR@"] = BINFMTDIR
    substitutions["@MODULESLOADDIR@"] = MODULESLOADDIR
    substitutions["@UDEVLIBEXECDIR@"] = UDEVLIBEXECDIR
    substitutions["@UDEVRULESDIR@"] = UDEVRULESDIR
    substitutions["@UDEVHWDBDIR@"] = UDEVHWDBDIR
    substitutions["@ENVIRONMENTDIR@"] = ENVIRONMENTDIR
    substitutions["@MODPROBEDIR@"] = MODPROBEDIR
    substitutions["@KERNELINSTALLDIR@"] = KERNELINSTALLDIR
    substitutions["@FACTORYDIR@"] = FACTORYDIR
    substitutions["@BOOTLIBDIR@"] = BOOTLIBDIR
    substitutions["@POLKITPOLICYDIR@"] = POLKITPOLICYDIR
    substitutions["@POLKITRULESDIR@"] = POLKITRULESDIR
    substitutions["@SYSTEMCTL_BINARY_PATH@"] = SYSTEMCTL_BINARY_PATH
    substitutions["@SYSTEMD_BINARY_PATH@"] = SYSTEMD_BINARY_PATH
    substitutions["@SYSTEMD_EXECUTOR_BINARY_PATH@"] = SYSTEMD_EXECUTOR_BINARY_PATH
    substitutions["@SYSTEMD_SHUTDOWN_BINARY_PATH@"] = SYSTEMD_SHUTDOWN_BINARY_PATH
    substitutions["@RANDOM_SEED@"] = RANDOM_SEED
    substitutions["@RANDOM_SEED_DIR@"] = RANDOM_SEED_DIR
    substitutions["@CATALOG_DATABASE@"] = CATALOG_DATABASE
    substitutions["@CERTIFICATE_ROOT@"] = CERTIFICATE_ROOT

    # Numeric values
    substitutions["@DEFAULT_TIMEOUT_SEC@"] = str(DEFAULT_TIMEOUT_SEC)
    substitutions["@DEFAULT_USER_TIMEOUT_SEC@"] = str(DEFAULT_USER_TIMEOUT_SEC)
    substitutions["@UPDATE_HELPER_USER_TIMEOUT_SEC@"] = str(UPDATE_HELPER_USER_TIMEOUT_SEC)
    substitutions["@HIGH_RLIMIT_NOFILE@"] = str(HIGH_RLIMIT_NOFILE)

    ctx.actions.expand_template(
        template = ctx.file.template,
        output = output,
        substitutions = substitutions,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

config_h = rule(
    implementation = _config_h_impl,
    attrs = {
        "template": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "The config.h template file",
        ),
    },
    doc = "Generates config.h from template.",
)

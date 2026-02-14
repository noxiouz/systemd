# SPDX-License-Identifier: LGPL-2.1-or-later

"""Rules for generating perfect hash tables using gperf."""

def _gperf_impl(ctx):
    """Generate C code from gperf input file."""
    output = ctx.actions.declare_file(ctx.attr.out if ctx.attr.out else ctx.attr.name + ".c")

    args = ctx.actions.args()

    # Add gperf options
    if ctx.attr.language:
        args.add("-L", ctx.attr.language)
    if ctx.attr.struct_type:
        args.add("-t")
    if ctx.attr.ignore_case:
        args.add("--ignore-case")
    if ctx.attr.lookup_function_name:
        args.add("-N", ctx.attr.lookup_function_name)
    if ctx.attr.hash_function_name:
        args.add("-H", ctx.attr.hash_function_name)
    if ctx.attr.pic:
        args.add("-p")
    if ctx.attr.readonly_tables:
        args.add("-C")
    if ctx.attr.compare_strncmp:
        args.add("--compare-strncmp")
    if ctx.attr.readonly:
        args.add("-E")
    if ctx.attr.enum:
        args.add("-e")

    # Add any additional flags
    for flag in ctx.attr.extra_flags:
        args.add(flag)

    args.add(ctx.file.src)
    args.add("--output-file", output)

    ctx.actions.run(
        inputs = [ctx.file.src],
        outputs = [output],
        executable = "gperf",
        arguments = [args],
        mnemonic = "Gperf",
        progress_message = "Generating %s from %s" % (output.short_path, ctx.file.src.short_path),
    )

    return [
        DefaultInfo(files = depset([output])),
        OutputGroupInfo(
            out = depset([output]),
        ),
    ]

gperf = rule(
    implementation = _gperf_impl,
    attrs = {
        "src": attr.label(
            mandatory = True,
            allow_single_file = [".gperf"],
            doc = "The gperf input file",
        ),
        "out": attr.string(
            doc = "Output filename (defaults to name + '.c')",
        ),
        "language": attr.string(
            default = "ANSI-C",
            doc = "Output language (ANSI-C, C++, etc.)",
        ),
        "struct_type": attr.bool(
            default = True,
            doc = "Allow user-defined struct type (-t)",
        ),
        "ignore_case": attr.bool(
            default = False,
            doc = "Case-insensitive lookup (--ignore-case)",
        ),
        "lookup_function_name": attr.string(
            doc = "Name of lookup function (-N)",
        ),
        "hash_function_name": attr.string(
            doc = "Name of hash function (-H)",
        ),
        "pic": attr.bool(
            default = False,
            doc = "Generate position-independent code (-p)",
        ),
        "readonly_tables": attr.bool(
            default = False,
            doc = "Make tables read-only (-C)",
        ),
        "compare_strncmp": attr.bool(
            default = False,
            doc = "Use strncmp for comparisons (--compare-strncmp)",
        ),
        "readonly": attr.bool(
            default = False,
            doc = "Generate read-only declarations (-E)",
        ),
        "enum": attr.bool(
            default = False,
            doc = "Define constants using enum (-e)",
        ),
        "extra_flags": attr.string_list(
            default = [],
            doc = "Additional gperf flags",
        ),
    },
    doc = "Generates C code with perfect hash tables from gperf input files.",
)

def _gperf_capture_impl(ctx):
    """Generate C code from gperf input file, capturing stdout."""
    output = ctx.actions.declare_file(ctx.attr.out if ctx.attr.out else ctx.attr.name + ".c")

    # Build command for gperf
    cmd_parts = ["gperf"]

    if ctx.attr.language:
        cmd_parts.extend(["-L", ctx.attr.language])
    if ctx.attr.struct_type:
        cmd_parts.append("-t")
    if ctx.attr.ignore_case:
        cmd_parts.append("--ignore-case")
    if ctx.attr.lookup_function_name:
        cmd_parts.extend(["-N", ctx.attr.lookup_function_name])
    if ctx.attr.hash_function_name:
        cmd_parts.extend(["-H", ctx.attr.hash_function_name])
    if ctx.attr.pic:
        cmd_parts.append("-p")
    if ctx.attr.readonly_tables:
        cmd_parts.append("-C")
    for flag in ctx.attr.extra_flags:
        cmd_parts.append(flag)

    cmd_parts.append(ctx.file.src.path)

    ctx.actions.run_shell(
        inputs = [ctx.file.src],
        outputs = [output],
        command = " ".join(cmd_parts) + " > " + output.path,
        mnemonic = "GperfCapture",
        progress_message = "Generating %s from %s" % (output.short_path, ctx.file.src.short_path),
    )

    return [
        DefaultInfo(files = depset([output])),
        OutputGroupInfo(
            out = depset([output]),
        ),
    ]

gperf_capture = rule(
    implementation = _gperf_capture_impl,
    attrs = {
        "src": attr.label(
            mandatory = True,
            allow_single_file = [".gperf"],
            doc = "The gperf input file",
        ),
        "out": attr.string(
            doc = "Output filename (defaults to name + '.c')",
        ),
        "language": attr.string(
            default = "ANSI-C",
            doc = "Output language (ANSI-C, C++, etc.)",
        ),
        "struct_type": attr.bool(
            default = True,
            doc = "Allow user-defined struct type (-t)",
        ),
        "ignore_case": attr.bool(
            default = False,
            doc = "Case-insensitive lookup (--ignore-case)",
        ),
        "lookup_function_name": attr.string(
            doc = "Name of lookup function (-N)",
        ),
        "hash_function_name": attr.string(
            doc = "Name of hash function (-H)",
        ),
        "pic": attr.bool(
            default = False,
            doc = "Generate position-independent code (-p)",
        ),
        "readonly_tables": attr.bool(
            default = False,
            doc = "Make tables read-only (-C)",
        ),
        "extra_flags": attr.string_list(
            default = [],
            doc = "Additional gperf flags",
        ),
    },
    doc = "Generates C code with perfect hash tables from gperf input files (captures stdout).",
)

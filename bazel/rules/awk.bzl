# SPDX-License-Identifier: LGPL-2.1-or-later

"""Rules for AWK script processing."""

def _awk_codegen_impl(ctx):
    """Process files with AWK scripts."""
    output = ctx.actions.declare_file(ctx.attr.out)

    # Build command: awk -f script input1 input2 ... > output
    cmd_parts = ["awk", "-f", ctx.file.script.path]

    # Add any variables
    for var_name, var_value in ctx.attr.variables.items():
        cmd_parts.extend(["-v", "%s=%s" % (var_name, var_value)])

    # Add input files
    for f in ctx.files.srcs:
        cmd_parts.append(f.path)

    cmd = " ".join(cmd_parts) + " > " + output.path

    ctx.actions.run_shell(
        inputs = [ctx.file.script] + ctx.files.srcs,
        outputs = [output],
        command = cmd,
        mnemonic = "AwkCodegen",
        progress_message = "AWK processing %s" % output.short_path,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

awk_codegen = rule(
    implementation = _awk_codegen_impl,
    attrs = {
        "script": attr.label(
            mandatory = True,
            allow_single_file = [".awk"],
            doc = "The AWK script file",
        ),
        "srcs": attr.label_list(
            mandatory = True,
            allow_files = True,
            doc = "Input files to process",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "variables": attr.string_dict(
            default = {},
            doc = "AWK variables to set (-v name=value)",
        ),
    },
    doc = "Processes files with AWK scripts.",
)

def _awk_inline_impl(ctx):
    """Process files with inline AWK program."""
    output = ctx.actions.declare_file(ctx.attr.out)

    # Build command: awk 'program' input1 input2 ... > output
    cmd_parts = ["awk"]

    # Add any variables
    for var_name, var_value in ctx.attr.variables.items():
        cmd_parts.extend(["-v", "%s=%s" % (var_name, var_value)])

    # Add program (properly quoted)
    cmd_parts.append("'" + ctx.attr.program.replace("'", "'\\''") + "'")

    # Add input files
    for f in ctx.files.srcs:
        cmd_parts.append(f.path)

    cmd = " ".join(cmd_parts) + " > " + output.path

    ctx.actions.run_shell(
        inputs = ctx.files.srcs,
        outputs = [output],
        command = cmd,
        mnemonic = "AwkInline",
        progress_message = "AWK processing %s" % output.short_path,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

awk_inline = rule(
    implementation = _awk_inline_impl,
    attrs = {
        "program": attr.string(
            mandatory = True,
            doc = "The inline AWK program",
        ),
        "srcs": attr.label_list(
            mandatory = True,
            allow_files = True,
            doc = "Input files to process",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "variables": attr.string_dict(
            default = {},
            doc = "AWK variables to set (-v name=value)",
        ),
    },
    doc = "Processes files with inline AWK programs.",
)

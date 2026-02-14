# SPDX-License-Identifier: LGPL-2.1-or-later

"""Rules for shell and Python code generation."""

def _shell_codegen_impl(ctx):
    """Run a shell script to generate output, capturing stdout."""
    output = ctx.actions.declare_file(ctx.attr.out)

    # Build the command - pass CC/CPP and any additional args
    # The scripts expect: script CC [args...]
    # CC is typically used as: $CC -E -dM -include header.h - </dev/null
    # Note: meson passes 'cpp' (C preprocessor), not 'gcc'
    # Using 'cpp' is important because some scripts don't pass -E flag
    cmd_parts = ["bash", ctx.file.script.path, "cpp"]

    # Add system include arguments
    for include in ctx.attr.system_includes:
        cmd_parts.extend(["-isystem", include])

    cmd = " ".join(cmd_parts) + " > " + output.path

    ctx.actions.run_shell(
        inputs = [ctx.file.script] + ctx.files.srcs,
        outputs = [output],
        command = cmd,
        mnemonic = "ShellCodegen",
        progress_message = "Generating %s" % output.short_path,
        use_default_shell_env = True,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

shell_codegen = rule(
    implementation = _shell_codegen_impl,
    attrs = {
        "script": attr.label(
            mandatory = True,
            allow_single_file = [".sh"],
            doc = "The shell script to run",
        ),
        "srcs": attr.label_list(
            allow_files = True,
            doc = "Additional source files",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "system_includes": attr.string_list(
            default = [],
            doc = "System include paths to pass to CPP",
        ),
    },
    doc = "Runs a shell script with CPP to generate code.",
)

def _python_codegen_impl(ctx):
    """Run a Python script to generate output."""
    output = ctx.actions.declare_file(ctx.attr.out)

    # Build command parts
    cmd_parts = [ctx.attr.python_bin, ctx.file.script.path]
    cmd_parts.extend(ctx.attr.args)

    # Add input files as arguments if specified
    if ctx.attr.inputs_as_args:
        for f in ctx.files.srcs:
            cmd_parts.append(f.path)

    # Build environment
    env = dict(ctx.attr.env)
    if ctx.attr.pass_cpp:
        env["CPP"] = "${CC:-cc} -E"

    if ctx.attr.capture:
        cmd = " ".join(cmd_parts) + " > " + output.path
    else:
        cmd_parts.extend(["-o", output.path])
        cmd = " ".join(cmd_parts)

    ctx.actions.run_shell(
        inputs = [ctx.file.script] + ctx.files.srcs + ctx.files.data,
        outputs = [output],
        command = cmd,
        env = env,
        mnemonic = "PythonCodegen",
        progress_message = "Generating %s" % output.short_path,
        use_default_shell_env = True,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

python_codegen = rule(
    implementation = _python_codegen_impl,
    attrs = {
        "script": attr.label(
            mandatory = True,
            allow_single_file = [".py"],
            doc = "The Python script to run",
        ),
        "srcs": attr.label_list(
            allow_files = True,
            doc = "Source files as inputs",
        ),
        "data": attr.label_list(
            allow_files = True,
            doc = "Additional data files",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "args": attr.string_list(
            default = [],
            doc = "Arguments to pass to the script",
        ),
        "env": attr.string_dict(
            default = {},
            doc = "Environment variables",
        ),
        "python_bin": attr.string(
            default = "python3",
            doc = "Python interpreter to use",
        ),
        "capture": attr.bool(
            default = True,
            doc = "Capture stdout to output file",
        ),
        "inputs_as_args": attr.bool(
            default = False,
            doc = "Pass input files as arguments",
        ),
        "pass_cpp": attr.bool(
            default = False,
            doc = "Pass CPP environment variable",
        ),
    },
    doc = "Runs a Python script to generate code.",
)

def _generate_gperf_from_list_impl(ctx):
    """Generate gperf input from a list file using generate-gperfs.py."""
    output = ctx.actions.declare_file(ctx.attr.out)

    cmd_parts = [
        "python3",
        ctx.file.script.path,
        ctx.attr.name_arg,
        # Quote prefix to handle empty string properly
        "'" + ctx.attr.prefix + "'",
        ctx.file.list_file.path,
    ]
    # Quote the includes to prevent shell interpretation of < >
    for inc in ctx.attr.includes:
        cmd_parts.append("'" + inc + "'")

    cmd = " ".join(cmd_parts) + " > " + output.path

    ctx.actions.run_shell(
        inputs = [ctx.file.script, ctx.file.list_file],
        outputs = [output],
        command = cmd,
        mnemonic = "GenerateGperf",
        progress_message = "Generating %s" % output.short_path,
        use_default_shell_env = True,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

generate_gperf_from_list = rule(
    implementation = _generate_gperf_from_list_impl,
    attrs = {
        "script": attr.label(
            mandatory = True,
            allow_single_file = [".py"],
            doc = "The generate-gperfs.py script",
        ),
        "list_file": attr.label(
            mandatory = True,
            allow_single_file = [".txt"],
            doc = "The *-list.txt file",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "name_arg": attr.string(
            mandatory = True,
            doc = "Name argument (e.g., 'af', 'errno')",
        ),
        "prefix": attr.string(
            default = "",
            doc = "Prefix for identifiers",
        ),
        "includes": attr.string_list(
            default = [],
            doc = "Header includes (e.g., '<errno.h>')",
        ),
    },
    doc = "Generates gperf input from list files.",
)

def _multi_output_codegen_impl(ctx):
    """Run a script that generates multiple output files."""
    outputs = []
    for out_name in ctx.attr.outs:
        outputs.append(ctx.actions.declare_file(out_name))

    # Build command
    cmd_parts = [ctx.attr.interpreter, ctx.file.script.path]
    cmd_parts.extend(ctx.attr.args)

    # Add output paths if needed
    if ctx.attr.outputs_as_args:
        for out in outputs:
            cmd_parts.append(out.path)

    env = dict(ctx.attr.env)
    env["BAZEL_OUTPUT_DIR"] = outputs[0].dirname if outputs else ""

    ctx.actions.run_shell(
        inputs = [ctx.file.script] + ctx.files.srcs + ctx.files.data,
        outputs = outputs,
        command = " ".join(cmd_parts),
        env = env,
        mnemonic = "MultiCodegen",
        progress_message = "Generating %s" % ", ".join([o.short_path for o in outputs]),
        use_default_shell_env = True,
    )

    return [
        DefaultInfo(files = depset(outputs)),
    ]

multi_output_codegen = rule(
    implementation = _multi_output_codegen_impl,
    attrs = {
        "script": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "Script to run",
        ),
        "srcs": attr.label_list(
            allow_files = True,
            doc = "Source files",
        ),
        "data": attr.label_list(
            allow_files = True,
            doc = "Data files",
        ),
        "outs": attr.string_list(
            mandatory = True,
            doc = "Output filenames",
        ),
        "args": attr.string_list(
            default = [],
            doc = "Arguments",
        ),
        "env": attr.string_dict(
            default = {},
            doc = "Environment variables",
        ),
        "interpreter": attr.string(
            default = "python3",
            doc = "Script interpreter",
        ),
        "outputs_as_args": attr.bool(
            default = False,
            doc = "Pass outputs as arguments",
        ),
    },
    doc = "Runs a script that generates multiple output files.",
)

# SPDX-License-Identifier: LGPL-2.1-or-later

"""Rules for Jinja2 template processing."""

def _jinja2_template_impl(ctx):
    """Process a Jinja2 template with configuration values."""
    output = ctx.actions.declare_file(ctx.attr.out)

    # Build the Python script to render the template
    script_content = '''
import sys
import os
import ast
import re

try:
    import jinja2
except ImportError:
    print("Error: jinja2 module not found. Please install it with: pip install jinja2", file=sys.stderr)
    sys.exit(1)

def parse_config_h(filename):
    """Parse config.h file to extract defines."""
    ans = {}
    try:
        with open(filename) as f:
            for line in f:
                m = re.match(r'#define\\s+(\\w+)\\s+(.*)', line)
                if not m:
                    continue
                a, b = m.groups()
                b = b.strip()
                # Try to parse as a literal
                if b and (b[0] in '123456789"' or b == '0'):
                    try:
                        b = ast.literal_eval(b)
                    except:
                        pass
                ans[a] = b
    except FileNotFoundError:
        pass
    return ans

def render(template_path, defines):
    with open(template_path) as f:
        text = f.read()
    template = jinja2.Template(
        text,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined
    )
    return template.render(defines)

if __name__ == '__main__':
    config_h = sys.argv[1]
    template = sys.argv[2]
    output = sys.argv[3]

    defines = parse_config_h(config_h)

    # Add extra defines from environment
    for key in os.environ:
        if key.startswith('JINJA_'):
            defines[key[6:]] = os.environ[key]

    result = render(template, defines)
    with open(output, 'w') as f:
        f.write(result)
'''

    # Create a temporary script file
    script = ctx.actions.declare_file(ctx.attr.name + "_jinja2_render.py")
    ctx.actions.write(script, script_content)

    # Get config.h if provided
    config_inputs = []
    config_path = "/dev/null"
    if ctx.file.config_h:
        config_inputs = [ctx.file.config_h]
        config_path = ctx.file.config_h.path

    # Build environment with extra variables
    env = {}
    for key, value in ctx.attr.variables.items():
        env["JINJA_" + key] = value

    ctx.actions.run(
        inputs = [ctx.file.template, script] + config_inputs + ctx.files.data,
        outputs = [output],
        executable = "python3",
        arguments = [
            script.path,
            config_path,
            ctx.file.template.path,
            output.path,
        ],
        env = env,
        mnemonic = "Jinja2Template",
        progress_message = "Rendering Jinja2 template %s" % ctx.file.template.short_path,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

jinja2_template = rule(
    implementation = _jinja2_template_impl,
    attrs = {
        "template": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "The Jinja2 template file",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "config_h": attr.label(
            allow_single_file = True,
            doc = "The config.h file to parse for defines",
        ),
        "variables": attr.string_dict(
            default = {},
            doc = "Additional variables to pass to the template",
        ),
        "data": attr.label_list(
            allow_files = True,
            doc = "Additional data files",
        ),
    },
    doc = "Processes Jinja2 templates with config.h context.",
)

def _simple_template_impl(ctx):
    """Simple template substitution without Jinja2."""
    output = ctx.actions.declare_file(ctx.attr.out)

    ctx.actions.expand_template(
        template = ctx.file.template,
        output = output,
        substitutions = ctx.attr.substitutions,
    )

    return [
        DefaultInfo(files = depset([output])),
    ]

simple_template = rule(
    implementation = _simple_template_impl,
    attrs = {
        "template": attr.label(
            mandatory = True,
            allow_single_file = True,
            doc = "The template file",
        ),
        "out": attr.string(
            mandatory = True,
            doc = "Output filename",
        ),
        "substitutions": attr.string_dict(
            default = {},
            doc = "Substitutions to make (@KEY@ -> value)",
        ),
    },
    doc = "Simple template substitution.",
)

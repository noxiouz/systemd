# SPDX-License-Identifier: LGPL-2.1-or-later
project('scx-identity-fixture', 'c', meson_version : '>= 1.1', default_options : ['c_std=gnu17'])

linux = get_option('linux-source')
qmap = get_option('qmap-build')
cc = meson.get_compiler('c')

executable('identity-provider', 'provider.c',
           include_directories : include_directories(
                   qmap / 'include', linux / 'tools/sched_ext', linux / 'tools/sched_ext/include',
                   linux / 'tools/lib', linux / 'tools/include', linux / 'tools/include/uapi'),
           dependencies : [cc.find_library('bpf', dirs : qmap / 'obj/libbpf', static : true),
                           cc.find_library('elf'), dependency('zlib'), dependency('threads')])

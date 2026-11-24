# SPDX-License-Identifier: LGPL-2.1-or-later
project('scx-identity-fixture', 'c', meson_version : '>= 1.1', default_options : ['c_std=gnu17'])

linux = get_option('linux-source')
qmap = get_option('qmap-build')
cc = meson.get_compiler('c')

includes = include_directories(qmap / 'include', linux / 'tools/sched_ext', linux / 'tools/sched_ext/include',
                               linux / 'tools/lib', linux / 'tools/include', linux / 'tools/include/uapi')
deps = [cc.find_library('bpf', dirs : qmap / 'obj/libbpf', static : true),
        cc.find_library('elf'), dependency('zlib'), dependency('threads')]
executable('identity-provider', 'provider.c', 'qmap.c', include_directories : includes, dependencies : deps)

if get_option('mixed')
        clang = find_program('clang-23')
        bpftool = find_program(qmap / 'sbin/bpftool')
        foreach name : ['fifo', 'dispatch', 'admission-exec']
                object = custom_target(name + '.bpf.o', input : 'mixed/' + name + '.bpf.c', output : name + '.bpf.o',
                                       command : [clang, '-target', 'bpf', '-O2', '-g', '-mcpu=v3',
                                                  '-D__TARGET_ARCH_arm64', '-fms-extensions', '-Wno-microsoft-anon-tag',
                                                  '-I' + qmap / 'include', '-I' + linux / 'tools/sched_ext/include',
                                                  '-I' + linux / 'tools/include/uapi', '-I/usr/include/aarch64-linux-gnu',
                                                  '-c', '@INPUT@', '-o', '@OUTPUT@'], build_by_default : true)
                if name == 'fifo'
                        skel = custom_target('fifo.skel.h', input : object, output : 'fifo.skel.h',
                                             command : [bpftool, 'gen', 'skeleton', '@INPUT@', 'name', 'fifo'],
                                             capture : true)
                        executable('fifo-provider', 'provider.c', 'mixed/fifo.c', skel,
                                   include_directories : includes, dependencies : deps)
                endif
        endforeach
endif

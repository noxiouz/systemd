# SPDX-License-Identifier: LGPL-2.1-or-later

integration_tests += [
        integration_test_template + {
                'name' : fs.name(meson.current_source_dir()),
                'vm' : true,
                'mkosi-args' : ['--cpus=8'],
        },
]

foreach mode : ['attached', 'missing']
        integration_tests += [
                integration_test_template + {
                        'name' : 'TEST-99-SCHED-EXT-BOOT' + (mode == 'missing' ? '-MISSING' : ''),
                        'configuration' : integration_test_template['configuration'] + {
                                'command' : '/usr/lib/systemd/tests/testdata/units/sched-ext-boot.sh ' + mode + ' %n',
                        },
                        'vm' : true,
                        'firmware' : 'auto',
                        'storage' : 'persistent',
                        # The missing-loader case reports its verdict through the workload's OnFailure=.
                        'exit-code' : mode == 'missing' ? 187 : 123,
                },
        ]
endforeach

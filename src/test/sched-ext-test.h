/* SPDX-License-Identifier: LGPL-2.1-or-later */
#pragma once

#include <linux/types.h>

struct scx_test_exit {
        __s64 code;
        __s32 kind;
        char message[128];
};

/* SPDX-License-Identifier: LGPL-2.1-or-later */
#pragma once

#include "execute.h"
#include "unit.h"

typedef struct Scheduler {
        Unit meta;

        ExecCommand *exec_start;
        UnitRef target_slice;
} Scheduler;

extern const UnitVTable scheduler_vtable;

DEFINE_CAST(SCHEDULER, Scheduler);

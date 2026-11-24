/* SPDX-License-Identifier: LGPL-2.1-or-later */
#pragma once

#include "execute.h"
#include "unit.h"

typedef enum SchedulerState {
        SCHEDULER_DEAD,
        SCHEDULER_ATTACHING,
        SCHEDULER_ATTACHED,
        SCHEDULER_FAILED,
        _SCHEDULER_STATE_MAX,
} SchedulerState;

typedef struct Scheduler {
        Unit meta;

        SchedulerState state;
        ExecCommand *exec_start;
        UnitRef target_slice;
} Scheduler;

extern const UnitVTable scheduler_vtable;

DEFINE_CAST(SCHEDULER, Scheduler);

Unit* unit_get_start_scheduler(Unit *u);
void scheduler_set_state(Scheduler *s, SchedulerState state);
int scheduler_attach(Scheduler *s, int target_fd);
void scheduler_detach(Scheduler *s);

/* SPDX-License-Identifier: LGPL-2.1-or-later */
#pragma once

#include "cgroup.h"
#include "execute.h"
#include "unit.h"

typedef enum SchedulerState {
        SCHEDULER_DEAD,
        SCHEDULER_ATTACHING,
        SCHEDULER_ATTACHED,
        SCHEDULER_DETACHING,
        SCHEDULER_FAILED,
        _SCHEDULER_STATE_MAX,
} SchedulerState;

typedef struct SchedulerRuntime SchedulerRuntime;

typedef struct Scheduler {
        Unit meta;

        SchedulerState state;
        ExecCommand *exec_start;
        UnitRef target_slice;
        usec_t timeout_usec;
        int result;
        uint32_t link_id;
        char *failure_reason;

        ExecContext exec_context;
        CGroupContext cgroup_context;
        ExecRuntime *exec_runtime;
        CGroupRuntime *cgroup_runtime;
        SchedulerRuntime *runtime;
} Scheduler;

extern const UnitVTable scheduler_vtable;

DEFINE_CAST(SCHEDULER, Scheduler);

Unit* unit_get_start_scheduler(Unit *u);
void scheduler_set_state(Scheduler *s, SchedulerState state);
typedef struct SchedulerBackend {
        int (*attach)(Scheduler *s, int target_fd);
        void (*stop)(Scheduler *s);
        void (*done)(Scheduler *s);
        bool (*is_live)(Scheduler *s);
        void (*sigchld)(Scheduler *s, pid_t pid, int code, int status);
} SchedulerBackend;

extern const SchedulerBackend scheduler_backend;
bool scheduler_is_attached(Scheduler *s);

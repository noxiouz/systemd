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
        _SCHEDULER_STATE_INVALID = -EINVAL,
} SchedulerState;

typedef struct SchedulerRuntime SchedulerRuntime;

typedef struct Scheduler {
        Unit meta;

        SchedulerState state, deserialized_state;
        ExecCommand *exec_start;
        UnitRef target_slice, runtime_parent;
        usec_t timeout_usec;
        int result;
        uint32_t link_id;
        char *failure_reason;

        ExecContext exec_context;
        KillContext kill_context;
        CGroupContext cgroup_context;
        ExecRuntime *exec_runtime;
        CGroupRuntime *cgroup_runtime;
        SchedulerRuntime *runtime;
} Scheduler;

extern const UnitVTable scheduler_vtable;

DEFINE_CAST(SCHEDULER, Scheduler);

Unit* unit_get_start_scheduler(Unit *u);
int unit_verify_start_scheduler(Unit *u);
int unit_set_scheduler_dependencies(Unit *u, Unit *parent);
int scheduler_bind_parent(Scheduler *s, Unit *parent);
void scheduler_set_state(Scheduler *s, SchedulerState state);
typedef struct SchedulerBackend {
        int (*attach)(Scheduler *s, int target_fd);
        void (*stop)(Scheduler *s, bool initiate);
        void (*done)(Scheduler *s);
        int (*serialize)(Scheduler *s, FILE *f, FDSet *fds);
        int (*deserialize)(Scheduler *s, const char *key, const char *value, FDSet *fds);
        int (*coldplug)(Scheduler *s);
        bool (*is_live)(Scheduler *s);
        int (*get_effective)(Slice *slice, Unit **ret);
        void (*sigchld)(Scheduler *s, pid_t pid, int code, int status);
} SchedulerBackend;

extern const SchedulerBackend scheduler_backend;
bool scheduler_is_attached(Scheduler *s);

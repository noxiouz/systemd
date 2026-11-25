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
bool scheduler_is_attached(Scheduler *s);

int scheduler_attach(Scheduler *s, int target_fd);
void scheduler_detach(Scheduler *s, bool initiate);
bool scheduler_link_is_live(Scheduler *s);
int slice_get_effective_scheduler(Slice *slice, Unit **ret);
void scheduler_sigchld(Unit *u, pid_t pid, int code, int status);
void scheduler_runtime_done(Scheduler *s);
int scheduler_runtime_serialize(Scheduler *s, FILE *f, FDSet *fds);
int scheduler_runtime_deserialize(Scheduler *s, const char *key, const char *value, FDSet *fds);
int scheduler_runtime_coldplug(Scheduler *s);

/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "alloc-util.h"
#include "cgroup-util.h"
#include "dbus-unit.h"
#include "fd-util.h"
#include "manager.h"
#include "path-util.h"
#include "scheduler.h"
#include "slice.h"
#include "special.h"
#include "unit-name.h"

static const UnitActiveState state_translation_table[_SCHEDULER_STATE_MAX] = {
        [SCHEDULER_DEAD]      = UNIT_INACTIVE,
        [SCHEDULER_ATTACHING] = UNIT_ACTIVATING,
        [SCHEDULER_ATTACHED]  = UNIT_ACTIVE,
        [SCHEDULER_DETACHING] = UNIT_DEACTIVATING,
        [SCHEDULER_FAILED]    = UNIT_FAILED,
};

Unit* unit_get_start_scheduler(Unit *u) {
        if (u->type == UNIT_SERVICE)
                return slice_get_configured_scheduler(SLICE(UNIT_GET_SLICE(u)));

        if (u->type == UNIT_SCHEDULER) {
                Unit *target = UNIT_DEREF(SCHEDULER(u)->target_slice);
                if (target)
                        return slice_get_configured_scheduler(SLICE(UNIT_GET_SLICE(target)));
        }

        return NULL;
}

void scheduler_set_state(Scheduler *s, SchedulerState state) {
        SchedulerState old = s->state;

        if (state == old)
                return;

        bus_unit_send_pending_change_signal(UNIT(s), /* including_new= */ false);
        s->state = state;
        unit_notify(UNIT(s), state_translation_table[old], state_translation_table[state], /* reload_success= */ true);
}

bool scheduler_is_attached(Scheduler *s) {
        return s->state == SCHEDULER_ATTACHED && (!scheduler_backend.is_live || scheduler_backend.is_live(s));
}

static void scheduler_init(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));

        s->timeout_usec = u->manager->defaults.timeout_start_usec;
        s->exec_context.std_output = u->manager->defaults.std_output;
        s->exec_context.std_error = u->manager->defaults.std_error;
}

static void scheduler_done(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));

        if (scheduler_backend.done)
                scheduler_backend.done(s);
        free(s->failure_reason);
        exec_command_free_list(s->exec_start);
        unit_ref_unset(&s->target_slice);
}

static int scheduler_load(Unit *u) {
        _cleanup_free_ char *name = NULL;
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));
        Unit *target;
        int r;

        r = unit_load_fragment_and_dropin(u, /* fragment_required= */ true);
        if (r < 0 || u->load_state != UNIT_LOADED)
                return r;

        if (!s->exec_start || s->exec_start->command_next)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOEXEC),
                                            "A scheduler must have exactly one ExecStart= command.");

        if (!u->instance)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOEXEC),
                                            "A scheduler must be instantiated for a slice.");

        r = unit_name_build_from_type(u->instance, NULL, UNIT_SLICE, &name);
        if (r == -ENOMEM)
                return r;
        if (r < 0 || !slice_name_is_valid(name))
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOEXEC),
                                            "Instance '%s' does not identify a slice.", u->instance);

        r = manager_load_unit_prepare(u->manager, name, NULL, NULL, &target);
        if (r < 0)
                return r;

        unit_ref_set(&s->target_slice, u, target);
        r = unit_load(target);
        if (r < 0 || target->load_state != UNIT_LOADED)
                return log_unit_error_errno(u, r < 0 ? r : SYNTHETIC_ERRNO(ENOEXEC),
                                            "Cannot load target slice %s.", name);

        Unit *declared = UNIT_DEREF(SLICE(target)->scheduler);
        if (declared && declared != u)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOEXEC), "%s declares Scheduler=%s.", name, declared->id);

        r = unit_patch_contexts(u);
        if (r < 0)
                return r;
        r = unit_set_default_slice(u);
        if (r < 0)
                return r;

        /* The target cgroup must exist independently of its scheduler, including at the perpetual root. */
        r = unit_add_two_dependencies(u, UNIT_AFTER, UNIT_REQUIRES, target, /* add_reference= */ true, UNIT_DEPENDENCY_IMPLICIT);
        if (r < 0)
                return r;

        if (path_is_absolute(s->exec_start->path))
                return unit_add_mounts_for(u, s->exec_start->path, UNIT_DEPENDENCY_FILE, UNIT_MOUNT_REQUIRES);

        return 0;
}

static int scheduler_start(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));
        Unit *target = UNIT_DEREF(s->target_slice);

        if (!target || UNIT_DEREF(SLICE(target)->scheduler) != u)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOLINK),
                                            "The target slice must declare this scheduler with Scheduler=.");

        if (unit_active_state(target) != UNIT_ACTIVE)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOLINK), "Target slice %s is inactive.", target->id);

        if (!unit_has_name(target, SPECIAL_ROOT_SLICE) && !unit_get_start_scheduler(u))
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOLINK),
                                            "Target slice %s has no configured parent scheduler.", target->id);

        if (s->state == SCHEDULER_ATTACHING)
                return 0;

        int r = unit_realize_cgroup(target);
        if (r < 0)
                return log_unit_error_errno(u, r, "Failed to realize target cgroup for %s: %m", target->id);

        CGroupRuntime *crt = unit_get_cgroup_runtime(target);
        if (!crt || !crt->cgroup_path)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENXIO), "Target %s has no cgroup.", target->id);

        _cleanup_close_ int fd = cg_path_open(crt->cgroup_path);
        if (fd < 0)
                return log_unit_error_errno(u, fd, "Cannot open target cgroup of %s: %m", target->id);

        r = unit_acquire_invocation_id(u);
        if (r < 0)
                return r;

        s->result = 0;
        s->failure_reason = mfree(s->failure_reason);
        scheduler_set_state(s, SCHEDULER_ATTACHING);
        r = scheduler_backend.attach(s, fd);
        if (r < 0) {
                s->result = r;
                scheduler_set_state(s, SCHEDULER_FAILED);
                return log_unit_error_errno(u, r, "Failed to attach scheduler to %s: %m", target->id);
        }

        return 1;
}

static int scheduler_stop(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));

        scheduler_set_state(s, SCHEDULER_DETACHING);
        scheduler_backend.stop(s);
        return 1;
}

static void scheduler_sigchld(Unit *u, pid_t pid, int code, int status) {
        if (scheduler_backend.sigchld)
                scheduler_backend.sigchld(SCHEDULER(u), pid, code, status);
}

static void scheduler_reset_failed(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));

        if (s->state == SCHEDULER_FAILED) {
                s->result = 0;
                s->failure_reason = mfree(s->failure_reason);
                scheduler_set_state(s, SCHEDULER_DEAD);
        }
}

static UnitActiveState scheduler_active_state(Unit *u) {
        return state_translation_table[SCHEDULER(u)->state];
}

static const char* scheduler_sub_state_to_string(Unit *u) {
        static const char *const table[_SCHEDULER_STATE_MAX] = {
                [SCHEDULER_DEAD]      = "dead",
                [SCHEDULER_ATTACHING] = "attaching",
                [SCHEDULER_ATTACHED]  = "attached",
                [SCHEDULER_DETACHING] = "detaching",
                [SCHEDULER_FAILED]    = "failed",
        };

        return table[SCHEDULER(u)->state];
}

const UnitVTable scheduler_vtable = {
        .object_size = sizeof(Scheduler),
        .exec_context_offset = offsetof(Scheduler, exec_context),
        .cgroup_context_offset = offsetof(Scheduler, cgroup_context),
        .exec_runtime_offset = offsetof(Scheduler, exec_runtime),
        .cgroup_runtime_offset = offsetof(Scheduler, cgroup_runtime),
        .init = scheduler_init,
        .sigchld_event = scheduler_sigchld,
        .reset_failed = scheduler_reset_failed,
        .sections = "Unit\0" "Scheduler\0" "Install\0",
        .done = scheduler_done,
        .load = scheduler_load,
        .start = scheduler_start,
        .stop = scheduler_stop,
        .can_fail = true,
        .active_state = scheduler_active_state,
        .sub_state_to_string = scheduler_sub_state_to_string,
};

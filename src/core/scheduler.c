/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "alloc-util.h"
#include "cgroup-util.h"
#include "dbus-unit.h"
#include "escape.h"
#include "fd-util.h"
#include "manager.h"
#include "parse-util.h"
#include "path-util.h"
#include "scheduler.h"
#include "serialize.h"
#include "service.h"
#include "slice.h"
#include "special.h"
#include "string-table.h"
#include "string-util.h"
#include "unit-name.h"

static const UnitActiveState state_translation_table[_SCHEDULER_STATE_MAX] = {
        [SCHEDULER_DEAD]      = UNIT_INACTIVE,
        [SCHEDULER_ATTACHING] = UNIT_ACTIVATING,
        [SCHEDULER_ATTACHED]  = UNIT_ACTIVE,
        [SCHEDULER_DETACHING] = UNIT_DEACTIVATING,
        [SCHEDULER_FAILED]    = UNIT_FAILED,
};

Unit* unit_get_start_scheduler(Unit *u) {
        if (u->type == UNIT_SERVICE) {
                /* Action-only services, including systemd-poweroff.service, have no process to schedule. */
                FOREACH_ELEMENT(command, SERVICE(u)->exec_command)
                        if (*command)
                                return slice_get_configured_scheduler(SLICE(UNIT_GET_SLICE(u)));
                return NULL;
        }

        if (u->type == UNIT_SCHEDULER) {
                Unit *target = UNIT_DEREF(SCHEDULER(u)->target_slice);
                if (target)
                        return slice_get_configured_scheduler(SLICE(UNIT_GET_SLICE(target)));
        }

        return NULL;
}

int unit_verify_start_scheduler(Unit *u) {
        Unit *required = unit_get_start_scheduler(u);
        int r;

        if (!required)
                return 0;

        /* A condition-skipped or lost scheduler may have completed its job successfully. Check
         * the complete configured chain immediately before handing admission to the unit type. */
        for (Unit *scheduler = required, *parent; scheduler; scheduler = parent) {
                parent = unit_get_start_scheduler(scheduler);
                if (!scheduler_is_attached(SCHEDULER(scheduler)) || UNIT_DEREF(SCHEDULER(scheduler)->runtime_parent) != parent)
                        return log_unit_notice_errno(u, SYNTHETIC_ERRNO(ENOLINK),
                                                     "Required scheduler %s is not attached under its configured parent.", scheduler->id);
        }

        if (u->type == UNIT_SERVICE) {
                Unit *effective;
                r = scheduler_backend.get_effective(SLICE(UNIT_GET_SLICE(u)), &effective);
                if (r < 0 || effective != required)
                        return log_unit_notice_errno(u, r < 0 && r != -EAGAIN ? r : SYNTHETIC_ERRNO(ENOLINK),
                                                     "Cannot confirm effective scheduler %s for workload.", required->id);
        }

        return 0;
}

int unit_set_scheduler_dependencies(Unit *u, Unit *parent) {
        assert(parent);

        UnitDependencyInfo previous = { .data = hashmap_get(unit_get_dependencies(u, UNIT_WANTS), parent) };
        if (FLAGS_SET(previous.origin_mask, UNIT_DEPENDENCY_SCHEDULER))
                return 0;

        unit_remove_dependencies(u, UNIT_DEPENDENCY_SCHEDULER);
        return unit_add_two_dependencies(u, UNIT_AFTER, UNIT_WANTS, parent, /* add_reference= */ false, UNIT_DEPENDENCY_SCHEDULER);
}

int scheduler_bind_parent(Scheduler *s, Unit *parent) {
        Unit *u = UNIT(s);
        int r;

        /* Reload may select a different parent for the next start. Keep the current parent pinned,
         * and keep its stop ordering and restart propagation until this attachment is gone. */
        r = unit_add_two_dependencies(u, UNIT_AFTER, UNIT_PART_OF, parent, /* add_reference= */ false, UNIT_DEPENDENCY_SCHEDULER_RUNTIME);
        if (r < 0)
                return r;
        r = unit_add_dependency(u, UNIT_WANTS, parent, /* add_reference= */ false, UNIT_DEPENDENCY_SCHEDULER_RUNTIME);
        if (r < 0)
                return r;

        unit_ref_set(&s->runtime_parent, u, parent);
        return 0;
}

void scheduler_set_state(Scheduler *s, SchedulerState state) {
        SchedulerState old = s->state;

        if (state == old)
                return;

        if (IN_SET(state, SCHEDULER_DEAD, SCHEDULER_FAILED))
                s->exec_runtime = exec_runtime_destroy(s->exec_runtime);

        bus_unit_send_pending_change_signal(UNIT(s), /* including_new= */ false);
        s->state = state;
        unit_notify(UNIT(s), state_translation_table[old], state_translation_table[state], /* reload_success= */ true);

        /* Job completion wakes the previous parent's stop job through the ordering edge. */
        if (IN_SET(state, SCHEDULER_FAILED, SCHEDULER_DEAD)) {
                unit_remove_dependencies(UNIT(s), UNIT_DEPENDENCY_SCHEDULER_RUNTIME);
                unit_ref_unset(&s->runtime_parent);
        }
}

bool scheduler_is_attached(Scheduler *s) {
        return s->state == SCHEDULER_ATTACHED && scheduler_backend.is_live(s);
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
        s->exec_runtime = exec_runtime_free(s->exec_runtime);
        free(s->failure_reason);
        exec_command_free_list(s->exec_start);
        unit_ref_unset(&s->target_slice);
        unit_ref_unset(&s->runtime_parent);
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

static int scheduler_test_startable(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));
        int r;

        if (s->state == SCHEDULER_ATTACHING)
                return false;

        r = unit_test_start_limit(u);
        if (r < 0) {
                s->result = r;
                s->failure_reason = mfree(s->failure_reason);
                scheduler_set_state(s, SCHEDULER_FAILED);
                return r;
        }

        return true;
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
        Unit *parent = unit_get_start_scheduler(u);
        r = parent ? scheduler_bind_parent(s, parent) : 0;
        if (r >= 0)
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

        if (s->state == SCHEDULER_DETACHING)
                return 0;

        scheduler_set_state(s, SCHEDULER_DETACHING);
        scheduler_backend.stop(s, /* initiate= */ true);
        return 1;
}

static void scheduler_notify_cgroup_empty(Unit *u) {
        Scheduler *s = SCHEDULER(u);

        if (s->state == SCHEDULER_DETACHING)
                scheduler_backend.stop(s, /* initiate= */ false);
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

static const char *const scheduler_state_table[_SCHEDULER_STATE_MAX] = {
        [SCHEDULER_DEAD]      = "dead",
        [SCHEDULER_ATTACHING] = "attaching",
        [SCHEDULER_ATTACHED]  = "attached",
        [SCHEDULER_DETACHING] = "detaching",
        [SCHEDULER_FAILED]    = "failed",
};

DEFINE_PRIVATE_STRING_TABLE_LOOKUP(scheduler_state, SchedulerState);

static const char* scheduler_sub_state_to_string(Unit *u) {
        return scheduler_state_to_string(SCHEDULER(u)->state);
}

static int scheduler_serialize(Unit *u, FILE *f, FDSet *fds) {
        Scheduler *s = SCHEDULER(u);

        (void) serialize_item(f, "state", scheduler_state_to_string(s->state));
        (void) serialize_item_format(f, "scheduler-result", "%d", s->result);
        if (u->start_limit_hit)
                (void) serialize_bool(f, "scheduler-start-limit-hit", true);
        (void) serialize_item_escaped(f, "scheduler-failure", s->failure_reason);
        return scheduler_backend.serialize ? scheduler_backend.serialize(s, f, fds) : 0;
}

static int scheduler_deserialize_item(Unit *u, const char *key, const char *value, FDSet *fds) {
        Scheduler *s = SCHEDULER(u);

        if (streq(key, "state")) {
                SchedulerState state = scheduler_state_from_string(value);
                if (state >= 0)
                        s->deserialized_state = state;
                return 0;
        }
        if (streq(key, "scheduler-start-limit-hit")) {
                int r = parse_boolean(value);
                if (r >= 0)
                        u->start_limit_hit = r;
                return r;
        }
        if (streq(key, "scheduler-result"))
                return safe_atoi(value, &s->result);
        if (streq(key, "scheduler-failure"))
                return cunescape(value, 0, &s->failure_reason);

        return scheduler_backend.deserialize ? scheduler_backend.deserialize(s, key, value, fds) : 0;
}

static int scheduler_coldplug(Unit *u) {
        Scheduler *s = SCHEDULER(u);

        if (IN_SET(s->deserialized_state, SCHEDULER_DEAD, SCHEDULER_FAILED)) {
                scheduler_set_state(s, s->deserialized_state);
                return 0;
        }
        (void) unit_setup_exec_runtime(u);
        if (scheduler_backend.coldplug)
                return scheduler_backend.coldplug(s);
        return -EOPNOTSUPP;
}

const UnitVTable scheduler_vtable = {
        .object_size = sizeof(Scheduler),
        .exec_context_offset = offsetof(Scheduler, exec_context),
        .kill_context_offset = offsetof(Scheduler, kill_context),
        .cgroup_context_offset = offsetof(Scheduler, cgroup_context),
        .exec_runtime_offset = offsetof(Scheduler, exec_runtime),
        .cgroup_runtime_offset = offsetof(Scheduler, cgroup_runtime),
        .init = scheduler_init,
        .sigchld_event = scheduler_sigchld,
        .notify_cgroup_empty = scheduler_notify_cgroup_empty,
        .reset_failed = scheduler_reset_failed,
        .sections = "Unit\0" "Scheduler\0" "Install\0",
        .done = scheduler_done,
        .load = scheduler_load,
        .serialize = scheduler_serialize,
        .deserialize_item = scheduler_deserialize_item,
        .coldplug = scheduler_coldplug,
        .test_startable = scheduler_test_startable,
        .start = scheduler_start,
        .stop = scheduler_stop,
        .can_fail = true,
        .active_state = scheduler_active_state,
        .sub_state_to_string = scheduler_sub_state_to_string,
};

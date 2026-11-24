/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "alloc-util.h"
#include "manager.h"
#include "scheduler.h"
#include "slice.h"
#include "unit-name.h"

static void scheduler_done(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));

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

        return 0;
}

static int scheduler_start(Unit *u) {
        Scheduler *s = ASSERT_PTR(SCHEDULER(u));
        Unit *target = UNIT_DEREF(s->target_slice);

        if (!target || UNIT_DEREF(SLICE(target)->scheduler) != u)
                return log_unit_error_errno(u, SYNTHETIC_ERRNO(ENOLINK),
                                            "The target slice must declare this scheduler with Scheduler=.");

        return -EOPNOTSUPP;
}

static UnitActiveState scheduler_active_state(Unit *u) {
        return UNIT_INACTIVE;
}

static const char* scheduler_sub_state_to_string(Unit *u) {
        return "dead";
}

const UnitVTable scheduler_vtable = {
        .object_size = sizeof(Scheduler),
        .sections = "Unit\0" "Scheduler\0" "Install\0",
        .done = scheduler_done,
        .load = scheduler_load,
        .start = scheduler_start,
        .active_state = scheduler_active_state,
        .sub_state_to_string = scheduler_sub_state_to_string,
};

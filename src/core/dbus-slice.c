/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "alloc-util.h"
#include "bus-get-properties.h"
#include "dbus-cgroup.h"
#include "dbus-slice.h"
#include "dbus-util.h"
#include "fileio.h"
#include "manager.h"
#include "scheduler.h"
#include "slice.h"
#include "string-util.h"
#include "unit.h"

static int property_get_currently_active(
                sd_bus *bus,
                const char *path,
                const char *interface,
                const char *property,
                sd_bus_message *reply,
                void *userdata,
                sd_bus_error *reterr_error) {

        Slice *s = ASSERT_PTR(userdata);

        assert(bus);
        assert(reply);

        return sd_bus_message_append(
                        reply,
                        "u",
                        (uint32_t) slice_get_currently_active(s, /* ignore= */ NULL, /* with_pending= */ false));
}

static int property_get_slice_scheduler(
                sd_bus *bus,
                const char *path,
                const char *interface,
                const char *property,
                sd_bus_message *reply,
                void *userdata,
                sd_bus_error *reterr_error) {

        Slice *s = ASSERT_PTR(userdata);
        const char *value;
        int r;

        if (streq(property, "Scheduler") || streq(property, "ConfiguredScheduler")) {
                Unit *configured = streq(property, "Scheduler") ?
                        UNIT_DEREF(s->scheduler) : slice_get_configured_scheduler(s);
                value = configured ? configured->id : NULL;
        } else {
                _cleanup_free_ char *state = NULL;

                r = read_one_line_file("/sys/kernel/sched_ext/state", &state);
                if (r == -ENOENT || (r >= 0 && streq(state, "disabled")))
                        value = "kernel";
                else if (r < 0 || !streq(state, "enabled"))
                        value = "unknown";
                else if (unit_active_state(UNIT(s)) != UNIT_ACTIVE)
                        value = "inactive";
                else if (MANAGER_IS_USER(UNIT(s)->manager))
                        value = "foreign";
                else {
                        Unit *effective;
                        r = slice_get_effective_scheduler(s, &effective);
                        if (r == -ENOMEM)
                                return r;
                        value = r < 0 ? "unknown" : effective ? effective->id : "foreign";
                }
        }

        return sd_bus_message_append(reply, "s", value);
}

const sd_bus_vtable bus_slice_vtable[] = {
        SD_BUS_VTABLE_START(0),
        SD_BUS_PROPERTY("Scheduler", "s", property_get_slice_scheduler, 0, 0),
        SD_BUS_PROPERTY("ConfiguredScheduler", "s", property_get_slice_scheduler, 0, 0),
        SD_BUS_PROPERTY("EffectiveScheduler", "s", property_get_slice_scheduler, 0, 0),
        /* The following are currently constant, but we should change that eventually (i.e. open them up via
         * systemctl set-property), hence they aren't marked as constant */
        SD_BUS_PROPERTY("ConcurrencyHardMax", "u", bus_property_get_unsigned, offsetof(Slice, concurrency_hard_max), 0),
        SD_BUS_PROPERTY("ConcurrencySoftMax", "u", bus_property_get_unsigned, offsetof(Slice, concurrency_soft_max), 0),
        SD_BUS_PROPERTY("ActivatingConcurrencyMax", "u", bus_property_get_unsigned, offsetof(Slice, activating_concurrency_max), 0),
        SD_BUS_PROPERTY("NCurrentlyActive", "u", property_get_currently_active, 0, 0),
        SD_BUS_VTABLE_END
};

static int bus_slice_set_transient_property(
                Slice *s,
                const char *name,
                sd_bus_message *message,
                UnitWriteFlags flags,
                sd_bus_error *reterr_error) {

        Unit *u = UNIT(s);

        assert(s);
        assert(name);
        assert(message);

        flags |= UNIT_PRIVATE;

        if (streq(name, "ConcurrencyHardMax"))
                return bus_set_transient_unsigned(u, name, &s->concurrency_hard_max, message, flags, reterr_error);

        if (streq(name, "ConcurrencySoftMax"))
                return bus_set_transient_unsigned(u, name, &s->concurrency_soft_max, message, flags, reterr_error);

        if (streq(name, "ActivatingConcurrencyMax"))
                return bus_set_transient_unsigned(u, name, &s->activating_concurrency_max, message, flags, reterr_error);

        return 0;
}

int bus_slice_set_property(
                Unit *u,
                const char *name,
                sd_bus_message *message,
                UnitWriteFlags flags,
                sd_bus_error *reterr_error) {

        Slice *s = SLICE(u);
        int r;

        assert(name);
        assert(u);

        r = bus_cgroup_set_property(u, &s->cgroup_context, name, message, flags, reterr_error);
        if (r != 0)
                return r;

        if (u->transient && u->load_state == UNIT_STUB) {
                /* This is a transient unit, let's allow a little more */

                r = bus_slice_set_transient_property(s, name, message, flags, reterr_error);
                if (r != 0)
                        return r;
        }

        return 0;
}

int bus_slice_commit_properties(Unit *u) {
        assert(u);

        (void) unit_realize_cgroup(u);

        return 0;
}

/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "bus-get-properties.h"
#include "dbus-scheduler.h"
#include "errno-util.h"
#include "scheduler.h"
#include "string-util.h"

static int property_get_scheduler(
                sd_bus *bus,
                const char *path,
                const char *interface,
                const char *property,
                sd_bus_message *reply,
                void *userdata,
                sd_bus_error *reterr_error) {

        Scheduler *s = ASSERT_PTR(userdata);
        const char *value;

        if (streq(property, "TargetSlice")) {
                Unit *target = UNIT_DEREF(s->target_slice);
                value = target ? target->id : NULL;
        } else if (streq(property, "ParentScheduler")) {
                Unit *parent = unit_get_start_scheduler(UNIT(s));
                value = parent ? parent->id : NULL;
        } else if (streq(property, "FailureReason"))
                value = s->failure_reason ?: (s->result < 0 ? STRERROR(s->result) : NULL);
        else {
                assert(streq(property, "Result"));
                switch (-s->result) {
                case 0:          value = "success";       break;
                case ETIMEDOUT:  value = "timeout";       break;
                case EBUSY:      value = "target-busy";   break;
                case EOPNOTSUPP: value = "unsupported";   break;
                case ECHILD:     value = "exit-code";     break;
                case ECANCELED:  value = "kernel-exit";   break;
                case ENOLINK:    value = "dependency";    break;
                case ESTALE:     value = "stale";         break;
                case EBADMSG:
                case EBADFD:
                case EXDEV:      value = "protocol";      break;
                default:         value = "resources";     break;
                }
        }

        return sd_bus_message_append(reply, "s", value);
}

const sd_bus_vtable bus_scheduler_vtable[] = {
        SD_BUS_VTABLE_START(0),
        SD_BUS_PROPERTY("TargetSlice", "s", property_get_scheduler, 0, SD_BUS_VTABLE_PROPERTY_CONST),
        SD_BUS_PROPERTY("ParentScheduler", "s", property_get_scheduler, 0, 0),
        SD_BUS_PROPERTY("Result", "s", property_get_scheduler, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
        SD_BUS_PROPERTY("FailureReason", "s", property_get_scheduler, 0, SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
        SD_BUS_PROPERTY("LinkID", "u", NULL, offsetof(Scheduler, link_id), SD_BUS_VTABLE_PROPERTY_EMITS_CHANGE),
        SD_BUS_VTABLE_END
};

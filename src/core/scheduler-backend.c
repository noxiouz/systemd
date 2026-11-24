/* SPDX-License-Identifier: LGPL-2.1-or-later */

#include "scheduler.h"

int scheduler_attach(Scheduler *s, int target_fd) {
        /* Admission requires independently verified identity, target, and parent of the attachment. */
        return -EOPNOTSUPP;
}

void scheduler_detach(Scheduler *s) {
}

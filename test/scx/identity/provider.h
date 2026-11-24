/* SPDX-License-Identifier: GPL-2.0 */
#pragma once

#include <sys/types.h>

struct bpf_link;
struct user_exit_info;

int run_provider(const char *generation, ino_t target, const char *role);
int serve_provider(const char *generation, ino_t target, struct user_exit_info *uei,
                   struct bpf_link *link, int attach_errno);

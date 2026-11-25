---
title: Scheduler Provider Protocol
category: Interfaces
layout: default
SPDX-License-Identifier: LGPL-2.1-or-later
---

# Scheduler Provider Protocol

A provider started by a `.scheduler` unit attaches a BPF CPU scheduler to the
cgroup selected by the system manager. The manager appends three arguments to the
configured command: the 32 lowercase hexadecimal digits of the invocation ID,
the decimal file descriptor number of the target cgroup directory, and the role
(`root` or `child`). Use the inode number of the supplied directory as the target
cgroup ID. Standard input is a connected Unix `SOCK_SEQPACKET` socket private to
this invocation.

Send one text record per packet, without a trailing newline or NUL:

```text
EVENT INVOCATION_ID CGROUP_ID ERRNO EXIT_KIND EXIT_CODE MESSAGE
```

Separate the fields with a single space. Numbers other than the invocation ID
are decimal. The final message may be empty or contain spaces. All records must
describe the target and invocation of this connection.

* Send `attach` after the attach call returns. On success, send the struct_ops
  link FD for this attachment with the packet using `SCM_RIGHTS`. On failure,
  supply the positive errno of the failed call.
* Respond to each one-byte `S` request with a `status` record containing the
  current kernel exit kind, code and diagnostic for this invocation. A live
  scheduler reports zero errno and exit kind.
* Send `exit` when a terminal kernel record becomes available and retain that
  record for subsequent status requests. A parent can reject a child after
  struct_ops registration succeeds.
* On a one-byte `Q` request or end of file, exit and release resources. If
  attachment succeeded concurrently with cancellation, still transfer the link
  before exiting so the manager can explicitly detach it.

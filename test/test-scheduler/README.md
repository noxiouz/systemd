# Scheduler activation transaction

Run `meson test -C build -v test-scheduler` from a writable, delegated cgroup on Linux.
The test manager uses a link-time backend that can delay, acknowledge, or fail an attachment.
Its file descriptor identifies the actual target cgroup. Its service commands run through
systemd-executor. The verbose test log includes the target and parent of every attempt,
the service command markers, the failed jobs, and the names in the intentional ordering cycle.

#!/usr/bin/env bash
# SPDX-License-Identifier: LGPL-2.1-or-later
set -euxo pipefail

# shellcheck source=test/units/test-control.sh
. "$(dirname "$0")"/test-control.sh

run_subtests_and_exit

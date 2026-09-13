"""Setting the robot's initial pose until the localizer has taken it.

A single ``/initialpose`` message is easily lost: the publisher is new and not
matched yet, or AMCL is still activating. :func:`confirm_initial_pose` keeps
publishing once per period until the localizer shows it took the pose, and gives
up with a clear message. It is plain Python (no ROS) so the loop is unit-tested
with fakes; the nav2 backend supplies the ROS callbacks.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from .types import StepTimeout, TaskCanceled

__all__ = ["INITIAL_POSE_COVARIANCE", "confirm_initial_pose"]

#: Diagonal of the 6x6 pose covariance sent with /initialpose (x, y, yaw), the
#: values RViz's "2D Pose Estimate" uses: 0.5 m and about 15 degrees std dev.
INITIAL_POSE_COVARIANCE: tuple[float, float, float] = (0.25, 0.25, math.radians(15.0) ** 2)


def confirm_initial_pose(
    publish: Callable[[], None],
    confirmed: Callable[[float], bool],
    *,
    period_s: float = 1.0,
    timeout_s: float = 30.0,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.05,
) -> int:
    """Publish, then re-publish every ``period_s`` until ``confirmed(t_first)``
    is true, where ``t_first`` is the ``clock()`` time of the first publish.

    Returns the number of messages published. Raises :class:`StepTimeout` after
    ``timeout_s`` and :class:`TaskCanceled` when ``should_stop()`` turns true.
    Runs on a worker thread (it sleeps)."""
    t_first = clock()
    publish()
    count = 1
    while True:
        if should_stop():
            raise TaskCanceled()
        if confirmed(t_first):
            return count
        now = clock()
        if now - t_first >= timeout_s:
            raise StepTimeout(f"the localizer did not confirm the initial pose within {timeout_s:g} s ({count} messages sent on /initialpose)")
        if now >= t_first + count * period_s:  # on a fixed 1 s grid, no drift
            publish()
            count += 1
        sleep(poll_s)

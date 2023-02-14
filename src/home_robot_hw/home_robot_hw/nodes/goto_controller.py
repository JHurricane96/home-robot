#!/usr/bin/env python
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import logging
from typing import List, Optional
import threading

import numpy as np
import sophus as sp
import rospy
from std_srvs.srv import Trigger, TriggerResponse
from std_srvs.srv import SetBool, SetBoolResponse
from geometry_msgs.msg import Twist, Pose, PoseStamped
from nav_msgs.msg import Odometry

from home_robot.agent.control.velocity_controllers import DDVelocityControlNoplan
from home_robot.utils.geometry import xyt_global_to_base, sophus2xyt, xyt2sophus
from home_robot_hw.ros.utils import matrix_from_pose_msg
from home_robot_hw.ros.visualizer import Visualizer


log = logging.getLogger(__name__)

CONTROL_HZ = 20


class GotoVelocityController:
    """
    Self-contained controller module for moving a diff drive robot to a target goal.
    Target goal is update-able at any given instant.
    """

    def __init__(
        self,
        hz: float,
        odom_only_feedback: bool = True,
    ):
        self.hz = hz
        self.odom_only = odom_only_feedback

        # Control module
        self.control = DDVelocityControlNoplan(hz)

        # Initialize
        self.xyt_loc = np.zeros(3)
        self.xyt_loc_odom = np.zeros(3)
        self.xyt_goal: Optional[np.ndarray] = None

        self.active = False
        self.track_yaw = True

        # Visualizations
        self.goal_visualizer = Visualizer("goto_controller/goal_abs")

    def _pose_update_callback(self, msg: PoseStamped):
        pose_sp = sp.SE3(matrix_from_pose_msg(msg.pose))
        self.xyt_loc = sophus2xyt(pose_sp)

    def _odom_update_callback(self, msg: Odometry):
        pose_sp = sp.SE3(matrix_from_pose_msg(msg.pose.pose))
        self.xyt_loc_odom = sophus2xyt(pose_sp)

    def _goal_update_callback(self, msg: Pose):
        pose_sp = sp.SE3(matrix_from_pose_msg(msg))

        """
        if self.odom_only:
            # Project absolute goal from current odometry reading
            pose_delta = xyt2sophus(self.xyt_loc_odom).inverse() * pose_sp
            pose_goal = xyt2sophus(self.xyt_loc_odom) * pose_delta
        else:
            # Assign absolute goal directly
            pose_goal = pose_sp
        """

        pose_goal = pose_sp

        self.xyt_goal = sophus2xyt(pose_goal)

        # Visualize
        self.goal_visualizer(
            (
                xyt2sophus(self.xyt_loc)
                * xyt2sophus(self.xyt_loc_odom).inverse()
                * pose_goal
            ).matrix()
        )

    def _enable_service(self, request):
        self.active = True
        return TriggerResponse(
            success=True,
            message=f"Goto controller is now RUNNING",
        )

    def _disable_service(self, request):
        self.active = False
        return TriggerResponse(
            success=True,
            message=f"Goto controller is now STOPPED",
        )

    def _set_yaw_tracking_service(self, request: SetBool):
        self.track_yaw = request.data
        status_str = "ON" if self.track_yaw else "OFF"
        return SetBoolResponse(
            success=True,
            message=f"Yaw tracking is now {status_str}",
        )

    def _compute_error_pose(self):
        """
        Updates error based on robot localization
        """
        xyt_loc = self.xyt_loc_odom if self.odom_only else self.xyt_loc
        xyt_err = xyt_global_to_base(self.xyt_goal, xyt_loc)
        # TODO: remove debug code
        # print(">>> err =", xyt_err[2], "=", xyt_loc[2], self.xyt_goal[2])
        if not self.track_yaw:
            xyt_err[2] = 0.0
        else:
            xyt_err[2] = (xyt_err[2] + np.pi) % (2 * np.pi) - np.pi

        return xyt_err

    def _set_velocity(self, v_m, w_r):
        cmd = Twist()
        cmd.linear.x = v_m
        cmd.angular.z = w_r
        self.vel_command_pub.publish(cmd)

    def _run_control_loop(self):
        rate = rospy.Rate(self.hz)

        while not rospy.is_shutdown():
            if self.active and self.xyt_goal is not None:
                # Get state estimation
                xyt_err = self._compute_error_pose()

                # Compute control
                v_cmd, w_cmd = self.control(xyt_err)

                # Command robot
                self._set_velocity(v_cmd, w_cmd)

            # Spin
            rate.sleep()

    def main(self):
        # ROS comms
        rospy.init_node("goto_controller")

        self.vel_command_pub = rospy.Publisher("stretch/cmd_vel", Twist, queue_size=1)

        rospy.Subscriber(
            "state_estimator/pose_filtered",
            PoseStamped,
            self._pose_update_callback,
            queue_size=1,
        )
        rospy.Subscriber(
            "odom",
            Odometry,
            self._odom_update_callback,
            queue_size=1,
        )
        rospy.Subscriber(
            "goto_controller/goal", Pose, self._goal_update_callback, queue_size=1
        )

        rospy.Service("goto_controller/enable", Trigger, self._enable_service)
        rospy.Service("goto_controller/disable", Trigger, self._disable_service)
        rospy.Service(
            "goto_controller/set_yaw_tracking", SetBool, self._set_yaw_tracking_service
        )

        # Run controller
        log.info("Goto Controller launched.")
        self._run_control_loop()


if __name__ == "__main__":
    GotoVelocityController(CONTROL_HZ).main()
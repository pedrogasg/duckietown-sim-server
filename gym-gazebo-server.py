#!/usr/bin/env python2

import subprocess
import cv2
import zmq
import time
import rospy
from cv_bridge import CvBridge, CvBridgeError
from gazebo_msgs.srv import GetModelState, SetModelState
from gazebo_stuff.model_state import State
from geometry_msgs.msg import Twist
from sensor_msgs.msg import Image, CompressedImage
from nav_msgs.msg import Odometry
from std_srvs.srv import Empty
import numpy as np

import pygazebo
import pygazebo.msg.world_control_pb2
import trollius
from trollius import From

SERVER_PORT = 7777

# Camera image size
CAMERA_WIDTH = 64
CAMERA_HEIGHT = 64

# Camera image shape
IMG_SHAPE = (CAMERA_WIDTH, CAMERA_HEIGHT, 3)

TIME_STEP_LENGTH = 100


import signal
import sys
def signal_handler(signal, frame):
    print ("exiting")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def sendArray(socket, array):
    """Send a numpy array with metadata over zmq"""
    md = dict(
        dtype=str(array.dtype),
        shape=array.shape,
    )
    # SNDMORE flag specifies this is a multi-part message
    socket.send_json(md, flags=zmq.SNDMORE)
    return socket.send(array, flags=0, copy=True, track=False)


print('Starting up')
context = zmq.Context()
socket = context.socket(zmq.PAIR)
socket.bind("tcp://*:%s" % SERVER_PORT)

bridge = CvBridge()

last_good_img = None


class ImageStuff():
    def __init__(self):
        self.last_good_img = None

    def image_callback(self, msg):
        # print("Received an image!")
        # setattr(msg, 'encoding', '')

        try:
            # Convert your ROS Image message to OpenCV2
            cv2_img = bridge.imgmsg_to_cv2(msg, "bgr8")
        except CvBridgeError, e:
            print(e)
        else:
            #
            # cv2.imwrite('camera_image.jpeg', cv2_img)
            self.last_good_img = cv2_img

class OdomData():
    def __init__(self):
        self.position = (0, 0 ,0)
        self.orientation = (0, 0, 0, 0)

    def odom_callback(self, msg):
        pos = msg.pose.pose.position
        quat = msg.pose.pose.orientation
        self.position = (pos.x, pos.y, pos.z)
        self.orientation = (quat.x, quat.y, quat.z, quat.w)

imagestuff = ImageStuff()
odom_data = OdomData()

rospy.init_node('gym', anonymous=True)
vel_pub = rospy.Publisher('/cmd_vel', Twist, queue_size=5)
unpause = rospy.ServiceProxy('/gazebo/unpause_physics', Empty)
pause = rospy.ServiceProxy('/gazebo/pause_physics', Empty)
# reset_proxy = rospy.ServiceProxy('/gazebo/reset_world', Empty)
get_state_proxy = rospy.ServiceProxy('/gazebo/get_model_state', GetModelState)
set_state_proxy = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)
image_topic = "/duckiebot/camera1/image_raw"
img_sub = rospy.Subscriber(image_topic, Image, imagestuff.image_callback)
odom_sub = rospy.Subscriber('odom', Odometry, odom_data.odom_callback)






@trollius.coroutine
def connect_loop():
    global publisher

    manager = yield From(pygazebo.connect())

    publisher = yield From(
        manager.advertise('/gazebo/default/world_control', 'gazebo.msgs.WorldControl')
    )

@trollius.coroutine
def step_loop():
    message = pygazebo.msg.world_control_pb2.WorldControl()
    message.multi_step = 10

    print('publish')
    publisher.publish(message)

loop = trollius.get_event_loop()
loop.run_until_complete(connect_loop())








# waiting for ROS to connect... TODO solve this with ROS callback
time.sleep(2)

# Store the initial robot state
init_state = State.get_state(get_state_proxy, "mybot", "world")

vanilla_state = State()
vanilla_state.reference_frame = "world"
vanilla_state.model_name = "mybot"
vanilla_state.pose.position.x = 1 # IDK why we are starting at (1,1,0), but Yanjun put the road here
vanilla_state.pose.position.y = 1
vanilla_state.pose.position.z = .04 # otherwise duckie will fly off if z=0
vanilla_state.pose.orientation.x = 0
vanilla_state.pose.orientation.y = 0
vanilla_state.pose.orientation.z = 1


def reset_alt():
    print ("Resetting world")

    # set_state_proxy(init_state)
    set_state_proxy(vanilla_state)

    vel_cmd = Twist()
    vel_cmd.linear.x =0
    vel_cmd.angular.z = 0
    vel_pub.publish(vel_cmd)

    # TODO: reset duckie positions, or better yet, make the duckies immovable


def poll_socket(socket, timetick = 10):
    poller = zmq.Poller()
    poller.register(socket, zmq.POLLIN)
    # wait up to 10msec
    try:
        print("poller ready")
        while True:
            obj = dict(poller.poll(timetick))
            if socket in obj and obj[socket] == zmq.POLLIN:
                yield socket.recv_json()
    except KeyboardInterrupt:
        print ("stopping server")
        quit()

def handle_message(msg):
    if msg['command'] == 'reset':
        print('resetting the simulation')
        # reset_proxy()
        reset_alt()
        # let it stabilize # temporary fix for duckiebot being too low
        # state = State.get_state(get_state_proxy, "mybot", "world")
        # execute 100 steps (.1 sim second, for stability)
        subprocess.call(["gz", "world", "-m", "100"])

    elif msg['command'] == 'action':
        print('received motor velocities')
        print(msg['values'])

        vel_cmd = Twist()
        left, right = tuple(msg['values'])

        if (left > 0 and right > 0) or (left < 0 and right < 0):
            vel_cmd.linear.x = 0.3 * left
            vel_cmd.angular.z = 0
        else:
            vel_cmd.linear.x = 0.05
            vel_cmd.angular.z = 0.3 * right

        vel_pub.publish(vel_cmd)

        startTime = time.time()

        #subprocess.call(["gz", "world", "-m", str(TIME_STEP_LENGTH)])

        loop.run_until_complete(step_loop())


        endTime = time.time()
        callTime = (endTime - startTime) * 1000
        print('gz call time: %.1f ms' % callTime)

    else:
        assert False, "unknown command"

    # Send world position data, etc
    # Note: the Gym client needs this to craft a reward function
    socket.send_json(
        {
            # XYZ position
            "position": odom_data.position,

            # XYZW quaternion
            "orientation": odom_data.orientation
        },
        flags=zmq.SNDMORE
    )

    # only resize when we need
    img = cv2.resize(imagestuff.last_good_img, (CAMERA_WIDTH, CAMERA_HEIGHT))

    # BGR to RGB
    img = img[:, :, ::-1]

    # to contiguous, otherwise ZMQ will complain
    img = np.ascontiguousarray(img, dtype=np.uint8)

    sendArray(socket, img)


for message in poll_socket(socket):
    handle_message(message)

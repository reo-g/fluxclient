# !/usr/bin/env python3

import os
import sys
import io
from math import pi, sin, cos, sqrt, degrees
import time
import datetime

from PIL import Image
import numpy as np


class LaserBase(object):
    """base class for all laser usage calss"""
    def __init__(self):
        self.laser_on = False
        self.focal_l = 12.0  # focal z coordinate

        self.laser_speed = 300  # speed F= mm/minute
        self.travel_speed = 1000
        self.draw_power = 255  # drawing
        self.fram_power = 30  # indicating

        self.obj_height = 10.9  # rubber
        self.obj_height = 3.21  # wood
        self.obj_height = 1.7  # pcb
        self.obj_height = 0.0  # plate

        self.pixel_per_mm = 8  # sample rate for each point
        self.radius = 85  # laser max radius = 85mm

        # list holding current image
        self.reset_image()

        # warning global setting, don't use theese unless you 100% understand what you are doing
        self.rotation = 0
        self.ratio = 1.

    def reset_image(self):
        w = self.pixel_per_mm * self.radius * 2
        self.image_map = np.ones((w, w), np.uint8) * 255

    def header(self, header):
        """
        header gcode for laser
        """
        gcode = []

        # header part
        gcode.append(";Generate by Flux Studio %s" % (datetime.datetime.fromtimestamp(time.time()).strftime('on %Y-%m-%d at %H:%M:%S')))
        gcode.append(";Laser Gcode")
        for i in header.split('\n'):
            gcode.append(";" + i)

        # force close laser
        self.laser_on = True
        gcode += self.turnOff()

        # setting
        gcode += ["X3F3", "X3F2", "X3F1"]

        # home
        gcode.append("G28")

        # move to proper height
        gcode.append("G1 F5000 Z%.5f" % (self.focal_l + self.obj_height))
        return gcode

    def turnOn(self):
        if self.laser_on is True:
            return []
        self.laser_on = True
        return ["M400", "X2O%d;turnOn" % self.draw_power, "G4 P20"]

    def turnOff(self):
        if self.laser_on is False:
            return []
        self.laser_on = False
        return ["M400", "X2O0;turnOff", "G4 P20"]

    def turnTo(self, power=None):
        if power is None:
            self.laser_on = True
            return ["M400", "X2O%d;turnTo %d" % (self.fram_power, self.fram_power), "G4 P20"]

        elif power != 0:
            self.laser_on = True
            return ["M400", "X2O%d;turnTo %d" % (power, power), "G4 P20"]

        elif power == 0:
            return self.turnOff()

    def moveTo(self, x, y, speed=None, z=None, ending=None):
        """
            apply global "rotation" and "scale"
            move to position x,y
        """

        x2 = (x * cos(self.rotation) - y * sin(self.rotation)) * self.ratio
        y2 = (x * sin(self.rotation) + y * cos(self.rotation)) * self.ratio

        x = x2
        y = y2

        if speed is None:
            speed = self.laser_speed

        if ending is None:
            if self.laser_on:
                ending = ';draw'
            else:
                ending = ';move'

        self.current_x = x
        self.current_y = y
        if z is None:
            return ["G1 F%.5f X%.5f Y%.5f %s" % (speed, x, y, ending)]
        else:
            return ["G1 F%.5f X%.5f Y%.5f Z%.5f %s" % (speed, x, y, z, ending)]

    def drawTo(self, x, y, speed=None, z=None):
        """
            turn on, move to x, y

            draw to position x,y
        """
        gcode = []
        gcode += self.turnOn()

        if speed is None:
            gcode += self.moveTo(x, y, self.laser_speed, z, ending=';draw')
        else:
            gcode += self.moveTo(x, y, speed, z, ending=';draw')

        return gcode

    def closeTo(self, x, y, speed=None, z=None):
        """
            turn off, move to x, y
        """
        gcode = []
        gcode += self.turnOff()

        if speed is None:
            gcode += self.moveTo(x, y, self.travel_speed, z)
        else:
            gcode += self.moveTo(x, y, speed, z)
        return gcode

    def to_image(self, buffer_data, img_width, img_height):
        """
        convert buffer_data(bytes) to a 2d array
        """
        int_data = list(buffer_data)
        assert len(int_data) == img_width * img_height, "data length != width * height, %d != %d * %d" % (len(int_data), img_width, img_height)
        image = [int_data[i * img_width: (i + 1) * img_width] for i in range(img_height)]

        return image

    def gcode_generate(self):
        """Virtual function gcode_generate"""
        raise NotImplementedError('Successor didn\'t implement "gcode_generate" method')

    def export_to_stream(self, stream, *args):
        stream.write(self.gcode_generate(*args))

    def set_params(self, key, value):
        if key == 'object_height':
            self.obj_height = float(value)

        elif key == 'laser_speed':
            self.laser_speed = float(value) * 60  # mm/s -> mm/min

        elif key == 'power':
            self.draw_power = (round(float(value) * 255))  # pwm, int

        elif key == 'shading':
            self.shading = int(value) == 1
        else:
            raise ValueError('undefine setting key')

    def rotate(self, x, y, rotation, cx=0., cy=0.):
        """
        compute new (x, y) after rotate toward (cx, cy)
        """
        vx = (x - cx)
        vy = (y - cy)
        x = cx + vx * cos(rotation) - vy * sin(rotation)
        y = cy + vx * sin(rotation) + vy * cos(rotation)
        return x, y

    def add_image(self, buffer_data, img_width, img_height, x1, y1, x2, y2, rotation, thres=255):
        """
        add image on top of current image i.e self.image_map
          parameters:
            buffer_data: image data in bytes array
            img_width, img_height: trivial
            x1, y1: absolute position of image's top-left corner after rotation
            x2, y2: absolute position of image's button_right corner after rotation
          return:
            None
        """
        pix = Image.frombytes('L', (img_width, img_height), buffer_data)

        # image center (rotation center)
        cx = (x1 + x2) / 2.
        cy = (y1 + y2) / 2.

        # compute four original corner
        ox1, oy1 = self.rotate(x1, y1, -rotation, cx, cy)
        ox3, oy3 = self.rotate(x2, y2, -rotation, cx, cy)

        ox2, oy2 = ox1, oy3
        ox4, oy4 = ox3, oy1

        # rotate four corner
        ox1, oy1 = self.rotate(ox1, oy1, rotation, cx, cy)
        ox2, oy2 = self.rotate(ox2, oy2, rotation, cx, cy)
        ox3, oy3 = self.rotate(ox3, oy3, rotation, cx, cy)
        ox4, oy4 = self.rotate(ox4, oy4, rotation, cx, cy)

        # find upper-left corner after rotation(edge)
        gx1 = min(ox1, ox2, ox3, ox4)
        gy1 = max(oy1, oy2, oy3, oy4)
        gy1_on_map = round((gx1 / self.radius * len(self.image_map) / 2.) + (len(self.image_map) / 2.))
        gx1_on_map = round(-(gy1 / self.radius * len(self.image_map) / 2.) + (len(self.image_map) / 2.))

        gx2 = max(ox1, ox2, ox3, ox4)
        gy2 = min(oy1, oy2, oy3, oy4)
        gy2_on_map = round((gx2 / self.radius * len(self.image_map) / 2.) + (len(self.image_map) / 2.))
        gx2_on_map = round(-(gy2 / self.radius * len(self.image_map) / 2.) + (len(self.image_map) / 2.))

        # shrink size if image too big, to avoid white frame disappear
        if pix.size[0] >= len(self.image_map) or pix.size[1] >= len(self.image_map):
            if pix.size[0] >= pix.size[1]:
                new_size = (len(self.image_map), len(self.image_map) * pix.size[1] // pix.size[0])
            else:
                new_size = (len(self.image_map) * pix.size[0] // pix.size[1], len(self.image_map))
            pix = pix.resize(new_size)

        # add white frame on each side
        new_pix = Image.new('L', (pix.size[0] + 2, pix.size[1] + 2), 255)
        new_pix.paste(pix, (1, 1))
        new_pix = new_pix.rotate(degrees(rotation), expand=1)
        new_pix = new_pix.resize((gy2_on_map - gy1_on_map, gx2_on_map - gx1_on_map))

        for h in range(new_pix.size[0]):
            # using white frame to find starting and ending index
            flag = False
            for find_s in range(new_pix.size[1]):
                if new_pix.getpixel((h, find_s)) > 0:
                    find_s += 1
                    flag = True
                    break
            if not flag:
                find_s = 0

            flag = False
            for find_e in range(new_pix.size[1] - 1, -1, -1):
                if new_pix.getpixel((h, find_e)) > 0:
                    break
            if not flag:
                find_e = new_pix.size[1]

            for w in range(find_s, find_e):
                if (gx1_on_map + w - len(self.image_map) / 2.) ** 2 + (gy1_on_map + h - len(self.image_map) / 2.) ** 2 < (len(self.image_map) / 2.) ** 2:
                    if new_pix.getpixel((h, w)) <= thres:
                        self.image_map[gx1_on_map + w][gy1_on_map + h] = new_pix.getpixel((h, w))

    def dump(self, file_name, mode='save'):
        """
            dump the image of this laser class

        """
        img = Image.fromarray(self.image_map)
        if mode == 'save':
            img.save(file_name, 'png')
            return
        elif mode == 'preview':
            # get the preview (640 * 640) png in bytes
            img = img.resize(640, 640)

            b = io.BytesIO()
            img.save(b, 'png')
            image_bytes = b.getvalue()
            return image_bytes
        else:
            print("unsupport mode %s" % mode, file=sys.stderr)
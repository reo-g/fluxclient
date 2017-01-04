
from collections import deque
from threading import Semaphore, Lock
from struct import Struct
from errno import errorcode, ETIMEDOUT
from uuid import UUID
import logging
import msgpack

import usb.core
import usb.util

from fluxclient.utils.version import StrictVersion
from fluxclient import __version__

logger = logging.getLogger(__name__)
HEAD_PACKER = Struct("<HB")
ID_VENDOR = 0xffff
ID_PRODUCT = 0xfd00


def match_direction(direction):
    def wrapper(ep):
        return usb.util.endpoint_direction(ep.bEndpointAddress) == direction
    return wrapper


class USBProtocol(object):
    running = False
    session = None
    _flag = 0
    _buf = b""

    @classmethod
    def get_interfaces(cls):
        return list(usb.core.find(idVendor=ID_VENDOR, idProduct=ID_PRODUCT,
                                  find_all=True))

    def __init__(self, usbdev):
        self._usbdev = dev = usbdev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except NotImplementedError:
            pass

        dev.set_configuration()
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]

        try:
            self._rx = usb.util.find_descriptor(
                intf, bmAttributes=0x2,
                custom_match=match_direction(usb.util.ENDPOINT_IN))

            self._tx = usb.util.find_descriptor(
                intf, bmAttributes=0x2,
                custom_match=match_direction(usb.util.ENDPOINT_OUT))
            logger.info("Host2Host USB device opened")

            while self._recv(1024, timeout=0.05):
                pass  # Clean all data in buffer

            self.do_handshake()
            self.chl_semaphore = Semaphore(0)
            self.chl_open_mutex = Lock()
            self.channels = {}
        except Exception:
            usb.util.dispose_resources(self._usbdev)
            raise

    def _send(self, buf):
        # Low level send
        try:
            self._tx.write(buf)
        except usb.core.USBError as e:
            if e.errno == ETIMEDOUT:
                raise FluxUSBError(*e.args, symbol=("TIMEOUT", ))
            else:
                raise FluxUSBError(*e.args,
                                   symbol=("UNKNOWN_ERROR",
                                           errorcode.get(e.errno, e.errno)))

    def _send_binary_ack(self, channel_idx):
        self._send(HEAD_PACKER.pack(4, channel_idx) + b"\x80")

    def _recv(self, length, timeout=0.001):
        # Low level recv
        try:
            # note: thread will dead lock if tiemout is 0
            return self._rx.read(length, int(timeout * 1000)).tobytes()
        except usb.core.USBError as e:
            if e.errno == ETIMEDOUT or e.backend_error_code == -116:
                return b""
            else:
                raise FluxUSBError(*e.args)

    def _feed_buffer(self, timeout=0.05):
        self._buf += self._recv(1024, timeout=0.05)

    def _unpack_buffer(self):
        l = len(self._buf)
        if l > 3:
            size, channel_idx = HEAD_PACKER.unpack(self._buf[:3])
            if size == 0:
                self._buf = self._buf[2:]
                return -1, None, None
            if l >= size:
                fin = self._buf[size - 1]
                buf = self._buf[3:size - 1]
                self._buf = self._buf[size:]
                return channel_idx, buf, fin
        return None, None, None

    def _handle_handshake(self, buf):
        data = msgpack.unpackb(buf, use_list=False, encoding="utf8",
                               unicode_errors="ignore")
        session = data.pop("session", "?")
        if self.session is None:
            logger.info("Get handshake session: %s", session)
        else:
            logger.info("Replace handshake session: %s", session)

        self.endpoint_profile = data
        self.session = session
        self.send_object(0xfe, {"session": self.session,
                                "client": "fluxclient-%s" % __version__})

    def _final_handshake(self, buf):
        data = msgpack.unpackb(buf, use_list=False, encoding="utf8",
                               unicode_errors="ignore")
        if data["session"] == self.session:
            self.uuid = UUID(hex=self.endpoint_profile["uuid"])
            self.serial = self.endpoint_profile["serial"]
            self.version = StrictVersion(self.endpoint_profile["version"])
            self.model_id = self.endpoint_profile["model"]
            self.nickname = self.endpoint_profile["nickname"]

            self._flag |= 1
            logger.info("Host2Host USB Connected")
            logger.debug("Serial: %s {%s}\nModel: %s\nName: %s\n",
                         self.serial, self.uuid, self.model_id, self.nickname)
            return True
        else:
            logger.info("USB final handshake error with wrong session "
                        "recv=%i, except=%i", data["session"], self.session)
            self.session = None
            return False

    def do_handshake(self):
        ttl = 5
        self._usbdev.ctrl_transfer(0x40, 0xFD, 0, 0)
        self.send_object(0xfc, None)  # Request handshake
        while ttl:
            self._feed_buffer(timeout=0.3)
            data = None

            while True:
                d = self._unpack_buffer()
                if d[0] is None:
                    break
                else:
                    data = d

            if data and data[0] is not None:
                channel_idx, buf, fin = data
                if channel_idx == 0xff and fin == 0xf0:
                    self._handle_handshake(buf)
                    continue
                elif channel_idx == 0xfd and fin == 0xf0:
                    if self.session is not None:
                        if self._final_handshake(buf):
                            return True
                    else:
                        logger.warning("Recv unexcept final handshake")
                elif channel_idx == -1:
                    logger.warning("Recv 0")
                else:
                    logger.warning("Recv unexcept channel idx %r and fin "
                                   "%r in handshake", channel_idx, fin)
            else:
                logger.info("Handshake timeout, retry")

            self.send_object(0xfc, None)  # Request handshake
            ttl -= 1
        raise FluxUSBError("Handshake failed.", symbol=("TIMEOUT", ))

    def run_once(self):
        self._feed_buffer()
        channel_idx, buf, fin = self._unpack_buffer()
        if channel_idx is None:
            return
        elif channel_idx == -1:
            raise FluxUSBError("USB protocol broken")
        elif channel_idx < 0x80:
            channel = self.channels.get(channel_idx)
            if channel is None:
                raise FluxUSBError("Recv bad channel idx 0x%02x" % channel_idx)
            if fin == 0xf0:
                channel.on_object(msgpack.unpackb(buf, encoding="utf8",
                                  unicode_errors="ignore"))
            elif fin == 0xff:
                self._send_binary_ack(channel_idx)
                channel.on_binary(buf)
            elif fin == 0xc0:
                channel.on_binary_ack()
            else:
                raise FluxUSBError("Recv bad fin 0x%02x" % fin)
        elif channel_idx == 0xf1:
            if fin != 0xf0:
                raise FluxUSBError("Recv bad fin 0x%02x" % fin)
            self._on_channel_ctrl_response(msgpack.unpackb(buf))
        else:
            self.stop()
            self.close()
            raise FluxUSBError("Recv bad control channel 0x%02x" % channel_idx)

    def run(self):
        try:
            self._flag |= 2
            while self._flag == 3:
                self.run_once()
        except FluxUSBError as e:
            logger.error("USB Error: %s", e)
            self._flag = 0
            self.close()
        except Exception:
            logger.exception("Unknown error")
            self._flag = 0
            self.close()
            raise

    def stop(self):
        self._flag &= ~2

    def close(self):
        self.send_object(0xfc, None)
        for idx, channel in self.channels.items():
            channel.close(directly=True)
        logger.info("Host2Host closed")
        usb.util.dispose_resources(self._usbdev)

    def send_object(self, channel, obj):
        payload = msgpack.packb(obj)
        buf = HEAD_PACKER.pack(len(payload) + 4, channel) + payload + b"\xb0"
        self._send(buf)

    def send_binary(self, channel, buf):
        buf = HEAD_PACKER.pack(len(buf) + 4, channel) + buf + b"\xbf"
        self._send(buf)

    def _on_channel_ctrl_response(self, obj):
        index = obj.get(b"channel")
        status = obj.get(b"status")
        action = obj.get(b"action")
        if action == b"open":
            if status == b"ok":
                self.channels[index] = Channel(self, index)
                self.chl_semaphore.release()
                logger.info("Channel %i opened", index)
            else:
                logger.error("Channel %i open failed", index)
        elif action == b"close":
            if status == b"ok":
                self.channels.pop(index)
                logger.info("Channel %i closed", index)
            else:
                logger.error("Channel %i close failed", index)
        else:
            logger.error("Unknown channel action: %r", action)

    def _close_channel(self, channel):
        logger.info("Close channel %i", channel.index)
        self.send_object(0xf0, {"channel": channel.index, "action": "close"})

    def open_channel(self, channel_type="robot", timeout=10.0):
        # Send request
        with self.chl_open_mutex:
            idx = None
            for i in range(len(self.channels) + 1):
                if self.channels.get(i) is None:
                    idx = i
            logger.info("Request channel %i with type %s", idx, channel_type)
            self.send_object(0xf0, {"channel": idx, "action": "open",
                                    "type": channel_type})

            self.chl_semaphore.acquire(timeout=timeout)
            channel = self.channels.get(idx)
            if channel:
                return self.channels[idx]
            else:
                raise FluxUSBError("Channel creation failed")


class Channel(object):
    binary_stream = None

    def __init__(self, usbprotocol, index):
        self.index = index
        self.usbprotocol = usbprotocol
        self.obj_semaphore = Semaphore(0)
        self.buf_semaphore = Semaphore(0)
        self.ack_semaphore = Semaphore(0)
        self.objq = deque()
        self.bufq = deque()

        self.__opened = True

    def __del__(self):
        self.close()

    @property
    def alive(self):
        return self.__opened

    def close(self, directly=False):
        if self.__opened:
            self.__opened = False
            if directly is False:
                self.usbprotocol._close_channel(self)

    def on_object(self, obj):
        self.objq.append(obj)
        self.obj_semaphore.release()

    def on_binary(self, buf):
        if self.binary_stream:
            self.binary_stream.send(buf)
        else:
            self.bufq.append(buf)
            self.buf_semaphore.release()

    def get_buffer(self, timeout=10.0):
        if self.buf_semaphore.acquire(timeout=timeout) is False:
            raise FluxUSBError("Operation timeout", symbol=("TIMEOUT", ))
        return self.bufq.popleft()

    def on_binary_ack(self):
        self.ack_semaphore.release()

    def get_object(self, timeout=10.0):
        if self.obj_semaphore.acquire(timeout=timeout) is False:
            raise FluxUSBError("Operation timeout", symbol=("TIMEOUT", ))
        return self.objq.popleft()

    def send_object(self, obj):
        if self.__opened:
            self.usbprotocol.send_object(self.index, obj)
        else:
            raise FluxUSBError("Device is closed",
                               symbol=("DEVICE_ERROR", ))

    def send_binary(self, buf, timeout=10.0):
        if self.__opened:
            self.usbprotocol.send_binary(self.index, buf)
            if self.ack_semaphore.acquire(timeout=timeout) is False:
                raise FluxUSBError("Operation timeout", symbol=("TIMEOUT", ))
        else:
            raise FluxUSBError("Device is closed",
                               symbol=("DEVICE_ERROR", ))


class FluxUSBError(Exception):
    def __init__(self, *args, **kw):
        self.symbol = kw.get("symbol", ("UNKNOWN_ERROR", ))
        super().__init__(*args)

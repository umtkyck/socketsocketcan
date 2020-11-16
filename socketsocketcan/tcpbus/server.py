import socket
import sys
import argparse
import struct
import errno
from queue import Queue
from queue import Empty as QueueEmpty
from threading import Thread
import can
from time import sleep
import os

bustype = 'socketcan'
channel = 'can0' #This is for CAN Device on your linux operating system

class TCPBus(can.BusABC):
    FORMAT = "<IB3x8s"
    RECV_FRAME_SZ = 21
    CAN_EFF_FLAG = 0x80000000
    CAN_RTR_FLAG = 0x40000000
    CAN_ERR_FLAG = 0x20000000
    
    def __init__(self,port,hostname="",can_filters=None,**kwargs):
        super().__init__("whatever",can_filters)
        self.hostname = hostname #TODO: check validity. ping?
        self.port = port #TODO: check validity.
        self._is_connected = False
        self.recv_buffer = Queue()
        self.send_buffer = Queue()
        self._shutdown_flag = Queue()

        #open socket and wait for connection to establish.
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind((self.hostname, self.port))
        self._socket.listen()
        self._conn, addr = self._socket.accept()
        self._is_connected = True
        self._conn.settimeout(0.5) #blocking makes exiting an infinite loop hard
        
		#open can socket
        self.bus = can.interface.Bus(channel=channel, bustype=bustype)

        #now we're connected, kick off other threads.
        self._tcp_listener = Thread(target=self._poll_socket)
        self._tcp_listener.start()

        self._tcp_writer = Thread(target=self._poll_send)
        self._tcp_writer.start()

        self._canbus_poller = Thread(target=self._poll_receive)
        self._canbus_poller.start()

    def _recv_internal(self,timeout=None):
        #TODO: filtering shit
        return (self.recv_buffer.get(timeout=timeout), True)

    def send(self,msg):
        if msg.is_extended_id:
            msg.arbitration_id |= self.CAN_EFF_FLAG
        if msg.is_remote_frame:
            msg.arbitration_id |= self.CAN_RTR_FLAG
        if msg.is_error_frame:
            msg.arbitration_id |= self.CAN_ERR_FLAG        
        print("UMIT2: ")
        print(msg)
        self.send_buffer.put(msg)

    def _stop_threads(self):
        self._shutdown_flag.put(True)
        self._socket.close() #shutdown might be faster but can be ugly and raise an exception
        self._is_connected = False

    def shutdown(self):
        """gracefully close TCP connection and exit threads"""
        if self.is_connected:
            self._stop_threads()
        #can't join threads because this might be called from that thread, so just wait...
        while self._tcp_listener.is_alive() or self._tcp_writer.is_alive():
            sleep(0.005)

    @property
    def is_connected(self):
        """check that a TCP connection is active"""
        return self._is_connected

    def _msg_to_bytes(self,msg):
        """convert Message object to bytes to be put on TCP socket"""
        arb_id = msg.arbitration_id.to_bytes(4,"little") #TODO: masks
        dlc = msg.dlc.to_bytes(1,"little")
        data = msg.data + bytes(8-msg.dlc)
        return arb_id+dlc+data

    def _bytes_to_message(self,b):
        """convert raw TCP bytes to can.Message object"""
        ts = int.from_bytes(b[:4],"little") + int.from_bytes(b[4:8],"little")/1e6
        can_id = int.from_bytes(b[8:12],"little")
        dlc = b[12] #TODO: sanity check on these values in case of corrupted messages.

        #decompose ID
        is_extended = bool(can_id & self.CAN_EFF_FLAG) #CAN_EFF_FLAG
        if is_extended:
            arb_id = can_id & 0x1FFFFFFF
        else:
            arb_id = can_id & 0x000007FF
        
        return can.Message(
            timestamp = ts,
            arbitration_id = arb_id,
            is_extended_id = is_extended,
            is_error_frame = bool(can_id & self.CAN_ERR_FLAG), #CAN_ERR_FLAG
            is_remote_frame = bool(can_id & self.CAN_RTR_FLAG), #CAN_RTR_FLAG
            dlc=dlc,
            data=b[13:13+dlc]
        )

    def _poll_socket(self):
        """background thread to check for new CAN messages on the TCP socket"""
        part_formed_message = bytearray() # TCP transfer might off part way through sending a message
        with self._conn as conn:
            while self._shutdown_flag.empty():
                try:
                    data = conn.recv(self.RECV_FRAME_SZ * 20)
                except socket.timeout:
                    #no data, just try again.
                    continue
                except OSError:
                    # socket's been closed.
                    self._stop_threads()
                    break                

                if len(data):
                    # process the 1 or more messages we just received

                    if len(part_formed_message):
                        data = part_formed_message + data #add on the previous remainder
                    
                    #check how many whole and incomplete messages we got through.
                    num_incomplete_bytes = len(data) % self.RECV_FRAME_SZ
                    num_frames = len(data) // self.RECV_FRAME_SZ

                    #to pre-pend next time:
                    if num_incomplete_bytes:
                        part_formed_message = data[-num_incomplete_bytes:]
                    else:
                        part_formed_message = bytearray()

                    c = 0
                    #print("Operation Started")
                    for _ in range(num_frames):
                        var = self._bytes_to_message(data[c:c+self.RECV_FRAME_SZ])
                        print(var)
                        self.recv_buffer.put(var)
                        can_id = var.arbitration_id | self.CAN_EFF_FLAG
                        self.bus.send(var)
                        c += self.RECV_FRAME_SZ
                    #print("Operation Stopped")
                
                else:
                    #socket's been closed at the other end.
                    self._stop_threads()
                    break

    def _poll_send(self):
        """background thread to send messages when they are put in the queue"""
        with self._conn as s:
            while self._shutdown_flag.empty():
                try:
                    msg = self.send_buffer.get(timeout=0.02) # Check the timeouts for high rate msgs
                    data = self._msg_to_bytes(msg)
                    while not self.send_buffer.empty(): #we know there's one message, might be more.
                        data += self._msg_to_bytes(self.send_buffer.get())
                    try:
                        s.sendall(data)
                    except OSError:
                        # socket's been closed.
                        self._stop_threads()
                        break
                except QueueEmpty:
                    pass #NBD, just means nothing to send.

    def _poll_receive(self):
        """background thread to get messages from canbus"""
        with self._conn as s:
            while self._shutdown_flag.empty():
                try:
                    msg = self.bus.recv(timeout=0.02) # Check the timeouts for high rate msgs
                    if msg is not None:
                        print("Received: ")
                        print(msg)
                        self.send_buffer.put(msg)
                except:
                    pass #NBD, just means nothing to send.





        

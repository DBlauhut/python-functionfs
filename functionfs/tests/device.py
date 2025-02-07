# This file is part of python-functionfs
# Copyright (C) 2016-2021  Vincent Pelletier <plr.vincent@gmail.com>
#
# python-functionfs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# python-functionfs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with python-functionfs.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import print_function
import errno
import functools
import socket
import threading
import functionfs
import functionfs.ch9
from functionfs.gadget import (
    GadgetSubprocessManager,
    ConfigFunctionFFSSubprocess,
)
from . import common

FS_BULK_MAX_PACKET_SIZE = 64
HS_BULK_MAX_PACKET_SIZE = 512

class EPThread(threading.Thread):
    daemon = True

    def __init__(self, ep_file, method, **kw):
        super().__init__(**kw)
        self.__method = method
        self.ep_file = ep_file
        self.echo_buf = bytearray(512)
        self.__run_lock = run_lock = threading.Lock()
        run_lock.acquire()
        super().start()

    def start(self):
        ep_num = self.ep_file.getDescriptor().bEndpointAddress
        for offset in range(len(self.echo_buf)):
            self.echo_buf[offset] = ep_num
        if ep_num & functionfs.ch9.USB_DIR_IN:
            self.check = lambda: False
        else:
            def check():
                return ep_num != self.echo_buf[0]
            self.check = check
        self.__run_lock.release()

    def run(self):
        method = self.__method
        echo_buf = self.echo_buf
        run_lock = self.__run_lock
        while True:
            run_lock.acquire()
            check = self.check
            print(self.name, 'start')
            while True:
                try:
                    method(echo_buf)
                except IOError as exc:
                    if exc.errno == errno.ESHUTDOWN:
                        break
                    if exc.errno not in (errno.EINTR, errno.EAGAIN):
                        raise
                if check():
                    self.ep_file.halt()
            print(self.name, 'exit')
            run_lock.acquire(False)

class FunctionFSTestDevice(functionfs.Function):
    def __init__(self, path, ep_pair_count=15):
        ep_list = sum(
            [
                [
                    x | functionfs.ch9.USB_DIR_IN,
                    x | functionfs.ch9.USB_DIR_OUT,
                ]
                for x in range(1, ep_pair_count + 1)
            ],
            [],
        )
        INTERFACE_DESCRIPTOR = functionfs.getDescriptor(
            functionfs.USBInterfaceDescriptor,
            bInterfaceNumber=0,
            bAlternateSetting=0,
            bNumEndpoints=len(ep_list),
            bInterfaceClass=functionfs.ch9.USB_CLASS_VENDOR_SPEC,
            bInterfaceSubClass=0,
            bInterfaceProtocol=0,
            iInterface=1,
        )
        fs_list = [INTERFACE_DESCRIPTOR]
        hs_list = [INTERFACE_DESCRIPTOR]
        for endpoint in ep_list:
            fs_list.append(
                functionfs.getDescriptor(
                    functionfs.USBEndpointDescriptorNoAudio,
                    bEndpointAddress=endpoint,
                    bmAttributes=functionfs.ch9.USB_ENDPOINT_XFER_BULK,
                    wMaxPacketSize=FS_BULK_MAX_PACKET_SIZE,
                    bInterval=0,
                )
            )
            hs_list.append(
                functionfs.getDescriptor(
                    functionfs.USBEndpointDescriptorNoAudio,
                    bEndpointAddress=endpoint,
                    bmAttributes=functionfs.ch9.USB_ENDPOINT_XFER_BULK,
                    wMaxPacketSize=HS_BULK_MAX_PACKET_SIZE,
                    bInterval=0,
                )
            )
        super().__init__(
            path,
            fs_list=fs_list,
            hs_list=hs_list,
#            ss_list=DESC_LIST,
            lang_dict={
                0x0409: [
                    common.INTERFACE_NAME,
                ],
            },
        )
        self.__ep_count = len(ep_list)
        self.__echo_payload = b'NOT SET'
        self.__thread_list = []

    def __enter__(self):
        result = super().__enter__()
        assert len(self._ep_list) == self.__ep_count + 1
        thread_list = self.__thread_list
        for ep_file in self._ep_list[1:]:
            thread_list.append(
                EPThread(
                    name=ep_file.name,
                    ep_file=ep_file,
                    method=getattr(ep_file, 'readinto' if ep_file.readable() else 'write'),
                )
            )
        return result

    def onEnable(self):
        print('functionfs: ENABLE')
        try:
            print('Real interface 0:', self.ep0.getRealInterfaceNumber(0))
        except IOError:
            pass
        for ep_file in self._ep_list[1:]:
            print(ep_file.name + ':')
            descriptor = ep_file.getDescriptor()
            for klass in reversed(descriptor.__class__.mro()):
                for arg_id, _ in getattr(klass, '_fields_', ()):
                    print('  %s\t%s' % (
                        {
                            'b': '  0x%02x',
                            'w': '0x%04x',
                        }[arg_id[0]] % (getattr(descriptor, arg_id), ),
                        arg_id,
                    ))
            print('  FIFO status:', end='')
            try:
                value = ep_file.getFIFOStatus()
            except IOError as exc:
                print('(failed: %r)' % (exc, ))
            else:
                print(value)
            print('  Real number:', ep_file.getRealEndpointNumber())
            # XXX: can this raise if endpoint is not halted ?
            ep_file.clearHalt()
            ep_file.flushFIFO()
        for thread in self.__thread_list:
            thread.start()

    def onDisable(self):
        print('functionfs: DISABLE')

    def onBind(self):
        print('functionfs: BIND')

    def onUnbind(self):
        print('functionfs: UNBIND')

    def onSuspend(self):
        print('functionfs: SUSPEND')

    def onResume(self):
        print('functionfs: RESUME')

    def onSetup(self, request_type, request, value, index, length):
        request_type_type = request_type & functionfs.ch9.USB_TYPE_MASK
        if request_type_type == functionfs.ch9.USB_TYPE_VENDOR:
            if request == common.REQUEST_ECHO:
                if (request_type & functionfs.ch9.USB_DIR_IN) == functionfs.ch9.USB_DIR_IN:
                    self.ep0.write(self.__echo_payload[:length])
                elif length:
                    self.__echo_payload = self.ep0.read(length)
            else:
                print('functionfs: onSetup: halt')
                self.ep0.halt(request_type)
        else:
            super().onSetup(
                request_type, request, value, index, length,
            )

def main():
    parser = GadgetSubprocessManager.getArgumentParser(
        description='python-functionfs test gadget',
    )
    parser.add_argument(
        '--ep-count',
        type=int,
        default=15,
        help='Number of pairs (IN + OUT) of USB endpoints to request from '
        'UDC.',
    )
    args = parser.parse_args()
    def getConfigFunctionSubprocess(**kw):
        return ConfigFunctionFFSSubprocess(
            getFunction=functools.partial(
                FunctionFSTestDevice,
                ep_pair_count=args.ep_count,
            ),
            **kw
        )
    with GadgetSubprocessManager(
        args=args,
        config_list=[
            {
                'function_list': [
                    getConfigFunctionSubprocess,
                ],
                'MaxPower': 500,
                'lang_dict': {
                    0x409: {
                        'configuration': 'test',
                    },
                },
            }
        ],
        idVendor=0x1d6b, # Linux Foundation
        idProduct=0x0104, # Multifunction Composite Gadget
        lang_dict={
            0x409: {
                'serialnumber': '1234',
                'product': socket.gethostname(),
                'manufacturer': 'Foo Corp.',
            },
        },
    ) as gadget:
        # Note: events are not serviced in this process, but in the process
        # spawned by ConfigFunctionFFSSubprocess.
        print('Servicing functionfs events forever...')
        gadget.waitForever()

if __name__ == '__main__':
    main()

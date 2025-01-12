import attr
import typing
import asyncio
import logging
import itertools
import async_timeout

from collections import defaultdict

import zigpy_znp.commands
import zigpy_znp.types as t
import zigpy_znp.config as conf
import zigpy_znp.commands as c
from zigpy_znp.types import nvids

from zigpy_znp import uart
from zigpy_znp.commands import SysCommands, RPCErrorCommands
from zigpy_znp.frames import GeneralFrame
from zigpy_znp.exceptions import CommandNotRecognized, InvalidCommandResponse


LOGGER = logging.getLogger(__name__)


def _deduplicate_commands(commands):
    # Command matching as a relation forms a partially ordered set.
    # To avoid triggering our callbacks multiple times per packet, we
    # should remove redundant partial commands.
    maximal_commands = []

    for command in commands:
        for index, other_command in enumerate(maximal_commands):
            if other_command.matches(command):
                # If the other command matches us, we are redundant
                break
            elif command.matches(other_command):
                # If we match another command, we replace it
                maximal_commands[index] = command
                break
            else:
                # Otherwise, we keep looking
                pass  # pragma: no cover
        else:
            # If we matched nothing and nothing matched us, we extend the list
            maximal_commands.append(command)

    # The start of each chain is the maximal element
    return tuple(maximal_commands)


@attr.s(frozen=True)
class BaseResponseListener:
    matching_commands: typing.Tuple[t.CommandBase] = attr.ib(
        converter=_deduplicate_commands
    )

    @matching_commands.validator
    def check(self, attribute, commands):
        if not commands:
            raise ValueError("Listener must have at least one command")

    def matching_headers(self):
        return {response.header for response in self.matching_commands}

    def resolve(self, response: t.CommandBase) -> bool:
        if not any(c.matches(response) for c in self.matching_commands):
            return False

        if not self._resolve(response):
            return False

        return True

    def _resolve(self, response: t.CommandBase) -> bool:
        """
        Implemented by subclasses to handle matched commands.

        Return value indicates whether or not the listener has actually resolved,
        which can sometimes be unavoidable.
        """
        raise NotImplementedError()  # pragma: no cover

    def cancel(self):
        """
        Implement by subclasses to cancel the listener.

        Return value indicates whether or not the listener is cancelable.
        """
        raise NotImplementedError()  # pragma: no cover


@attr.s(frozen=True)
class OneShotResponseListener(BaseResponseListener):
    future: asyncio.Future = attr.ib(
        default=attr.Factory(lambda: asyncio.get_running_loop().create_future())
    )

    def _resolve(self, response: t.CommandBase) -> bool:
        if self.future.done():
            # This happens if the UART receives multiple packets during the same
            # event loop step and all of them match this listener. Our Future's
            # add_done_callback will not fire synchronously and thus the listener
            # is never properly removed. This isn't going to break anything.
            LOGGER.debug("Future already has a result set: %s", self.future)
            return False

        self.future.set_result(response)
        return True

    def cancel(self):
        if not self.future.done():
            self.future.cancel()

        return True


@attr.s(frozen=True)
class CallbackResponseListener(BaseResponseListener):
    callback: typing.Callable[[t.CommandBase], typing.Any] = attr.ib()

    def _resolve(self, response: t.CommandBase) -> bool:
        try:
            result = self.callback(response)

            # Run coroutines in the background
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            LOGGER.warning(
                "Caught an exception while executing callback", exc_info=True
            )

        # Returning False could cause our callback to be called multiple times in a row
        return True

    def cancel(self):
        # You can't cancel a callback
        return False


class ZNP:
    def __init__(self, config: conf.ConfigType):
        self._uart = None
        self._app = None
        self._config = config

        self._response_listeners = defaultdict(list)
        self._reconnect_task = None
        self._sync_request_lock = asyncio.Lock()

    def set_application(self, app):
        assert self._app is None
        self._app = app

    @property
    def _port_path(self) -> str:
        return self._config[conf.CONF_DEVICE][conf.CONF_DEVICE_PATH]

    async def connect(self) -> None:
        assert self._uart is None

        self._uart = await uart.connect(self._config[conf.CONF_DEVICE], self)
        LOGGER.debug("Testing connection to %s", self._uart.transport.serial.name)

        try:
            # Make sure that our port works
            await self.request(c.SysCommands.Ping.Req())
        except Exception:
            self._uart = None
            raise

        # XXX: To make sure we don't switch to the wrong device upon reconnect,
        #      update our config to point to the last-detected port.
        if self._config[conf.CONF_DEVICE][conf.CONF_DEVICE_PATH] == "auto":
            self._config[conf.CONF_DEVICE][
                conf.CONF_DEVICE_PATH
            ] = self._uart.transport.serial.name

        LOGGER.debug(
            "Connected to %s at %s baud",
            self._uart.transport.serial.name,
            self._uart.transport.serial.baudrate,
        )

    def _cancel_all_listeners(self) -> None:
        for header, listeners in self._response_listeners.items():
            for listener in listeners:
                listener.cancel()

    async def _reconnect(self) -> None:
        for attempt in itertools.count(start=1):
            LOGGER.debug(
                "Trying to reconnect to %s, attempt %d", self._port_path, attempt
            )

            try:
                self._cancel_all_listeners()

                await self.connect()
                await self._app.startup()

                return
            except Exception as e:
                LOGGER.error("Failed to reconnect", exc_info=e)
                await asyncio.sleep(
                    self._config[conf.CONF_ZNP_CONFIG][
                        conf.CONF_AUTO_RECONNECT_RETRY_DELAY
                    ]
                )

    def connection_lost(self, exc) -> None:
        LOGGER.debug("We were disconnected from %s: %s", self._port_path, exc)

        self._uart = None
        self._cancel_all_listeners()

        # exc=None means that the connection was closed
        if (
            not self._config[conf.CONF_ZNP_CONFIG][conf.CONF_AUTO_RECONNECT]
            or exc is None
        ):
            LOGGER.debug("Connection was purposefully closed. Not reconnecting.")
            return

        # Reconnect in the background using our previous port path
        # Note that this will reuse the same port as before
        LOGGER.debug("Starting background reconnection task")
        self._reconnect_task = asyncio.create_task(self._reconnect())

    def close(self) -> None:
        if self._uart is not None:
            self._uart.close()
            self._uart = None

        self._cancel_all_listeners()

        # Cancel any existing reconnect tasks, if any
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()

    def _remove_listener(self, listener: BaseResponseListener) -> None:
        LOGGER.trace("Removing listener %s", listener)

        for header in listener.matching_headers():
            self._response_listeners[header].remove(listener)

            if not self._response_listeners[header]:
                LOGGER.trace("Cleaning up empty listener list for header %s", header)
                del self._response_listeners[header]

        total_listeners = sum(len(l) for l in self._response_listeners.values())
        LOGGER.trace("There are %d listeners remaining", total_listeners)

    def frame_received(self, frame: GeneralFrame) -> None:
        """
        Called when a frame has been received.
        Can be called multiple times in a single event loop step.
        """

        command_cls = zigpy_znp.commands.COMMANDS_BY_ID[frame.header]
        command = command_cls.from_frame(frame)

        LOGGER.debug("Received command: %s", command)

        matched = False

        for listener in self._response_listeners[command.header]:
            if not listener.resolve(command):
                LOGGER.trace("%s does not match %s", command, listener)
                continue

            matched = True
            LOGGER.trace("%s matches %s", command, listener)

        if not matched:
            LOGGER.warning("Received an unhandled command: %s", command)

    def callback_for_responses(self, responses, callback) -> None:
        listener = CallbackResponseListener(responses, callback=callback)

        LOGGER.trace("Creating callback %s", listener)

        for header in listener.matching_headers():
            self._response_listeners[header].append(listener)

    def callback_for_response(self, response, callback) -> None:
        return self.callback_for_responses([response], callback)

    def wait_for_responses(self, responses) -> asyncio.Future:
        listener = OneShotResponseListener(responses)

        LOGGER.trace("Creating one-shot listener %s", listener)

        for header in listener.matching_headers():
            self._response_listeners[header].append(listener)

        # Remove the listener when the future is done, not only when it gets a result
        listener.future.add_done_callback(lambda _: self._remove_listener(listener))

        return listener.future

    def wait_for_response(self, response: t.CommandBase) -> asyncio.Future:
        return self.wait_for_responses([response])

    async def request(self, request, **response_params):
        if type(request) is not request.Req:
            raise ValueError(f"Cannot send a command that isn't a request: {request!r}")

        if request.Rsp:
            renamed_response_params = {}

            for param, value in response_params.items():
                if not param.startswith("Rsp"):
                    raise KeyError(
                        f"All response params must start with 'Rsp': {param!r}"
                    )

                renamed_response_params[param.replace("Rsp", "", 1)] = value

            # Construct our response before we send the request so that we fail early
            partial_response = request.Rsp(partial=True, **renamed_response_params)
        elif response_params:
            raise ValueError(
                f"Command has no response so response_params={response_params} "
                f"will have no effect"
            )

        LOGGER.debug("Sending request: %s", request)

        # If our request has no response, we cannot wait for one
        if not request.Rsp:
            LOGGER.debug("Request has no response, not waiting for one.")
            self._uart.send(request.to_frame())
            return

        # We should only be sending one SREQ at a time, according to the spec
        async with self._sync_request_lock:
            # We need to create the response listener before we send the request
            response_future = self.wait_for_responses(
                [
                    request.Rsp(partial=True),
                    RPCErrorCommands.CommandNotRecognized.Rsp(
                        partial=True, RequestHeader=request.header
                    ),
                ]
            )
            self._uart.send(request.to_frame())

            # We should get a SRSP in a reasonable amount of time
            async with async_timeout.timeout(
                self._config[conf.CONF_ZNP_CONFIG][conf.CONF_SREQ_TIMEOUT]
            ):
                # We lock until either a sync response is seen or an error occurs
                response = await response_future

        if isinstance(response, RPCErrorCommands.CommandNotRecognized.Rsp):
            raise CommandNotRecognized(f"Fatal request error: {response}")

        # If the sync response we got is not what we wanted, this is an error
        if not partial_response.matches(response):
            raise InvalidCommandResponse(
                f"SRSP was not what we expected: {response} !~ {partial_response}"
            )

        return response

    async def request_callback_rsp(self, *, request, callback, **response_params):
        """
        Sends a [SA]REQ request and waits for its AREQ response. A bug-free version of:

            req_rsp = await req
            callback_rsp = await req_callback
        """

        callback_response = self.wait_for_response(callback)
        response = self.request(request, **response_params)

        await response
        return await callback_response

    async def nvram_write(
        self, nv_id: nvids.BaseNvIds, value, *, offset: t.uint8_t = 0
    ):
        # While unpythonic, explicit type checking here means we can detect overflows
        if not isinstance(nv_id, nvids.BaseNvIds):
            raise ValueError(
                "The nv_id param must be an instance of BaseNvIds. "
                "Extend one of the tables in zigpy_znp.types.nvids."
            )

        if not isinstance(value, bytes):
            value = value.serialize()

        # Find the next NVID in the table to check that our write doesn't overflow
        # It's not foolproof, but it will catch simple mistakes
        all_enum_values = list(type(nv_id))
        all_enum_values.sort(key=lambda i: i.value)
        index = all_enum_values.index(nv_id)

        if index == len(all_enum_values) - 1:
            LOGGER.warning(
                "NVID is at end of table, cannot check for overflow"
            )  # pragma: no cover
        else:
            next_nvid = all_enum_values[index + 1]
            end_address = nv_id + offset + len(value)

            if end_address > next_nvid:
                raise ValueError("OSALNVWrite request overflows into %s", next_nvid)

        return await self.request(
            SysCommands.OSALNVWrite.Req(
                Id=nv_id, Offset=offset, Value=t.ShortBytes(value)
            ),
            RspStatus=t.Status.Success,
        )

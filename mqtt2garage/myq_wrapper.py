#!/usr/bin/env python
import asyncio
import datetime as dt
from typing import Dict, List, Optional, Union

from aiohttp import ClientSession
from asyncio_throttle import Throttler
from cached_property import cached_property
from pymyq import login
from pymyq.api import API
from pymyq.device import MyQDevice
from pymyq.errors import AuthenticationError, MyQError, RequestError
from pymyq.garagedoor import (
    STATE_OPEN,
    STATE_OPENING,
    STATE_CLOSED,
    STATE_CLOSING,
)

from mqtt2garage import log
from mqtt2garage.config import Cfg
from mqtt2garage.events import GarageStateEvent

logger = log.getLogger()


class RunState:
    def __init__(self):
        self.api: Optional[API] = None
        self.garage_doors: Dict[str, GarageDoor] = {}
        self.topics: Dict[str, str] = {}


class GarageDoor:
    _started = False
    # TODO(flaviof): hard coded limit throttle on how often we allow changing of state
    throttler = Throttler(rate_limit=4, period=66)

    def __init__(self, device: MyQDevice):
        self._device = device
        self._open_state: Optional[bool] = None
        self._last_request_ts = None

    @classmethod
    def is_started(cls):
        return cls._started

    @property
    def state_payload(self):
        # return self.state_name(self._open_state)
        return self._device.state

    @property
    def name(self) -> str:
        return self._device.name

    @property
    def device_id(self) -> str:
        return self._device.device_id

    def __str__(self):
        return f"'{self.name}' ({self.device_id})"

    @cached_property
    def topic(self) -> str:
        cfg = Cfg()
        suffix = cfg.alias.get(self.device_id) or self.device_id
        return f"{cfg.mqtt_topic}/{suffix}"

    @cached_property
    def topics(self) -> List[str]:
        cfg = Cfg()
        topics = set([f"{cfg.mqtt_topic}/{self.device_id}"])
        topics.add(self.topic)
        return list(topics)

    async def open_door(
        self, wait_for_state: bool = False
    ) -> Union[asyncio.Task, bool]:
        async with self.throttler:
            try:
                rc = await self._device.open(wait_for_state=wait_for_state)
                self._open_state = True
                self._last_request_ts = dt.datetime.now()
            except MyQError as e:
                logger.error(f"{self} unable to open_door: {e}")
            return rc

    async def close_door(
        self, wait_for_state: bool = False
    ) -> Union[asyncio.Task, bool]:
        async with self.throttler:
            try:
                rc = await self._device.close(wait_for_state=wait_for_state)
                self._open_state = False
                self._last_request_ts = dt.datetime.now()
            except MyQError as e:
                logger.error(f"{self} unable to close_door: {e}")
            return rc

    @staticmethod
    def state_from_name(is_open: Optional[str]) -> bool:
        # Note: Consider door open when state is STATE_CLOSING as well.
        return is_open in (STATE_OPEN, STATE_OPENING, STATE_CLOSING)

    @staticmethod
    def state_name(is_open: Optional[bool]) -> str:
        if is_open is None:
            return "¯\\_(ツ)_/¯"
        return STATE_OPEN if is_open else STATE_CLOSED

    @staticmethod
    def state_is_toggle(is_toggle: str) -> bool:
        return is_toggle.lower() in ("toggle", "flip", "other", "change", "reverse")

    @staticmethod
    def state_is_open(is_open: str) -> bool:
        return is_open.lower() in (
            STATE_OPEN,
            "yes",
            "1",
            "yeah",
            "yay",
            "woot",
            "up",
            "tada",
        )

    @staticmethod
    def state_is_closed(is_closed: str) -> bool:
        return is_closed.lower() in (
            STATE_CLOSED,
            "no",
            "0",
            "boo",
            "nay",
            "close",
            "down",
            "shut",
        )

    @staticmethod
    def is_ping(payload: str) -> bool:
        return payload.lower() in ("check", "get", "ping", "update", "fetch")

    def state_parse(self, payload: str) -> (str, bool):
        if payload in (STATE_OPEN, STATE_CLOSED):
            return None, self.state_from_name(payload)
        if not payload or self.is_ping(payload):
            return self.state_name(self._open_state), self._open_state
        if self.state_is_toggle(payload):
            new_state = False if self._open_state else True
            return self.state_name(new_state), new_state
        if self.state_is_open(payload):
            return STATE_OPEN, True
        if self.state_is_closed(payload):
            return STATE_CLOSED, False
        raise ValueError(f"cannot translate {payload}")

    @classmethod
    def prepare_for_start(cls):
        logger.debug("Clearing class-wide started state")
        cls._started = False


async def _set_api(run_state: RunState, web_session):
    cfg = Cfg()
    try:
        # raise AuthenticationError("testing this change")
        run_state.api = await login(
            cfg.myq_email, cfg.myq_password, web_session, cfg.user_agent
        )
    except AuthenticationError as e:
        logger.error(f"Dampening login issue: {e}")
        await asyncio.sleep(15)
        raise e
    run_state.garage_doors, run_state.topics = {}, {}
    for account in run_state.api.accounts:
        logger.debug(f"Account Name: {run_state.api.accounts[account]}")
        device_ids = [
            device_id
            for device_id in run_state.api.covers
            if run_state.api.devices[device_id].account == account
        ]
        for device_id in device_ids:
            device = run_state.api.devices[device_id]
            garage_door = GarageDoor(device)
            run_state.garage_doors[device_id] = garage_door
            for topic in garage_door.topics:
                run_state.topics[topic] = device_id
            logger.info(f"Registered {garage_door} on topics {garage_door.topics}")

    # No garage doors found is a sad thing to have
    assert run_state.garage_doors

    GarageDoor._started = True


async def handle_garage_poller(
    run_state: RunState, main_events_q: asyncio.Queue, poller_ticker_q: asyncio.Queue
):
    async with ClientSession() as web_session:
        try:
            await _set_api(run_state, web_session)
        except RequestError as e:
            raise RuntimeError("Getting MyQ api failed") from e

        cfg = Cfg()
        failures = 0
        while True:
            for garage_door in run_state.garage_doors.values():
                # Skip doors that have recently changed state, so it is given time to quiesce
                now = dt.datetime.now()
                if garage_door._last_request_ts and (
                    now
                    < garage_door._last_request_ts
                    + dt.timedelta(seconds=cfg.poll_interval * 3)
                ):
                    logger.debug(
                        f"{garage_door} changed recently and will be skipped by pooler this time."
                    )
                    continue

                curr_device_state = garage_door._device.state
                is_open = garage_door.state_from_name(curr_device_state)
                if is_open != garage_door._open_state:
                    logger.info(
                        f"{garage_door} changed state to {garage_door._device.state}"
                    )
                    await main_events_q.put(
                        GarageStateEvent(
                            device_id=garage_door.device_id, is_open=is_open
                        )
                    )
                    garage_door._open_state = is_open

            ticker_msg = await poller_ticker_q.get()
            if ticker_msg:
                logger.debug(f"poller_ticker_q message: {ticker_msg}")

            try:
                logger.debug("Updating garage doors from myQ api")
                await run_state.api.update_device_info()
                failures = 0
            except MyQError as err:
                logger.error("Unable to update device info: %s", err)
                failures += 1
                await asyncio.sleep(5)
            # TODO(flaviof): Make failure tolerance configurable
            if failures >= 3:
                raise RuntimeError("Too many consecutive failures from API")

            poller_ticker_q.task_done()


async def handle_garage_poller_ticker(poll_interval, poller_ticker_q: asyncio.Queue):
    assert poll_interval
    while True:
        await asyncio.sleep(poll_interval)
        await poller_ticker_q.put(False)


async def handle_garage_requests(run_state: RunState, garage_events_q):
    while True:
        if not GarageDoor._started:
            logger.debug("Waiting for poller to get started")
            await asyncio.sleep(3)
            continue

        garage_state_event = await garage_events_q.get()
        garage_door = run_state.garage_doors[garage_state_event.device_id]
        wanted_open_state = garage_state_event.is_open
        if wanted_open_state != garage_door._open_state:
            logger.info(
                f"{garage_door} changing state to {garage_door.state_name(wanted_open_state)}"
            )
            if wanted_open_state:
                await garage_door.open_door()
            else:
                await garage_door.close_door()
        else:
            logger.debug(
                f"{garage_door} state unchanged as {garage_door.state_name(wanted_open_state)}"
            )

        garage_events_q.task_done()

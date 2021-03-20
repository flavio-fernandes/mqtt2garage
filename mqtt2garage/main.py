#!/usr/bin/env python
import asyncio
from contextlib import AsyncExitStack

from asyncio_mqtt import Client, MqttError

from mqtt2garage import log
from mqtt2garage.config import Cfg
from mqtt2garage.events import GarageStateEvent, MqttMsgEvent
from mqtt2garage.mqtt import (
    handle_mqtt_publish,
    handle_periodic_mqtt_report,
    handle_mqtt_messages,
    send_mqtt_report,
)
from mqtt2garage.myq_wrapper import (
    RunState,
    GarageDoor,
    handle_garage_poller,
    handle_garage_poller_ticker,
    handle_garage_requests,
)


async def handle_main_event_garage(
    garage_state_event: GarageStateEvent,
    run_state: RunState,
    mqtt_send_q: asyncio.Queue,
    _garage_events_q: asyncio.Queue,
    _poller_ticker_q: asyncio.Queue,
):
    garage_door = run_state.garage_doors.get(garage_state_event.device_id)
    if not garage_door:
        logger.warning(
            f"No garage door with device_id {garage_state_event.device_id}: Ignoring myq event"
        )
        return
    payload = garage_door.state_name(garage_state_event.is_open)
    logger.info(
        f"Myq event requesting mqtt for {garage_door} to publish {garage_door.topic} as {payload}"
    )
    await mqtt_send_q.put(MqttMsgEvent(topic=garage_door.topic, payload=payload))


async def handle_main_event_mqtt(
    mqtt_msg: MqttMsgEvent,
    run_state: RunState,
    mqtt_send_q: asyncio.Queue,
    garage_events_q: asyncio.Queue,
    poller_ticker_q: asyncio.Queue,
):
    device_id = run_state.topics.get(mqtt_msg.topic)
    garage_door = run_state.garage_doors.get(device_id)
    if not garage_door:
        if GarageDoor.is_ping(mqtt_msg.payload):
            poller_ticker_q.put_nowait("handle_main_event_mqtt ping payload")
            await mqtt_send_q.put(MqttMsgEvent(topic=mqtt_msg.topic, payload=None))
            await send_mqtt_report(run_state, mqtt_send_q)
            logger.info("pong")
        elif mqtt_msg.topic != Cfg().mqtt_topic:
            logger.warning(
                f"No garage door for topic {mqtt_msg.topic} found: Ignoring mqtt event"
            )
        return
    try:
        translated, new_state = garage_door.state_parse(mqtt_msg.payload)
        # NOTE: When payload gets tranalated, it means that it was not a clear open/close value.
        # That includes valid values such as "ping" or "update". For that reason, let's poke myQ
        # poller_ticker to fetch us the latest and greatest value from the device.
        if translated:
            await poller_ticker_q.put(f"handle_main_event_mqtt for {translated}")
            await mqtt_send_q.put(
                MqttMsgEvent(topic=mqtt_msg.topic, payload=translated)
            )
            # If topic used was not the main one used for garage door, send that too
            if mqtt_msg.topic != garage_door.topic:
                await mqtt_send_q.put(
                    MqttMsgEvent(topic=garage_door.topic, payload=translated)
                )
            return
    except ValueError as e:
        logger.warning(f"Ignoring payload for topic {mqtt_msg.topic}: {e}")
        return
    new_state_str = garage_door.state_name(new_state)
    try:
        garage_events_q.put_nowait(
            GarageStateEvent(device_id=garage_door.device_id, is_open=new_state)
        )
    except asyncio.queues.QueueFull:
        logger.error(
            f"{garage_door} is too busy to take request to be set as {new_state_str}"
        )
        return
    msg = f"Mqtt event causing {garage_door} to be set as {new_state_str}"
    if new_state_str != mqtt_msg.payload:
        msg += f" ({mqtt_msg.payload})"
    logger.info(msg)


async def handle_main_events(
    run_state: RunState,
    mqtt_send_q: asyncio.Queue,
    garage_events_q: asyncio.Queue,
    main_events_q: asyncio.Queue,
    poller_ticker_q: asyncio.Queue,
):
    handlers = {
        "GarageStateEvent": handle_main_event_garage,
        "MqttMsgEvent": handle_main_event_mqtt,
    }
    while True:
        main_event = await main_events_q.get()
        logger.debug(f"Handling {main_event.event}...")
        handler = handlers.get(main_event.event)
        if handler:
            await handler(
                main_event, run_state, mqtt_send_q, garage_events_q, poller_ticker_q
            )
        else:
            logger.error(f"No handler found for {main_event.event}")
        main_events_q.task_done()


async def cancel_tasks(tasks):
    logger.info("Cancelling all tasks")
    for task in tasks:
        if task.done():
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def main_loop():
    global stop_gracefully

    # https://pypi.org/project/asyncio-mqtt/
    logger.debug("Starting main event processing loop")
    cfg = Cfg()
    mqtt_send_q = asyncio.Queue(maxsize=256)
    garage_events_q = asyncio.Queue(maxsize=4)
    main_events_q = asyncio.Queue(maxsize=256)
    poller_ticker_q = asyncio.Queue(maxsize=1)

    # We 💛 context managers. Let's create a stack to help
    # us manage them.
    async with AsyncExitStack() as stack:
        # Keep track of the asyncio tasks that we create, so that
        # we can cancel them on exit
        tasks = set()
        stack.push_async_callback(cancel_tasks, tasks)

        client = Client(
            cfg.mqtt_host,
            username=cfg.mqtt_username,
            password=cfg.mqtt_password,
            client_id=cfg.mqtt_client_id,
        )
        await stack.enter_async_context(client)

        messages = await stack.enter_async_context(client.unfiltered_messages())
        task = asyncio.create_task(handle_mqtt_messages(messages, main_events_q))
        tasks.add(task)

        GarageDoor.prepare_for_start()
        run_state = RunState()
        await client.subscribe(f"{cfg.mqtt_topic}/#")

        tasks.add(asyncio.create_task(handle_mqtt_publish(client, mqtt_send_q)))
        tasks.add(
            asyncio.create_task(handle_periodic_mqtt_report(run_state, mqtt_send_q))
        )
        tasks.add(
            asyncio.create_task(handle_garage_requests(run_state, garage_events_q))
        )
        tasks.add(
            asyncio.create_task(
                handle_garage_poller(run_state, main_events_q, poller_ticker_q)
            )
        )
        tasks.add(
            asyncio.create_task(
                handle_garage_poller_ticker(cfg.poll_interval, poller_ticker_q)
            )
        )

        task = asyncio.create_task(
            handle_main_events(
                run_state, mqtt_send_q, garage_events_q, main_events_q, poller_ticker_q
            )
        )
        tasks.add(task)

        # Wait for everything to complete (or fail due to, e.g., network errors)
        await asyncio.gather(*tasks)

    logger.debug("all done!")


# cfg_globals
stop_gracefully = False
logger = None


async def main():
    global stop_gracefully

    # Reconnect automatically if the connection is lost.
    reconnect_interval = Cfg().reconnect_interval
    while not stop_gracefully:
        try:
            await main_loop()
        except MqttError as error:
            logger.warning(
                f'MQTT error "{error}". Reconnecting in {reconnect_interval} seconds.'
            )
        except (KeyboardInterrupt, SystemExit):
            logger.info("got KeyboardInterrupt")
            stop_gracefully = True
            break
        await asyncio.sleep(reconnect_interval)


if __name__ == "__main__":
    logger = log.getLogger()
    log.initLogger()

    knobs = Cfg().knobs
    if knobs.get("log_to_console"):
        log.log_to_console()
    if knobs.get("log_level_debug"):
        log.set_log_level_debug()

    logger.debug("mqtt2garage process started")
    asyncio.run(main())
    if not stop_gracefully:
        raise RuntimeError("main is exiting")

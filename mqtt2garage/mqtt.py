import asyncio

from mqtt2garage import log
from mqtt2garage.config import Cfg
from mqtt2garage.events import MqttMsgEvent
from mqtt2garage.myq_wrapper import RunState

logger = log.getLogger()


async def handle_mqtt_publish(client, mqtt_send_q: asyncio.Queue):
    while True:
        mqtt_msg = await mqtt_send_q.get()
        topic, payload = mqtt_msg.topic, mqtt_msg.payload
        # logger.debug(f"Publishing: {topic} {payload}")
        try:
            await client.publish(topic, payload, timeout=15)
            logger.debug(f"Published: {topic} {payload}")
        except Exception as e:
            logger.error(f"Client failed publish {topic} {payload}: {e}")
        mqtt_send_q.task_done()
        # Dampen publishes. This is a fail-safe and should not affect anything unless
        # there is a bug lurking somewhere
        await asyncio.sleep(3)


async def send_mqtt_report(run_state: RunState, mqtt_send_q: asyncio.Queue):
    for garage_door in run_state.garage_doors.values():
        if not garage_door.is_started():
            continue
        logger.debug(f"mqtt_report for {garage_door}")
        await mqtt_send_q.put(
            MqttMsgEvent(topic=garage_door.topic, payload=garage_door.state_payload)
        )


async def handle_periodic_mqtt_report(run_state: RunState, mqtt_send_q: asyncio.Queue):
    report_interval = Cfg().periodic_mqtt_report
    if not report_interval:
        logger.info("periodic_mqtt_report is disabled")
        return
    while True:
        await asyncio.sleep(report_interval)
        await send_mqtt_report(run_state, mqtt_send_q)


async def handle_mqtt_messages(messages, main_events_q: asyncio.Queue):
    async for message in messages:
        msg_topic = message.topic
        msg_payload = message.payload.decode()
        logger.debug(f"Received mqtt topic:{msg_topic} payload:{msg_payload}")
        await main_events_q.put(MqttMsgEvent(topic=msg_topic, payload=msg_payload))

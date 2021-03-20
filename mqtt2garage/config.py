#!/usr/bin/env python
import os
import sys
from collections import namedtuple

from strictyaml import (
    load,
    Map,
    Str,
    Bool,
    Float,
    Optional,
    Email,
    EmptyDict,
    MapPattern,
)

from mqtt2garage import const
from mqtt2garage import log

CFG_FILENAME = os.path.dirname(os.path.abspath(const.__file__)) + "/../data/config.yaml"
Info = namedtuple("Info", "knobs mqtt cfg_globals myq alias raw_cfg")


class Cfg:
    _info = None  # class (or static) variable

    @property
    def knobs(self):
        return self._get_info().knobs

    @property
    def mqtt_host(self):
        return self._get_info().mqtt["host"]

    @property
    def mqtt_client_id(self):
        return self._get_info().mqtt["client_id"]

    @property
    def mqtt_username(self):
        return self._get_info().mqtt.get("username")

    @property
    def mqtt_password(self):
        return self._get_info().mqtt.get("password")

    @property
    def mqtt_topic(self):
        return self._get_info().cfg_globals["topic_prefix"]

    @property
    def reconnect_interval(self):
        return self._get_info().cfg_globals["reconnect_interval"]

    @property
    def poll_interval(self):
        return self._get_info().cfg_globals["poll_interval"]

    @property
    def periodic_mqtt_report(self):
        return self._get_info().cfg_globals["periodic_mqtt_report"]

    @property
    def user_agent(self):
        cfg_globals = self._get_info().cfg_globals
        return cfg_globals.get("user_agent")

    @property
    def myq_email(self):
        return self._get_info().myq["email"]

    @property
    def myq_password(self):
        return self._get_info().myq["password"]

    @property
    def alias(self):
        return self._get_info().alias

    @classmethod
    def _get_config_filename(cls):
        if len(sys.argv) > 1:
            return sys.argv[1]
        return CFG_FILENAME

    @classmethod
    def _get_info(cls):

        # https://hitchdev.com/strictyaml
        schema = Map(
            {
                Optional("knobs"): Map(
                    {
                        Optional("log_to_console"): Bool(),
                        Optional("log_level_debug"): Bool(),
                    }
                )
                | EmptyDict(),
                "globals": Map(
                    {
                        "topic_prefix": Str(),
                        Optional(
                            "reconnect_interval",
                            default=const.MQTT_DEFAULT_RECONNECT_INTERVAL,
                        ): Float(),
                        Optional(
                            "poll_interval", default=const.MYQ_DEFAULT_POLL_INTERVAL
                        ): Float(),
                        Optional(
                            "periodic_mqtt_report",
                            default=const.DEFAULT_PERIODIC_MQTT_REPORT,
                        ): Float(),
                        Optional("user_agent"): Str(),
                    }
                ),
                "mqtt": Map(
                    {
                        "host": Str(),
                        Optional(
                            "client_id", default=const.MQTT_DEFAULT_CLIENT_ID
                        ): Str(),
                        Optional("username"): Str(),
                        Optional("password"): Str(),
                    }
                ),
                "myq": Map(
                    {
                        "email": Email(),
                        "password": Str(),
                    }
                ),
                Optional("alias"): MapPattern(Str(), Str()) | EmptyDict(),
            }
        )

        if not cls._info:
            config_filename = cls._get_config_filename()
            logger.info("loading yaml config file %s", config_filename)
            with open(config_filename, "r") as ymlfile:
                raw_cfg = load(ymlfile.read(), schema).data
                cls._parse_raw_cfg(raw_cfg)
        return cls._info

    @classmethod
    def _parse_raw_cfg(cls, raw_cfg):
        cls._info = Info(
            raw_cfg.get("knobs", {}),
            raw_cfg.get("mqtt"),
            raw_cfg.get("globals", {}),
            raw_cfg.get("myq"),
            raw_cfg.get("alias", {}),
            raw_cfg,
        )


# =============================================================================


logger = log.getLogger()
if __name__ == "__main__":
    log.initLogger()
    c = Cfg()
    logger.info("c.knobs: {}".format(c.knobs))
    logger.info("c.mqtt_host: {}".format(c.mqtt_host))
    logger.info("c.cfg_globals: {}".format(c.cfg_globals))
    logger.info("c.myq: {}".format(c.myq))
    logger.info("c.alias: {}".format(c.alias))

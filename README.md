# mqtt2garage
#### A python-based project that provides a wrapper to arraylabs/pymyq for MQTT access

This repo provides an MQTT interface to the awesome [python-myQ](https://github.com/arraylabs/pymyq) library.
More specifically, one can use the MQTT protocol to open/close/query MyQ compatible garage doors.

The minimal configuration for managing/monitoring the devices is:

```yaml
globals:
    # every device will have this prefix in its topic, followed
    # by its serial number. Example: /myq/1234567890AB
    topic_prefix: /myq
mqtt:
    # ip/dns for the mqtt broker
    host: 192.168.1.250
myq:
    email: user@example.com
    password: super_secret_stuff
```

There are many more attributes for further customizations, shown in the
[data/config.yaml](https://github.com/flavio-fernandes/mqtt2garage/blob/main/data/config.yaml) file.

Starting this project can be done by setting up a service (see 
[mqtt2garage.service](https://github.com/flavio-fernandes/mqtt2garage/blob/main/mqtt2garage/bin/mqtt2garage.service.example) as 
reference), or doing the following steps:
```shell script
$ ./mqtt2garage/bin/create-env.sh && \
  source ./env/bin/activate && \
  export PYTHONPATH=${PWD}

$ python3 mqtt2garage/main.py ./data/config.yaml
```

Granted the config refers to a valid [MyQ](https://www.myq.com/) account, use regular MQTT tools for
controlling and monitoring. Example below.

Controlling the garage doors via MQTT:
```shell script
$ mosquitto_pub -h ${mqttBroker} -t /myq -m ping
$ mosquitto_pub -h ${mqttBroker} -t /myq/${garageSerialNumberOrAlias} -m open
$ mosquitto_pub -h ${mqttBroker} -t /myq/${garageSerialNumberOrAlias2} -m toggle
```

Subscribe to see changes to garage doors, regardless on how they were controlled:
```shell script
$ mosquitto_sub -F '@Y-@m-@dT@H:@M:@S@z : %q : %t : %p' -h ${mqttBroker} -t '/myq/#'
```

**NOTE:** Use python 3.7 or newer, as this project requires a somewhat
recent implementation of [asyncio](https://realpython.com/async-io-python/).

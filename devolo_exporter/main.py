'''
=head1 NAME
devolo_exporter Prometheus Plugin to monitor status of Devolo Magic 2 LAN

=head1 DESCRIPTION

=head1 REQUIREMENTS
- BeautifulSoup
- pycryptodome

'''

import asyncio
import logging
import threading
import time
from http.server import HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Union, Dict

import click
from deepmerge import Merger
from devolo_plc_api import Device
from prometheus_client import CollectorRegistry, MetricsHandler
from prometheus_client.metrics_core import GaugeMetricFamily, InfoMetricFamily
from ruamel.yaml import YAML

IP_ADDRESS = "ip_address"
PASSWORD = "password"
EXPORTER = "exporter"
PORT = "port"
HOST = "host"

# pick default timeout one second less than the default prometheus timeout of 10s
DEFAULT_CONFIG = {
    EXPORTER: {
        PORT: 5642,
        HOST: 'localhost',
    }
}

TOPOLOGY = {
    0: "unknown",
    1: "local",
    2: "remote",
}

TECHNOLOGY = {
    0: "unknown",
    3: "HomePluvAV Thunderbolt",
    4: "HomePlugAV Panther",
    7: "G.hn Spirit",
}


def load_config(config_file: Union[str, Path]) -> Dict:
    """
    Loads and validates YAML config for this exporter and fills in default values
    :param config_file:
    :return: config as dictionary
    """
    yaml = YAML()
    with open(config_file) as f:
        config = yaml.load(f)

    # merge with default config: use 'override' for lists to let users replace extractor setting entirely
    merger = Merger([(list, "override"), (dict, "merge")], ["override"], ["override"])
    config = merger.merge(DEFAULT_CONFIG, config)

    for param in [IP_ADDRESS, PASSWORD]:
        if not param in config:
            raise ValueError(
                f"'{param}' is a mandatory config parameter, but it is missing in the YAML configuration file. Please see README.md for an example."
            )

    if EXPORTER in config.keys():
        if config[EXPORTER][PORT] < 0 or config[EXPORTER][PORT] > 65535:
            raise ValueError(f"Invalid exporter port.")

    return config


# Taken 1:1 from prometheus-client==0.7.1, see https://github.com/prometheus/client_python/blob/3cb4c9247f3f08dfbe650b6bdf1f53aa5f6683c1/prometheus_client/exposition.py
class _ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    """Thread per request HTTP server."""

    # Make worker threads "fire and forget". Beginning with Python 3.7 this
    # prevents a memory leak because ``ThreadingMixIn`` starts to gather all
    # non-daemon threads in a list in order to join on them at server close.
    # Enabling daemon threads virtually makes ``_ThreadingSimpleServer`` the
    # same as Python 3.7's ``ThreadingHTTPServer``.
    daemon_threads = True


class PlcNetCollector(object):
    def __init__(
            self,
            logger,
            ip_address: str,
            password: str,
    ):
        self.logger = logger
        self.ip_address = ip_address
        self.password = password

    def collect(self):
        self.logger.info("Collecting from " + self.ip_address)
        labels = ["hostname", "ip_address", "network_name"]

        device_information_metrics = InfoMetricFamily(
            'devolo_device_info',
            'General device information',
            labels=labels,
        )

        connected_devices_metric = GaugeMetricFamily(
            "devolo_connected_devices",
            "Number of connected devices",
            unit="amount",
            labels=labels,
        )

        tx_rate_metrics = GaugeMetricFamily(
            'devolo_tx_rate',
            'Device tx rate',
            unit="megabits",
            labels=['from_hostname', 'to_hostname', 'from_ip_address', 'to_ip_address', 'network_name'],
        )

        rx_rate_metrics = GaugeMetricFamily(
            'devolo_rx_rate',
            'Device rx rate',
            unit="megabits",
            labels=['from_hostname', 'to_hostname', 'from_ip_address', 'to_ip_address', 'network_name'],
        )

        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

        with Device(ip=self.ip_address) as dpa:
            dpa.password = self.password
            network = dpa.plcnet.get_network_overview()
            mac_to_device = {}
            for device in network.devices:
                label_values = [device.user_device_name, device.ipv4_address, device.user_network_name]
                device_information_metrics.add_metric(label_values, {
                    'product_name': device.product_name,
                    'product_id': device.product_id,
                    'friendly_version': device.friendly_version,
                    'full_version': device.full_version,
                    'mac_address': device.mac_address,
                    'topology': TOPOLOGY.get(device.topology, "unknown"),
                    'technology': TECHNOLOGY.get(device.technology, "unknown"),
                    'attached_to_gateway': str(device.attached_to_router),
                    'network_name': device.user_network_name,
                    'ipv4_address': device.ipv4_address,
                })
                mac_to_device[device.mac_address] = device
                connected_devices_metric.add_metric(label_values, len(device.bridged_devices))
            for data_rate in network.data_rates:
                to_device = mac_to_device[data_rate.mac_address_to]
                from_device = mac_to_device[data_rate.mac_address_from]
                label_values = [from_device.user_device_name, to_device.user_device_name, from_device.ipv4_address,
                                to_device.ipv4_address, from_device.user_network_name]
                tx_rate_metrics.add_metric(label_values,float(data_rate.tx_rate))
                rx_rate_metrics.add_metric(label_values, float(data_rate.rx_rate))

        yield device_information_metrics
        yield connected_devices_metric
        yield tx_rate_metrics
        yield rx_rate_metrics


@click.command()
@click.argument("config_file", type=click.Path(exists=True, dir_okay=False))
@click.option('-d', '--debug', is_flag=True, help="show debug messages", )
def main(config_file='config.yml', debug=False):
    """
    Launch the exporter using a YAML config file.
    """

    log_level = logging.DEBUG if debug else logging.INFO
    format1 = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    format2 = '[%(asctime)s:%(levelname)05s:%(filename)20s:%(lineno)3s - %(funcName)25s() ] %(message)s'
    logging.basicConfig(format=format2, level=log_level)
    logger = logging.getLogger()

    # load user and merge with defaults
    config = load_config(config_file)
    exporter_config = config[EXPORTER]

    # fire up collector
    reg = CollectorRegistry()
    reg.register(
        PlcNetCollector(
            logger,
            ip_address=config[IP_ADDRESS],
            password=config[PASSWORD],
        )
    )

    # start http server
    CustomMetricsHandler = MetricsHandler.factory(reg)
    httpd = _ThreadingSimpleServer((exporter_config[HOST], exporter_config[PORT]), CustomMetricsHandler)
    httpd_thread = threading.Thread(target=httpd.serve_forever)
    httpd_thread.start()

    logger.info(
        f"Exporter running at http://{exporter_config[HOST]}:{exporter_config[PORT]}, querying {config[IP_ADDRESS]}"
    )

    # wait indefinitely
    try:
        while True:
            time.sleep(3)
    except KeyboardInterrupt:
        httpd.shutdown()
        httpd_thread.join()

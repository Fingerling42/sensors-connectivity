import json
import time
import typing as tp
import logging
from tempfile import NamedTemporaryFile
import ipfshttpclient
import robonomicsinterface as RI
import requests
import threading
from pinatapy import PinataPy

from utils.database import DataBase
from feeders import IFeeder
from stations import StationData, Measurement
from drivers.ping import PING_MODEL

thlock = threading.RLock()


def _sort_payload(data: dict) -> dict:
    ordered = {}
    for k, v in data.items():
        meas = sorted(v["measurements"], key=lambda x: x["timestamp"])
        ordered[k] = {"model": v["model"], "geo": v["geo"], "measurements": meas}
    return ordered


def _get_multihash(
    buf: set, db: object, endpoint: str = "/ip4/127.0.0.1/tcp/5001/http"
) -> tp.Dict[str, str]:
    payload = {}
    for m in buf:
        if m.public in payload:
            payload[m.public]["measurements"].append(m.measurement_check())
        else:
            payload[m.public] = {
                "model": m.model,
                "geo": "{},{}".format(m.geo_lat, m.geo_lon),
                "measurements": [m.measurement_check()],
            }

    logging.debug(f"Payload before sorting: {payload}")
    payload = _sort_payload(payload)
    logging.debug(f"Payload sorted: {payload}")

    temp = NamedTemporaryFile(mode="w", delete=False)
    logging.debug(f"Created temp file: {temp.name}")
    temp.write(json.dumps(payload))
    temp.close()

    with ipfshttpclient.connect(endpoint) as client:
        response = client.add(temp.name)
        db.add_data("not sent", response["Hash"], time.time(), json.dumps(payload))
        return (response["Hash"], temp.name)


def _pin_to_pinata(file_path: str, config: dict) -> None:
    pinata_api = config["datalog"]["pinata_api"]
    pinata_secret = config["datalog"]["pinata_secret"]
    if pinata_secret:
        try:
            logging.info("Pinning file to Pinata")
            pinata = PinataPy(pinata_api, pinata_secret)
            pinata.pin_file_to_ipfs(file_path)
            hash = pinata.pin_list()["rows"][0]["ipfs_pin_hash"]
            logging.info(f"File sent to pinata. Hash is {hash}")
        except Exception as e:
            logging.warning(f"Failed while pining file to Pinata. Error: {e}")


class DatalogFeeder(IFeeder):
    """
    The feeder is responsible for collecting measurements and
    publishing an IPFS hash to Robonomics on Substrate

    It requires the full path to `robonomics` execution binary
    and an account's private key
    """

    def __init__(self, config) -> None:
        super().__init__(config)
        self.last_time: float = time.time()
        self.buffer: set = set()
        self.interval: int = self.config["datalog"]["dump_interval"]
        self.ipfs_endpoint: str = (
            config["robonomics"]["ipfs_provider"]
            if config["robonomics"]["ipfs_provider"]
            else "/ip4/127.0.0.1/tcp/5001/http"
        )
        self.db: DataBase = DataBase(self.config)
        self.db.create_table()

    def feed(self, data: tp.List[StationData]) -> None:
        if self.config["datalog"]["enable"]:
            logging.info("DatalogFeeder:")
            for d in data:
                if d.measurement.public and d.measurement.model != PING_MODEL:
                    logging.debug(f"Adding data to buffer: {d.measurement}")
                    self.buffer.add(d.measurement)

            if (time.time() - self.last_time) >= self.interval:
                if self.buffer:
                    logging.debug("About to publish collected data...")
                    logging.debug(f"Buffer is {self.buffer}")
                    ipfs_hash, file_path = _get_multihash(
                        self.buffer, self.db, self.ipfs_endpoint
                    )
                    self._pin_to_temporal(file_path)
                    _pin_to_pinata(file_path, self.config)
                    self.to_datalog(ipfs_hash)
                else:
                    logging.info("Nothing to publish")
                # self.buffer = set()
                # self.last_time = time.time()
            else:
                logging.info("Still collecting measurements...")

    def _pin_to_temporal(self, file_path: str) -> None:
        username = self.config["datalog"]["temporal_username"]
        password = self.config["datalog"]["temporal_password"]
        if username and password:
            auth_url = "https://api.temporal.cloud/v2/auth/login"
            token_resp = requests.post(
                auth_url, json={"username": username, "password": password}
            )
            token = token_resp.json()

            url_add = "https://api.temporal.cloud/v2/ipfs/public/file/add"
            headers = {"Authorization": f"Bearer {token['token']}"}
            resp = requests.post(
                url_add,
                files={"file": open(file_path), "hold_time": (None, 1)},
                headers=headers,
            )

            if resp.status_code == 200:
                logging.info("File pinned to Temporal Cloud")

    def to_datalog(self, ipfs_hash: str) -> None:
        logging.info(ipfs_hash)
        self.last_time = time.time()
        self.buffer = set()
        interface = RI.RobonomicsInterface(seed=self.config["datalog"]["suri"])
        try:
            robonomics_receipt = interface.record_datalog(ipfs_hash)
            logging.info(
                f"Ipfs hash sent to Robonomics Parachain and included in block {robonomics_receipt}"
            )
            self.db.update_status("sent", ipfs_hash)
        except Exception as e:
            logging.warning(
                f"Something went wrong during extrinsic submission to Robonomics: {e}"
            )

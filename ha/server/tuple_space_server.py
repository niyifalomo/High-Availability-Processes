import subprocess
import argparse
import os
import time
from queue import Empty
from random import randint


from ha.commons.clients import HearBeatClient, ProxyClient
from ha.commons.logger import get_module_logger
import config as conf
from ha.commons.sap_servers import HearthBeatRequestHandler, HeartBeatServer, MainServer, PrimaryServerRequestHandler, \
    BaseMulitThreadAdmin, ProxyServer, ProxyRequestHandler
from ha.proxy.proxy_server import ProxyThreadAdmin
from ha.server.tuple_space_app.tuplespace_app import TupleSpaceApp

logger = get_module_logger(__name__)


class TupleSpaceThreadAdmin(BaseMulitThreadAdmin):

    def __init__(self, parsed_args: argparse.ArgumentParser(), app: TupleSpaceApp):
        super().__init__(parsed_args)
        self.backup_started = False
        self.heart_beat_server_started = False
        self.heart_beat_client_stared = False
        self.registered_on_proxy = False
        self.isPrimary = parsed_args.is_primary
        self.name = 'primary' if parsed_args.is_primary else 'backup'
        self.primary_process_id = ""
        self.backup_process_id = ""
        self.app = app
        self.thread_Q_handlers.update({'BEAT':self.handle_heartbeat_sap})

    def initialize(self):
        """ Initial Tuple Space service"""
        try:
            if self.isPrimary:
                self.init_as_primary()
            else:
                self.init_as_backup()

            self.monitor_threads()
        except InterruptedError as err:
            logger.error("{} Shutting down {}".format(self.name, err))
        except Exception as err:
            logger.error("{} Shutting down {}".format(self.name, err))
        finally:
            self.shutdown_service()

    def init_as_primary(self):
        logger.info("Starting primary service")
        self.app.init()
        self.start_shutdown_socket()
        self.start_backup()
        self.start_heartbeat_socket()
        self.get_first_heartbeat()
        self.update_backup()
        self.start_main_tps_server()
        self.register_on_proxy()

    def init_as_backup(self):
        logger.debug("server starting as backup")
        self.start_shutdown_socket()
        self.start_main_tps_server()
        self.start_heartbeat_client()

    def start_main_tps_server(self):
        logger.info('starting {} server'.format(self.name))
        tps = MainServer(PrimaryServerRequestHandler,
                         self.parsed_args.tp_sap[0],
                         self.parsed_args.tp_sap[1],
                         app_to_run=self.app,
                         server_type=self.name
                         )
        self.start_thread(tps.serve_forever, tps.server_type)

    def monitor_threads(self):
        #to simulate backup shutdown
        cnt = 0
        rand_period = randint(0, 50)
        logger.info("monitoring {} server threads ".format(self.name))
        while True:
            time.sleep(1)
            try:
                data = self.thread_Q.get(False)
                self.thread_Q_handlers[data['command']](data['payload'])
            except Empty:
                pass
            # #for backup test
            if self.isPrimary and cnt == rand_period:
                self.send_shutdwon(conf.PRIMARY_SERVER_SHUTDOWN_IP, conf.BACKUP_SERVER_SHUTDOWN_PORT)
                cnt = 0
            cnt+=1


    def start_backup(self) -> None:
        if not self.backup_started:
            """ Start backup service with specific arguments"""
            # use os.getpid() get process id that can be used to kill the primary process
            # add additional parameters for the backup service
            backup_start_cmd = """python "{}" server -tpfile "{}" -tpsap "{}" -shutdown "{}" -heartbeat "{}" -backup "{}" -bk_shutdown "{}" -proxy "{}" --is_primary "{}" -primary_id "{}"
            """.format(self.server_script_name,
                       self.parsed_args.tuple_space_file,
                       (conf.BACKUP_SERVER_2_PROXY_IP, conf.BACKUP_SERVER_2_PROXY_PORT),
                       (self.parsed_args.bk_shutdown_sap[0], self.parsed_args.bk_shutdown_sap[1]),
                       (conf.BACKUP_SERVER_HEARTBEAT_IP, conf.BACKUP_SERVER_HEARTBEAT_PORT),
                       (self.parsed_args.tp_sap[0], self.parsed_args.tp_sap[1]),
                       (self.parsed_args.backup_sap[0], self.parsed_args.backup_sap[1]),
                       (self.parsed_args.proxy_sap[0], self.parsed_args.proxy_sap[1]),
                       'false',
                       str(os.getpid())
                       )

            logger.info("Starting backup server")
            subprocess.Popen(backup_start_cmd, shell=False)
            self.backup_started = True

    def start_heartbeat_socket(self, ):
        if not self.heart_beat_server_started:
            hb = HeartBeatServer(HearthBeatRequestHandler,
                                 self.parsed_args.heartbeat_sap[0],
                                 self.parsed_args.heartbeat_sap[1],
                                 Q=self.thread_Q)
            self.start_thread(hb.serve_forever, hb.server_type)
            self.heart_beat_server_started = True

    def handle_heartbeat_sap(self, msg):
        # print(msg)
        if self.isPrimary:
            if msg == 'NO_HEART_BEAT':
                logger.error('NO_HEART_BEAT from backup... restarting backup')
                self.backup_started = False
                self.restart_backup()

    def restart_backup(self):
        self.start_backup()
        self.get_first_heartbeat()
        self.update_backup()

    def handle_shutdown_sap(self, msg):
        raise InterruptedError("shutdown requested")

    def shutdown_service(self):
        logger.info("shutting down all {} server services".format(self.name))
        self.app.shutdown()

    def get_first_heartbeat(self):
        try:
            logger.info("waiting for first heartbeat")
            data = self.thread_Q.get(timeout=conf.HEARTBEAT_WAIT_TIME)
            return data == 'HB-0'
        except Empty:
            raise RuntimeError("no heartbeat after {} secs".format(conf.HEARTBEAT_WAIT_TIME))

    def start_heartbeat_client(self):
        if not self.heart_beat_client_stared:
            try:
                hb_client = HearBeatClient(conf.PRIMARY_SERVER_HEARTBEAT_IP, conf.PRIMARY_SERVER_HEARTBEAT_PORT)
                self.start_thread(hb_client.send_heartbeat, 'hb_client')
            except OSError:
                self.isPrimary = True
                self.init_as_primary()
            logger.info('backup heartbeat client running')
            self.heart_beat_client_stared = True

    def register_on_proxy(self):
        #@TODO: NIYI

        if self.isPrimary and not self.registered_on_proxy:
            try:
                proxy_client = ProxyClient(conf.PROXY_COMM_IP, conf.PROXY_COMM_PORT)
                proxy_client.send_sap(self.parsed_args.tp_sap[0],self.parsed_args.tp_sap[1])
            except OSError:
                pass
        self.registered_on_proxy = True
        """
        if not self.registered_on_proxy:
            
            self.registered_on_proxy = True
        """

    def update_backup(self):
        #@TODO: NIYI
        pass


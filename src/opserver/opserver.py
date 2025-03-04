#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

#
# Opserver
#
# Operational State Server for VNC
#

from gevent import monkey
monkey.patch_all()
try:
    from collections import OrderedDict
except ImportError:
    # python 2.6 or earlier, use backport
    from ordereddict import OrderedDict
from uveserver import UVEServer
import sys
import ConfigParser
import bottle
import json
import uuid
import argparse
import time
import redis
import base64
import socket
import struct
import errno
import copy
import datetime
import pycassa
from analytics_db import AnalyticsDb

from pycassa.pool import ConnectionPool
from pycassa.columnfamily import ColumnFamily
from pysandesh.util import UTCTimestampUsec
from pysandesh.sandesh_base import *
from pysandesh.sandesh_session import SandeshWriter
from pysandesh.gen_py.sandesh_trace.ttypes import SandeshTraceRequest
from pysandesh.connection_info import ConnectionState
from pysandesh.gen_py.process_info.ttypes import ConnectionType,\
    ConnectionStatus
from sandesh_common.vns.ttypes import Module, NodeType
from sandesh_common.vns.constants import ModuleNames, CategoryNames,\
     ModuleCategoryMap, Module2NodeType, NodeTypeNames, ModuleIds,\
     INSTANCE_ID_DEFAULT, COLLECTOR_DISCOVERY_SERVICE_NAME,\
     ANALYTICS_API_SERVER_DISCOVERY_SERVICE_NAME, ALARM_PARTITION_SERVICE_NAME
from sandesh.viz.constants import _TABLES, _OBJECT_TABLES,\
    _OBJECT_TABLE_SCHEMA, _OBJECT_TABLE_COLUMN_VALUES, \
    _STAT_TABLES, STAT_OBJECTID_FIELD, STAT_VT_PREFIX, \
    STAT_TIME_FIELD, STAT_TIMEBIN_FIELD, STAT_UUID_FIELD, \
    STAT_SOURCE_FIELD, SOURCE, MODULE
from sandesh.viz.constants import *
from sandesh.analytics.ttypes import *
from sandesh.analytics.cpuinfo.ttypes import ProcessCpuInfo
from sandesh.discovery.ttypes import CollectorTrace
from opserver_util import OpServerUtils
from opserver_util import ServicePoller
from cpuinfo import CpuInfoData
from sandesh_req_impl import OpserverSandeshReqImpl
from sandesh.analytics_database.ttypes import *
from sandesh.analytics_database.constants import PurgeStatusString
from overlay_to_underlay_mapper import OverlayToUnderlayMapper, \
     OverlayToUnderlayMapperError
from generator_introspect_util import GeneratorIntrospectUtil
from stevedore import hook
from partition_handler import PartInfo, UveStreamer, UveCacheProcessor

_ERRORS = {
    errno.EBADMSG: 400,
    errno.ENOBUFS: 403,
    errno.EINVAL: 404,
    errno.ENOENT: 410,
    errno.EIO: 500,
    errno.EBUSY: 503
}

@bottle.error(400)
@bottle.error(403)
@bottle.error(404)
@bottle.error(410)
@bottle.error(500)
@bottle.error(503)
def opserver_error(err):
    return err.body
#end opserver_error

class LinkObject(object):

    def __init__(self, name, href):
        self.name = name
        self.href = href
    # end __init__
# end class LinkObject


def obj_to_dict(obj):
    # Non-null fields in object get converted to json fields
    return dict((k, v) for k, v in obj.__dict__.iteritems())
# end obj_to_dict


def redis_query_start(host, port, redis_password, qid, inp):
    redish = redis.StrictRedis(db=0, host=host, port=port,
                                   password=redis_password)
    for key, value in inp.items():
        redish.hset("QUERY:" + qid, key, json.dumps(value))
    query_metadata = {}
    query_metadata['enqueue_time'] = OpServerUtils.utc_timestamp_usec()
    redish.hset("QUERY:" + qid, 'query_metadata', json.dumps(query_metadata))
    redish.hset("QUERY:" + qid, 'enqueue_time',
                OpServerUtils.utc_timestamp_usec())
    redish.lpush("QUERYQ", qid)

    res = redish.blpop("REPLY:" + qid, 10)
    if res is None:
        return None
    # Put the status back on the queue for the use of the status URI
    redish.lpush("REPLY:" + qid, res[1])

    resp = json.loads(res[1])
    return int(resp["progress"])
# end redis_query_start


def redis_query_status(host, port, redis_password, qid):
    redish = redis.StrictRedis(db=0, host=host, port=port,
                               password=redis_password)
    resp = {"progress": 0}
    chunks = []
    # For now, the number of chunks will be always 1
    res = redish.lrange("REPLY:" + qid, -1, -1)
    if not res:
        return None
    chunk_resp = json.loads(res[0])
    ttl = redish.ttl("REPLY:" + qid)
    if int(ttl) != -1:
        chunk_resp["ttl"] = int(ttl)
    query_time = redish.hmget("QUERY:" + qid, ["start_time", "end_time"])
    chunk_resp["start_time"] = query_time[0]
    chunk_resp["end_time"] = query_time[1]
    if chunk_resp["progress"] == 100:
        chunk_resp["href"] = "/analytics/query/%s/chunk-final/%d" % (qid, 0)
    chunks.append(chunk_resp)
    resp["progress"] = chunk_resp["progress"]
    resp["chunks"] = chunks
    return resp
# end redis_query_status


def redis_query_chunk_iter(host, port, redis_password, qid, chunk_id):
    redish = redis.StrictRedis(db=0, host=host, port=port,
                               password=redis_password)

    iters = 0
    fin = False

    while not fin:
        #import pdb; pdb.set_trace()
        # Keep the result line valid while it is being read
        redish.persist("RESULT:" + qid + ":" + str(iters))
        elems = redish.lrange("RESULT:" + qid + ":" + str(iters), 0, -1)
        yield elems
        if elems == []:
            fin = True
        else:
            redish.delete("RESULT:" + qid + ":" + str(iters), 0, -1)
        iters += 1

    return
# end redis_query_chunk_iter


def redis_query_chunk(host, port, redis_password, qid, chunk_id):
    res_iter = redis_query_chunk_iter(host, port, redis_password, qid, chunk_id)

    dli = u''
    starter = True
    fin = False
    yield u'{"value": ['
    outcount = 0
    while not fin:

        #import pdb; pdb.set_trace()
        # Keep the result line valid while it is being read
        elems = res_iter.next()

        fin = True
        for elem in elems:
            fin = False
            outcount += 1
            if starter:
                dli += '\n' + elem
                starter = False
            else:
                dli += ', ' + elem
        if not fin:
            yield dli + '\n'
            dli = u''

    if outcount == 0:
        yield '\n' + u']}'
    else:
        yield u']}'
    return
# end redis_query_chunk



def redis_query_result(host, port, redis_password, qid):
    try:
        status = redis_query_status(host, port, redis_password, qid)
    except redis.exceptions.ConnectionError:
        # Update connection info
        ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
            name = 'Query', status = ConnectionStatus.DOWN,
            message = 'Query[%s] result : Connection Error' % (qid),
            server_addrs = ['%s:%d' % (host, port)]) 
        yield bottle.HTTPError(_ERRORS[errno.EIO],
                'Failure in connection to the query DB')
    except Exception as e:
        # Update connection info
        ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
            name = 'Query', status = ConnectionStatus.DOWN,
            message = 'Query[%s] result : Exception: %s' % (qid, str(e)),
            server_addrs = ['%s:%d' % (host, port)])
        self._logger.error("Exception: %s" % e)
        yield bottle.HTTPError(_ERRORS[errno.EIO], 'Error: %s' % e)
    else:
        if status is None:
            yield bottle.HTTPError(_ERRORS[errno.ENOENT], 
                    'Invalid query id (or) query result purged from DB')
        if status['progress'] == 100:
            for chunk in status['chunks']:
                chunk_id = int(chunk['href'].rsplit('/', 1)[1])
                for gen in redis_query_chunk(host, port, redis_password, qid, 
                                             chunk_id):
                    yield gen
        else:
            yield {}
    # Update connection info
    ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
        message = None,
        status = ConnectionStatus.UP,
        server_addrs = ['%s:%d' % (host, port)],
        name = 'Query')
    return
# end redis_query_result

def redis_query_result_dict(host, port, redis_password, qid):

    stat = redis_query_status(host, port, redis_password, qid)
    prg = int(stat["progress"])
    res = []

    if (prg < 0) or (prg == 100):

        done = False
        gen = redis_query_result(host, port, redis_password, qid)
        result = u''
        while not done:
            try:
                result += gen.next()
                #import pdb; pdb.set_trace()
            except StopIteration:
                done = True
        res = (json.loads(result))['value']

    return prg, res
# end redis_query_result_dict


def redis_query_info(redish, qid):
    query_data = {}
    query_dict = redish.hgetall('QUERY:' + qid)
    query_metadata = json.loads(query_dict['query_metadata'])
    del query_dict['query_metadata']
    query_data['query_id'] = qid
    query_data['query'] = str(query_dict)
    query_data['enqueue_time'] = query_metadata['enqueue_time']
    return query_data
# end redis_query_info


class OpStateServer(object):

    def __init__(self, logger, redis_password=None):
        self._logger = logger
        self._redis_list = []
        self._redis_password= redis_password
    # end __init__

    def update_redis_list(self, redis_list):
        self._redis_list = redis_list
    # end update_redis_list

    def redis_publish(self, msg_type, destination, msg):
        # Get the sandesh encoded in XML format
        sandesh = SandeshWriter.encode_sandesh(msg)
        msg_encode = base64.b64encode(sandesh)
        redis_msg = '{"type":"%s","destination":"%s","message":"%s"}' \
            % (msg_type, destination, msg_encode)
        # Publish message in the Redis bus
        for redis_server in self._redis_list:
            redis_inst = redis.StrictRedis(redis_server[0], 
                                           redis_server[1], db=0,
                                           password=self._redis_password)
            try:
                redis_inst.publish('analytics', redis_msg)
            except redis.exceptions.ConnectionError:
                # Update connection info
                ConnectionState.update(conn_type = ConnectionType.REDIS_UVE,
                    name = 'UVE', status = ConnectionStatus.DOWN,
                    message = 'Connection Error',
                    server_addrs = ['%s:%d' % (redis_server[0], \
                        redis_server[1])])
                self._logger.error('No Connection to Redis [%s:%d].'
                                   'Failed to publish message.' \
                                   % (redis_server[0], redis_server[1]))
        return True
    # end redis_publish

# end class OpStateServer

class OpServer(object):

    """
    This class provides ReST API to get operational state of
    Contrail VNS system.

    The supported **GET** APIs are:
        * ``/analytics/virtual-network/<name>``
        * ``/analytics/virtual-machine/<name>``
        * ``/analytics/vrouter/<name>``:
        * ``/analytics/bgp-router/<name>``
        * ``/analytics/bgp-peer/<name>``
        * ``/analytics/xmpp-peer/<name>``
        * ``/analytics/collector/<name>``
        * ``/analytics/tables``:
        * ``/analytics/table/<table>``:
        * ``/analytics/table/<table>/schema``:
        * ``/analytics/table/<table>/column-values``:
        * ``/analytics/table/<table>/column-values/<column>``:
        * ``/analytics/query/<queryId>``
        * ``/analytics/query/<queryId>/chunk-final/<chunkId>``
        * ``/analytics/send-tracebuffer/<source>/<module>/<name>``
        * ``/analytics/operation/analytics-data-start-time``

    The supported **POST** APIs are:
        * ``/analytics/query``:
        * ``/analytics/operation/database-purge``:
    """
    def disc_publish(self):
        try:
            import discoveryclient.client as client
        except:
            try:
                # TODO: Try importing from the server. This should go away..
                import discovery.client as client
            except:
                raise Exception('Could not get Discovery Client')

        data = {
            'ip-address': self._args.host_ip,
            'port': self._args.rest_api_port,
        }
        self.disc = client.DiscoveryClient(
            self._args.disc_server_ip,
            self._args.disc_server_port,
            ModuleNames[Module.OPSERVER])
        self.disc.set_sandesh(self._sandesh)
        self._logger.info("Disc Publish to %s : %d - %s"
                          % (self._args.disc_server_ip,
                             self._args.disc_server_port, str(data)))
        self.disc.publish(ANALYTICS_API_SERVER_DISCOVERY_SERVICE_NAME, data)
    # end

    def __init__(self, args_str=' '.join(sys.argv[1:])):
        self._args = None
        self._parse_args(args_str)
        print args_str
 
        self._homepage_links = []
        self._homepage_links.append(
            LinkObject('documentation', '/documentation/index.html'))
        self._homepage_links.append(LinkObject('analytics', '/analytics'))

        super(OpServer, self).__init__()
        module = Module.OPSERVER
        self._moduleid = ModuleNames[module]
        node_type = Module2NodeType[module]
        self._node_type_name = NodeTypeNames[node_type]
        if self._args.worker_id:
            self._instance_id = self._args.worker_id
        else:
            self._instance_id = INSTANCE_ID_DEFAULT
        self._hostname = socket.gethostname()
        if self._args.dup:
            self._hostname += 'dup'
        self._sandesh = Sandesh()
        opserver_sandesh_req_impl = OpserverSandeshReqImpl(self)
        self._sandesh.init_generator(
            self._moduleid, self._hostname, self._node_type_name,
            self._instance_id, self._args.collectors, 'opserver_context',
            int(self._args.http_server_port), ['opserver.sandesh'],
            logger_class=self._args.logger_class,
            logger_config_file=self._args.logging_conf)
        self._sandesh.set_logging_params(
            enable_local_log=self._args.log_local,
            category=self._args.log_category,
            level=self._args.log_level,
            file=self._args.log_file,
            enable_syslog=self._args.use_syslog,
            syslog_facility=self._args.syslog_facility)
        ConnectionState.init(self._sandesh, self._hostname, self._moduleid,
            self._instance_id,
            staticmethod(ConnectionState.get_process_state_cb),
            NodeStatusUVE, NodeStatus)
        
        # Trace buffer list
        self.trace_buf = [
            {'name':'DiscoveryMsg', 'size':1000}
        ]
        # Create trace buffers 
        for buf in self.trace_buf:
            self._sandesh.trace_buffer_create(name=buf['name'], size=buf['size'])

        self._logger = self._sandesh._logger
        self._get_common = self._http_get_common
        self._put_common = self._http_put_common
        self._delete_common = self._http_delete_common
        self._post_common = self._http_post_common

        self._collector_pool = None
        self._state_server = OpStateServer(self._logger, self._args.redis_password)

        body = gevent.queue.Queue()
        self._uvedbstream = UveStreamer(self._logger, body, None, self.get_agp,
            self._args.partitions, self._args.redis_password)
        self._uvedbcache = UveCacheProcessor(self._logger, body, self._args.partitions)

        self._uve_server = UVEServer(('127.0.0.1',
                                      self._args.redis_server_port),
                                     self._logger,
                                     self._args.redis_password,
                                     self._uvedbcache)

        self._LEVEL_LIST = []
        for k in SandeshLevel._VALUES_TO_NAMES:
            if (k < SandeshLevel.UT_START):
                d = {}
                d[k] = SandeshLevel._VALUES_TO_NAMES[k]
                self._LEVEL_LIST.append(d)
        self._CATEGORY_MAP =\
            dict((ModuleNames[k], [CategoryNames[ce] for ce in v])
                 for k, v in ModuleCategoryMap.iteritems())

        self.disc = None
        self.agp = {}
        # TODO: Fix kafka provisioning before setting connection state down
        ConnectionState.update(conn_type = ConnectionType.UVEPARTITIONS,
            name = 'UVE-Aggregation', status = ConnectionStatus.UP)
        if self._args.disc_server_ip:
            self.disc_publish()
        else:
            for part in range(0,self._args.partitions):
                pi = PartInfo(ip_address = self._args.host_ip,
                              acq_time = UTCTimestampUsec(),
                              instance_id = "0",
                              port = self._args.redis_server_port)
                self.agp[part] = pi
            self.redis_uve_list = []
            try:
                if type(self._args.redis_uve_list) is str:
                    self._args.redis_uve_list = self._args.redis_uve_list.split()
                for redis_uve in self._args.redis_uve_list:
                    redis_ip_port = redis_uve.split(':')
                    redis_elem = (redis_ip_port[0], int(redis_ip_port[1]),0)
                    self.redis_uve_list.append(redis_elem)
            except Exception as e:
                self._logger.error('Failed to parse redis_uve_list: %s' % e)
            else:
                self._state_server.update_redis_list(self.redis_uve_list)
                self._uve_server.update_redis_uve_list(self.redis_uve_list)

        self._analytics_links = ['uves', 'alarms', 'tables', 'queries']

        self._VIRTUAL_TABLES = copy.deepcopy(_TABLES)

        self._ALARM_TYPES = {}
        for uk,uv in UVE_MAP.iteritems():
            mgr = hook.HookManager(
                namespace='contrail.analytics.alarms',
                name=uv,
                invoke_on_load=True,
                invoke_args=()
            )
            self._ALARM_TYPES[uv] = {}
            for extn in mgr[uv]:
                self._logger.info('Loaded extensions for %s: %s,%s doc %s' % \
                    (uv, extn.name, extn.entry_point_target, extn.obj.__doc__))
                ty = extn.entry_point_target.rsplit(":",1)[1]
                self._ALARM_TYPES[uv][ty]  = extn.obj.__doc__
           
        for t in _OBJECT_TABLES:
            obj = query_table(
                name=t, display_name=_OBJECT_TABLES[t].objtable_display_name,
                schema=_OBJECT_TABLE_SCHEMA,
                columnvalues=_OBJECT_TABLE_COLUMN_VALUES)
            self._VIRTUAL_TABLES.append(obj)

        for t in _STAT_TABLES:
            stat_id = t.stat_type + "." + t.stat_attr
            scols = []

            keyln = stat_query_column(name=STAT_SOURCE_FIELD, datatype='string', index=True)
            scols.append(keyln)

            tln = stat_query_column(name=STAT_TIME_FIELD, datatype='int', index=False)
            scols.append(tln)

            tcln = stat_query_column(name="CLASS(" + STAT_TIME_FIELD + ")", 
                     datatype='int', index=False)
            scols.append(tcln)

            teln = stat_query_column(name=STAT_TIMEBIN_FIELD, datatype='int', index=False)
            scols.append(teln)

            tecln = stat_query_column(name="CLASS(" + STAT_TIMEBIN_FIELD+ ")", 
                     datatype='int', index=False)
            scols.append(tecln)

            uln = stat_query_column(name=STAT_UUID_FIELD, datatype='uuid', index=False)
            scols.append(uln)

            cln = stat_query_column(name="COUNT(" + t.stat_attr + ")",
                    datatype='int', index=False)
            scols.append(cln)

            isname = False
            for aln in t.attributes:
                if aln.name==STAT_OBJECTID_FIELD:
                    isname = True
                scols.append(aln)
                if aln.datatype in ['int','double']:
                    sln = stat_query_column(name= "SUM(" + aln.name + ")",
                            datatype=aln.datatype, index=False)
                    scols.append(sln)
                    scln = stat_query_column(name= "CLASS(" + aln.name + ")",
                            datatype=aln.datatype, index=False)
                    scols.append(scln)
                    sln = stat_query_column(name= "MAX(" + aln.name + ")",
                            datatype=aln.datatype, index=False)
                    scols.append(sln)
                    scln = stat_query_column(name= "MIN(" + aln.name + ")",
                            datatype=aln.datatype, index=False)
                    scols.append(scln)

            if not isname: 
                keyln = stat_query_column(name=STAT_OBJECTID_FIELD, datatype='string', index=True)
                scols.append(keyln)

            sch = query_schema_type(type='STAT', columns=scols)

            stt = query_table(
                name = STAT_VT_PREFIX + "." + stat_id,
                display_name = t.display_name,
                schema = sch,
                columnvalues = [STAT_OBJECTID_FIELD, SOURCE])
            self._VIRTUAL_TABLES.append(stt)

        self._analytics_db = AnalyticsDb(self._logger,
                                         self._args.cassandra_server_list,
                                         self._args.redis_query_port,
                                         self._args.redis_password,
                                         self._args.cassandra_user,
                                         self._args.cassandra_password)

        bottle.route('/', 'GET', self.homepage_http_get)
        bottle.route('/analytics', 'GET', self.analytics_http_get)
        bottle.route('/analytics/uves', 'GET', self.uves_http_get)
        bottle.route('/analytics/alarms', 'GET', self.alarms_http_get)
        bottle.route('/analytics/alarms/acknowledge', 'POST',
            self.alarms_ack_http_post)
        bottle.route('/analytics/query', 'POST', self.query_process)
        bottle.route(
            '/analytics/query/<queryId>', 'GET', self.query_status_get)
        bottle.route('/analytics/query/<queryId>/chunk-final/<chunkId>',
                     'GET', self.query_chunk_get)
        bottle.route('/analytics/queries', 'GET', self.show_queries)
        bottle.route('/analytics/tables', 'GET', self.tables_process)
        bottle.route('/analytics/operation/database-purge',
                     'POST', self.process_purge_request)
        bottle.route('/analytics/operation/analytics-data-start-time',
	             'GET', self._get_analytics_data_start_time)
        bottle.route('/analytics/table/<table>', 'GET', self.table_process)
        bottle.route('/analytics/table/<table>/schema',
                     'GET', self.table_schema_process)
        for i in range(0, len(self._VIRTUAL_TABLES)):
            if len(self._VIRTUAL_TABLES[i].columnvalues) > 0:
                bottle.route('/analytics/table/<table>/column-values',
                             'GET', self.column_values_process)
                bottle.route('/analytics/table/<table>/column-values/<column>',
                             'GET', self.column_process)
        bottle.route('/analytics/send-tracebuffer/<source>/<module>/<instance_id>/<name>',
                     'GET', self.send_trace_buffer)
        bottle.route('/documentation/<filename:path>',
                     'GET', self.documentation_http_get)
        bottle.route('/analytics/uve-stream', 'GET', self.uve_stream)

        bottle.route('/analytics/<uvealarm>/<tables>', 'GET', self.dyn_list_http_get)
        bottle.route('/analytics/<uvealarm>/<table>/<name:path>', 'GET', self.dyn_http_get)
        bottle.route('/analytics/<uvealarm>/<tables>', 'POST', self.dyn_http_post)
        bottle.route('/analytics/alarms/<tables>/types', 'GET', self._uve_alarm_http_types)

        # start gevent to monitor disk usage and automatically purge
        if (self._args.auto_db_purge):
            gevent.spawn(self._auto_purge)

    # end __init__

    def dyn_http_get(self, uvealarm, table, name):
        is_alarm = None
        if uvealarm == "uves":
            is_alarm = False
        elif uvealarm == "alarms":
            is_alarm = True
        else:
            return {}
        return self._uve_alarm_http_get(table, name, is_alarm)

    def dyn_list_http_get(self, uvealarm, tables):
        is_alarm = None
        if uvealarm == "uves":
            is_alarm = False
        elif uvealarm == "alarms":
            is_alarm = True
        else:
            return []
        return self._uve_alarm_list_http_get(is_alarm)

    def dyn_http_post(self, uvealarm, tables):
        is_alarm = None
        if uvealarm == "uves":
            is_alarm = False
        elif uvealarm == "alarms":
            is_alarm = True
        else:
            return {}
        return self._uve_alarm_http_post(is_alarm)

    def _parse_args(self, args_str=' '.join(sys.argv[1:])):
        '''
        Eg. python opserver.py --host_ip 127.0.0.1
                               --redis_server_port 6379
                               --redis_query_port 6379
                               --redis_password
                               --collectors 127.0.0.1:8086
                               --cassandra_server_list 127.0.0.1:9160
                               --http_server_port 8090
                               --rest_api_port 8081
                               --rest_api_ip 0.0.0.0
                               --log_local
                               --log_level SYS_DEBUG
                               --log_category test
                               --log_file <stdout>
                               --use_syslog
                               --syslog_facility LOG_USER
                               --worker_id 0
                               --partitions 5
                               --redis_uve_list 127.0.0.1:6379
                               --auto_db_purge
        '''
        # Source any specified config/ini file
        # Turn off help, so we print all options in response to -h
        conf_parser = argparse.ArgumentParser(add_help=False)

        conf_parser.add_argument("-c", "--conf_file", action='append',
                                 help="Specify config file", metavar="FILE")
        args, remaining_argv = conf_parser.parse_known_args(args_str.split())

        defaults = {
            'host_ip'            : "127.0.0.1",
            'collectors'         : ['127.0.0.1:8086'],
            'cassandra_server_list' : ['127.0.0.1:9160'],
            'http_server_port'   : 8090,
            'rest_api_port'      : 8081,
            'rest_api_ip'        : '0.0.0.0',
            'log_local'          : False,
            'log_level'          : 'SYS_DEBUG',
            'log_category'       : '',
            'log_file'           : Sandesh._DEFAULT_LOG_FILE,
            'use_syslog'         : False,
            'syslog_facility'    : Sandesh._DEFAULT_SYSLOG_FACILITY,
            'dup'                : False,
            'redis_uve_list'     : ['127.0.0.1:6379'],
            'auto_db_purge'      : True,
            'db_purge_threshold' : 70,
            'db_purge_level'     : 40,
            'analytics_data_ttl' : 48,
            'analytics_config_audit_ttl' : -1,
            'analytics_statistics_ttl' : -1,
            'analytics_flow_ttl' : -1,
            'logging_conf': '',
            'logger_class': None,
            'partitions'        : 5,
        }
        redis_opts = {
            'redis_server_port'  : 6379,
            'redis_query_port'   : 6379,
            'redis_password'       : None,
        }
        disc_opts = {
            'disc_server_ip'     : None,
            'disc_server_port'   : 5998,
        }
        cassandra_opts = {
            'cassandra_user'     : None,
            'cassandra_password' : None,
        }

        # read contrail-analytics-api own conf file
        config = None
        if args.conf_file:
            config = ConfigParser.SafeConfigParser()
            config.read(args.conf_file)
            if 'DEFAULTS' in config.sections():
                defaults.update(dict(config.items("DEFAULTS")))
            if 'REDIS' in config.sections():
                redis_opts.update(dict(config.items('REDIS')))
            if 'DISCOVERY' in config.sections():
                disc_opts.update(dict(config.items('DISCOVERY')))
            if 'CASSANDRA' in config.sections():
                cassandra_opts.update(dict(config.items('CASSANDRA')))

        # update ttls
        if (defaults['analytics_config_audit_ttl'] == -1):
            defaults['analytics_config_audit_ttl'] = defaults['analytics_data_ttl']
        if (defaults['analytics_statistics_ttl'] == -1):
            defaults['analytics_statistics_ttl'] = defaults['analytics_data_ttl']
        if (defaults['analytics_flow_ttl'] == -1):
            defaults['analytics_flow_ttl'] = defaults['analytics_data_ttl']

        # Override with CLI options
        # Don't surpress add_help here so it will handle -h

        parser = argparse.ArgumentParser(
            # Inherit options from config_parser
            parents=[conf_parser],
            # print script description with -h/--help
            description=__doc__,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        defaults.update(redis_opts)
        defaults.update(disc_opts)
        defaults.update(cassandra_opts)
        defaults.update()
        parser.set_defaults(**defaults)

        parser.add_argument("--host_ip",
            help="Host IP address")
        parser.add_argument("--redis_server_port",
            type=int,
            help="Redis server port")
        parser.add_argument("--redis_query_port",
            type=int,
            help="Redis query port")
        parser.add_argument("--redis_password",
            help="Redis server password")
        parser.add_argument("--collectors",
            help="List of Collector IP addresses in ip:port format",
            nargs="+")
        parser.add_argument("--http_server_port",
            type=int,
            help="HTTP server port")
        parser.add_argument("--rest_api_port",
            type=int,
            help="REST API port")
        parser.add_argument("--rest_api_ip",
            help="REST API IP address")
        parser.add_argument("--log_local", action="store_true",
            help="Enable local logging of sandesh messages")
        parser.add_argument(
            "--log_level",  
            help="Severity level for local logging of sandesh messages")
        parser.add_argument(
            "--log_category", 
            help="Category filter for local logging of sandesh messages")
        parser.add_argument("--log_file",
            help="Filename for the logs to be written to")
        parser.add_argument("--use_syslog",
            action="store_true",
            help="Use syslog for logging")
        parser.add_argument("--syslog_facility",
            help="Syslog facility to receive log lines")
        parser.add_argument("--disc_server_ip",
            help="Discovery Server IP address")
        parser.add_argument("--disc_server_port",
            type=int,
            help="Discovery Server port")
        parser.add_argument("--dup", action="store_true",
            help="Internal use")
        parser.add_argument("--redis_uve_list",
            help="List of redis-uve in ip:port format. For internal use only",
            nargs="+")
        parser.add_argument(
            "--worker_id",
            help="Worker Id")
        parser.add_argument("--cassandra_server_list",
            help="List of cassandra_server_ip in ip:port format",
            nargs="+")
        parser.add_argument("--auto_db_purge", action="store_true",
            help="Automatically purge database if disk usage cross threshold")
        parser.add_argument(
            "--logging_conf",
            help=("Optional logging configuration file, default: None"))
        parser.add_argument(
            "--logger_class",
            help=("Optional external logger class, default: None"))
        parser.add_argument("--cassandra_user",
            help="Cassandra user name")
        parser.add_argument("--cassandra_password",
            help="Cassandra password")
        parser.add_argument("--partitions", type=int,
            help="Number of partitions for hashing UVE keys")

        self._args = parser.parse_args(remaining_argv)
        if type(self._args.collectors) is str:
            self._args.collectors = self._args.collectors.split()
        if type(self._args.redis_uve_list) is str:
            self._args.redis_uve_list = self._args.redis_uve_list.split()
        if type(self._args.cassandra_server_list) is str:
            self._args.cassandra_server_list = self._args.cassandra_server_list.split()
    # end _parse_args

    def get_args(self):
        return self._args
    # end get_args

    def get_http_server_port(self):
        return int(self._args.http_server_port)
    # end get_http_server_port

    def get_uve_server(self):
        return self._uve_server
    # end get_uve_server

    def homepage_http_get(self):
        json_body = {}
        json_links = []

        base_url = bottle.request.urlparts.scheme + \
            '://' + bottle.request.urlparts.netloc

        for link in self._homepage_links:
            json_links.append(
                {'link': obj_to_dict(
                    LinkObject(link.name, base_url + link.href))})

        json_body = \
            {"href": base_url,
             "links": json_links
             }

        return json_body
    # end homepage_http_get

    def uve_stream(self):
        bottle.response.set_header('Content-Type', 'text/event-stream')
        bottle.response.set_header('Cache-Control', 'no-cache')
        # This is needed to detect when the client hangs up
        rfile = bottle.request.environ['wsgi.input'].rfile

        body = gevent.queue.Queue()
        ph = UveStreamer(self._logger, body, rfile, self.get_agp,
            self._args.partitions, self._args.redis_password)
        ph.start()
        return body

    def documentation_http_get(self, filename):
        return bottle.static_file(
            filename, root='/usr/share/doc/contrail-analytics-api/html')
    # end documentation_http_get

    def _http_get_common(self, request):
        return (True, '')
    # end _http_get_common

    def _http_put_common(self, request, obj_dict):
        return (True, '')
    # end _http_put_common

    def _http_delete_common(self, request, id):
        return (True, '')
    # end _http_delete_common

    def _http_post_common(self, request, obj_dict):
        return (True, '')
    # end _http_post_common

    @staticmethod
    def _get_redis_query_ip_from_qid(qid):
        try:
            ip = qid.rsplit('-', 1)[1]
            redis_ip = socket.inet_ntop(socket.AF_INET, 
                            struct.pack('>I', int(ip, 16)))
        except Exception as err:
            return None
        return redis_ip
    # end _get_redis_query_ip_from_qid

    def _query_status(self, request, qid):
        resp = {}
        redis_query_ip = OpServer._get_redis_query_ip_from_qid(qid)
        if redis_query_ip is None:
            return bottle.HTTPError(_ERRORS[errno.EINVAL], 
                    'Invalid query id')
        try:
            resp = redis_query_status(host=redis_query_ip,
                                      port=int(self._args.redis_query_port),
                                      redis_password=self._args.redis_password,
                                      qid=qid)
        except redis.exceptions.ConnectionError:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Query[%s] status : Connection Error' % (qid),
                server_addrs = ['%s:%s' % (redis_query_ip, \
                    str(self._args.redis_query_port))])
            return bottle.HTTPError(_ERRORS[errno.EIO],
                    'Failure in connection to the query DB')
        except Exception as e:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Query[%s] status : Exception %s' % (qid, str(e)),
                server_addrs = ['%s:%s' % (redis_query_ip, \
                    str(self._args.redis_query_port))])
            self._logger.error("Exception: %s" % e)
            return bottle.HTTPError(_ERRORS[errno.EIO], 'Error: %s' % e)
        else:
            if resp is None:
                return bottle.HTTPError(_ERRORS[errno.ENOENT], 
                    'Invalid query id or Abandoned query id')
            resp_header = {'Content-Type': 'application/json'}
            resp_code = 200
            self._logger.debug("query [%s] status: %s" % (qid, resp))
            return bottle.HTTPResponse(
                json.dumps(resp), resp_code, resp_header)
    # end _query_status

    def _query_chunk(self, request, qid, chunk_id):
        redis_query_ip = OpServer._get_redis_query_ip_from_qid(qid)
        if redis_query_ip is None:
            yield bottle.HTTPError(_ERRORS[errno.EINVAL],
                    'Invalid query id')
        try:
            done = False
            gen = redis_query_chunk(host=redis_query_ip,
                                    port=int(self._args.redis_query_port),
                                    redis_password=self._args.redis_password,
                                    qid=qid, chunk_id=chunk_id)
            bottle.response.set_header('Content-Type', 'application/json')
            while not done:
                try:
                    yield gen.next()
                except StopIteration:
                    done = True
        except redis.exceptions.ConnectionError:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Query [%s] chunk #%d : Connection Error' % \
                    (qid, chunk_id),
                server_addrs = ['%s:%s' % (redis_query_ip, \
                    str(self._args.redis_query_port))])
            yield bottle.HTTPError(_ERRORS[errno.EIO],
                    'Failure in connection to the query DB')
        except Exception as e:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Query [%s] chunk #%d : Exception %s' % \
                    (qid, chunk_id, str(e)),
                server_addrs = ['%s:%s' % (redis_query_ip, \
                    str(self._args.redis_query_port))])
            self._logger.error("Exception: %s" % str(e))
            yield bottle.HTTPError(_ERRORS[errno.ENOENT], 'Error: %s' % e)
        else:
            self._logger.info(
                "Query [%s] chunk #%d read at time %d"
                % (qid, chunk_id, time.time()))
    # end _query_chunk

    def _query(self, request):
        reply = {}
        try:
            redis_query_ip, = struct.unpack('>I', socket.inet_pton(
                                        socket.AF_INET, self._args.host_ip))
            qid = str(uuid.uuid1(redis_query_ip))
            self._logger.info("Starting Query %s" % qid)

            tabl = ""
            for key, value in request.json.iteritems():
                if key == "table":
                    tabl = value

            self._logger.info("Table is " + tabl)

            tabn = None
            for i in range(0, len(self._VIRTUAL_TABLES)):
                if self._VIRTUAL_TABLES[i].name == tabl:
                    tabn = i

            if (tabn is not None):
                tabtypes = {}
                for cols in self._VIRTUAL_TABLES[tabn].schema.columns:
                    if cols.datatype in ['long', 'int']:
                        tabtypes[cols.name] = 'int'
                    elif cols.datatype in ['ipv4']:
                        tabtypes[cols.name] = 'ipv4'
                    else:
                        tabtypes[cols.name] = 'string'

                self._logger.info(str(tabtypes))

            if (tabn is None):
                if not tabl.startswith("StatTable."):
                    reply = bottle.HTTPError(_ERRORS[errno.ENOENT], 
                                'Table %s not found' % tabl)
                    yield reply
                    return
                else:
                    self._logger.info("Schema not known for dynamic table %s" % tabl)

            if tabl == OVERLAY_TO_UNDERLAY_FLOW_MAP:
                overlay_to_underlay_map = OverlayToUnderlayMapper(
                    request.json, self._args.host_ip,
                    self._args.rest_api_port, self._logger)
                try:
                    yield overlay_to_underlay_map.process_query()
                except OverlayToUnderlayMapperError as e:
                    yield bottle.HTTPError(_ERRORS[errno.EIO], str(e))
                return

            prg = redis_query_start('127.0.0.1',
                                    int(self._args.redis_query_port),
                                    self._args.redis_password,
                                    qid, request.json)
            if prg is None:
                # Update connection info
                ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                    name = 'Query', status = ConnectionStatus.DOWN,
                    message = 'Query[%s] Query Engine not responding' % qid,
                    server_addrs = ['127.0.0.1' + ':' + 
                        str(self._args.redis_query_port)])  
                self._logger.error('QE Not Responding')
                yield bottle.HTTPError(_ERRORS[errno.EBUSY], 
                        'Query Engine is not responding')
                return

        except redis.exceptions.ConnectionError:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Query[%s] Connection Error' % (qid),
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)])  
            yield bottle.HTTPError(_ERRORS[errno.EIO],
                    'Failure in connection to the query DB')
        except Exception as e:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Query[%s] Exception: %s' % (qid, str(e)),
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)])  
            self._logger.error("Exception: %s" % str(e))
            yield bottle.HTTPError(_ERRORS[errno.EIO],
                    'Error: %s' % e)
        else:
            redish = None
            if prg < 0:
                cod = -prg
                self._logger.error(
                    "Query Failed. Found Error %s" % errno.errorcode[cod])
                reply = bottle.HTTPError(_ERRORS[cod], errno.errorcode[cod])
                yield reply
            else:
                self._logger.info(
                    "Query Accepted at time %d , Progress %d"
                    % (time.time(), prg))
                # In Async mode, we should return with "202 Accepted" here
                # and also give back the status URI "/analytic/query/<qid>"
                # OpServers's client will poll the status URI
                if request.get_header('Expect') == '202-accepted' or\
                   request.get_header('Postman-Expect') == '202-accepted':
                    href = '/analytics/query/%s' % (qid)
                    resp_data = json.dumps({'href': href})
                    yield bottle.HTTPResponse(
                        resp_data, 202, {'Content-type': 'application/json'})
                else:
                    for gen in self._sync_query(request, qid):
                        yield gen
    # end _query

    def _sync_query(self, request, qid):
        # In Sync mode, Keep polling query status until final result is
        # available
        try:
            self._logger.info("Polling %s for query result" % ("REPLY:" + qid))
            prg = 0
            done = False
            while not done:
                gevent.sleep(1)
                resp = redis_query_status(host='127.0.0.1',
                                          port=int(
                                              self._args.redis_query_port),
                                          redis_password=self._args.redis_password,
                                          qid=qid)

                # We want to print progress only if it has changed
                if int(resp["progress"]) == prg:
                    continue

                self._logger.info(
                    "Query Progress is %s time %d" % (str(resp), time.time()))
                prg = int(resp["progress"])

                # Either there was an error, or the query is complete
                if (prg < 0) or (prg == 100):
                    done = True

            if prg < 0:
                cod = -prg
                self._logger.error("Found Error %s" % errno.errorcode[cod])
                reply = bottle.HTTPError(_ERRORS[cod], errno.errorcode[cod])
                yield reply
                return

            # In Sync mode, its time to read the final result. Status is in
            # "resp"
            done = False
            gen = redis_query_result(host='127.0.0.1',
                                     port=int(self._args.redis_query_port),
                                     redis_password=self._args.redis_password,
                                     qid=qid)
            bottle.response.set_header('Content-Type', 'application/json')
            while not done:
                try:
                    yield gen.next()
                except StopIteration:
                    done = True
            '''
            final_res = {}
            prg, final_res['value'] =\
                redis_query_result_dict(host=self._args.redis_server_ip,
                                        port=int(self._args.redis_query_port),
                                        qid=qid)
            yield json.dumps(final_res)
            '''

        except redis.exceptions.ConnectionError:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Sync Query[%s] Connection Error' % qid,
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)])  
            yield bottle.HTTPError(_ERRORS[errno.EIO],
                    'Failure in connection to the query DB')
        except Exception as e:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Sync Query[%s] Exception: %s' % (qid, str(e)),
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)])  
            self._logger.error("Exception: %s" % str(e))
            yield bottle.HTTPError(_ERRORS[errno.EIO], 
                    'Error: %s' % e)
        else:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.UP,
                message = None,
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)]) 
            self._logger.info(
                "Query Result available at time %d" % time.time())
        return
    # end _sync_query

    def query_process(self):
        self._post_common(bottle.request, None)
        result = self._query(bottle.request)
        return result
    # end query_process

    def query_status_get(self, queryId):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)
        return self._query_status(bottle.request, queryId)
    # end query_status_get

    def query_chunk_get(self, queryId, chunkId):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)
        return self._query_chunk(bottle.request, queryId, int(chunkId))
    # end query_chunk_get

    def show_queries(self):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)
        queries = {}
        try:
            redish = redis.StrictRedis(db=0, host='127.0.0.1',
                                       port=int(self._args.redis_query_port),
                                       password=self._args.redis_password)
            pending_queries = redish.lrange('QUERYQ', 0, -1)
            pending_queries_info = []
            for query_id in pending_queries:
                query_data = redis_query_info(redish, query_id)
                pending_queries_info.append(query_data)
            queries['pending_queries'] = pending_queries_info

            processing_queries = redish.lrange(
                'ENGINE:' + socket.gethostname(), 0, -1)
            processing_queries_info = []
            abandoned_queries_info = []
            error_queries_info = []
            for query_id in processing_queries:
                status = redis_query_status(host='127.0.0.1',
                                            port=int(
                                                self._args.redis_query_port),
                                            redis_password=self._args.redis_password,
                                            qid=query_id)
                query_data = redis_query_info(redish, query_id)
                if status is None:
                     abandoned_queries_info.append(query_data)
                elif status['progress'] < 0:
                     query_data['error_code'] = status['progress']
                     error_queries_info.append(query_data)
                else:
                     query_data['progress'] = status['progress']
                     processing_queries_info.append(query_data)
            queries['queries_being_processed'] = processing_queries_info
            queries['abandoned_queries'] = abandoned_queries_info
            queries['error_queries'] = error_queries_info
        except redis.exceptions.ConnectionError:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Show queries : Connection Error',
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)])  
            return bottle.HTTPError(_ERRORS[errno.EIO],
                    'Failure in connection to the query DB')
        except Exception as err:
            # Update connection info
            ConnectionState.update(conn_type = ConnectionType.REDIS_QUERY,
                name = 'Query', status = ConnectionStatus.DOWN,
                message = 'Show queries : Exception %s' % str(err),
                server_addrs = ['127.0.0.1' + ':' + 
                    str(self._args.redis_query_port)])  
            self._logger.error("Exception in show queries: %s" % str(err))
            return bottle.HTTPError(_ERRORS[errno.EIO], 'Error: %s' % err)
        else:
            bottle.response.set_header('Content-Type', 'application/json')
            return json.dumps(queries)
    # end show_queries

    @staticmethod
    def _get_tfilter(cfilt):
        tfilter = {}
        for tfilt in cfilt:
            afilt = tfilt.split(':')
            try:
                attr_list = tfilter[afilt[0]]
            except KeyError:
                tfilter[afilt[0]] = set()
                attr_list = tfilter[afilt[0]]
            finally:
                if len(afilt) > 1:
                    attr_list.add(afilt[1])
                    tfilter[afilt[0]] = attr_list
        return tfilter
    # end _get_tfilter

    @staticmethod
    def _uve_filter_set(req):
        filters = {}
        filters['sfilt'] = req.get('sfilt')
        filters['mfilt'] = req.get('mfilt')
        if req.get('cfilt'):
            infos = req['cfilt'].split(',')
            filters['cfilt'] = OpServer._get_tfilter(infos)
        else:
            filters['cfilt'] = None
        if req.get('kfilt'):
            filters['kfilt'] = req['kfilt'].split(',')
        else:
            filters['kfilt'] = None
        filters['ackfilt'] = req.get('ackfilt')
        if filters['ackfilt'] is not None:
            if filters['ackfilt'] != 'true' and filters['ackfilt'] != 'false':
                raise ValueError('Invalid ackfilt. ackfilt must be true|false')
        return filters
    # end _uve_filter_set

    @staticmethod
    def _uve_http_post_filter_set(req):
        filters = {}
        try:
            filters['kfilt'] = req['kfilt']
            if not isinstance(filters['kfilt'], list):
                raise ValueError('Invalid kfilt')
        except KeyError:
            filters['kfilt'] = ['*']
        filters['sfilt'] = req.get('sfilt')
        filters['mfilt'] = req.get('mfilt')
        try:
            cfilt = req['cfilt']
            if not isinstance(cfilt, list):
                raise ValueError('Invalid cfilt')
        except KeyError:
            filters['cfilt'] = None
        else:
            filters['cfilt'] = OpServer._get_tfilter(cfilt)
        try:
            ackfilt = req['ackfilt']
        except KeyError:
            filters['ackfilt'] = None
        else:
            if not isinstance(ackfilt, bool):
                raise ValueError('Invalid ackfilt. ackfilt must be bool')
            filters['ackfilt'] = 'true' if ackfilt else 'false'
        return filters
    # end _uve_http_post_filter_set

    def _uve_alarm_http_post(self, is_alarm):
        (ok, result) = self._post_common(bottle.request, None)
        base_url = bottle.request.urlparts.scheme + \
            '://' + bottle.request.urlparts.netloc
        if not ok:
            (code, msg) = result
            abort(code, msg)
        uve_type = bottle.request.url.rsplit('/', 1)[1]
        try:
            uve_tbl = UVE_MAP[uve_type]
        except Exception as e:
            yield bottle.HTTPError(_ERRORS[errno.EINVAL], 
                                   'Invalid table name')
        else:
            try:
                req = bottle.request.json
                filters = OpServer._uve_http_post_filter_set(req)
            except Exception as err:
                yield bottle.HTTPError(_ERRORS[errno.EBADMSG], err)
            bottle.response.set_header('Content-Type', 'application/json')
            yield u'{"value": ['
            first = True
            for key in filters['kfilt']:
                if key.find('*') != -1:
                    for gen in self._uve_server.multi_uve_get(uve_tbl, True,
                                                              filters,
                                                              is_alarm,
                                                              base_url):
                        if first:
                            yield u'' + json.dumps(gen)
                            first = False
                        else:
                            yield u', ' + json.dumps(gen)
                    yield u']}'
                    return
            first = True
            for key in filters['kfilt']:
                uve_name = uve_tbl + ':' + key
                _, rsp = self._uve_server.get_uve(uve_name, True, filters,
                                               is_alarm=is_alarm,
                                               base_url=base_url)
                if rsp != {}:
                    data = {'name': key, 'value': rsp}
                    if first:
                        yield u'' + json.dumps(data)
                        first = False
                    else:
                        yield u', ' + json.dumps(data)
            yield u']}'
    # end _uve_alarm_http_post

    def _uve_alarm_http_get(self, uve_type, name, is_alarm):
        # common handling for all resource get
        (ok, result) = self._get_common(bottle.request)
        base_url = bottle.request.urlparts.scheme + \
            '://' + bottle.request.urlparts.netloc
        if not ok:
            (code, msg) = result
            abort(code, msg)
        uve_tbl = uve_type
        if uve_type in UVE_MAP:
            uve_tbl = UVE_MAP[uve_type]

        bottle.response.set_header('Content-Type', 'application/json')
        uve_name = uve_tbl + ':' + name
        req = bottle.request.query
        try:
            filters = OpServer._uve_filter_set(req)
        except Exception as e:
            yield bottle.HTTPError(_ERRORS[errno.EBADMSG], e)
        
        flat = False
        if 'flat' in req.keys() or any(filters.values()):
            flat = True

        uve_name = uve_tbl + ':' + name
        if name.find('*') != -1:
            flat = True
            yield u'{"value": ['
            first = True
            if filters['kfilt'] is None:
                filters['kfilt'] = [name]
            for gen in self._uve_server.multi_uve_get(uve_tbl, flat,
                                                      filters, is_alarm, base_url):
                if first:
                    yield u'' + json.dumps(gen)
                    first = False
                else:
                    yield u', ' + json.dumps(gen)
            yield u']}'
        else:
            _, rsp = self._uve_server.get_uve(uve_name, flat, filters,
                                           is_alarm=is_alarm,
                                           base_url=base_url)
            yield json.dumps(rsp)
    # end _uve_alarm_http_get

    def _uve_alarm_http_types(self):
        # common handling for all resource get
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)
        arg_line = bottle.request.url.rsplit('/', 2)[1]
        uve_type = arg_line[:-1]

        bottle.response.set_header('Content-Type', 'application/json')
        ret = None
        try:
            uve_tbl = UVE_MAP[uve_type]
            ret = self._ALARM_TYPES[uve_tbl] 
        except Exception as e:
            return {}
        else:
            return json.dumps(ret)

    def _uve_alarm_list_http_get(self, is_alarm):
        # common handling for all resource get
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)
        arg_line = bottle.request.url.rsplit('/', 1)[1]
        uve_args = arg_line.split('?')
        uve_type = uve_args[0][:-1]
        if len(uve_args) != 1:
            uve_filters = ''
            filters = uve_args[1].split('&')
            filters = \
                [filt for filt in filters if filt[:len('kfilt')] != 'kfilt']
            if len(filters):
                uve_filters = '&'.join(filters)
            else:
                uve_filters = 'flat'
        else:
            uve_filters = 'flat'

        bottle.response.set_header('Content-Type', 'application/json')
        try:
            uve_tbl = UVE_MAP[uve_type]
        except Exception as e:
            return {}
        else:
            req = bottle.request.query
            try:
                filters = OpServer._uve_filter_set(req)
            except Exception as e:
                return bottle.HTTPError(_ERRORS[errno.EBADMSG], e)
            uve_list = self._uve_server.get_uve_list(
                uve_tbl, filters, True, is_alarm)
            uve_or_alarm = 'alarms' if is_alarm else 'uves'
            base_url = bottle.request.urlparts.scheme + '://' + \
                bottle.request.urlparts.netloc + \
                '/analytics/%s/%s/' % (uve_or_alarm, uve_type)
            uve_links =\
                [obj_to_dict(LinkObject(uve,
                                        base_url + uve + "?" + uve_filters))
                 for uve in uve_list]
            return json.dumps(uve_links)
    # end _uve_alarm_list_http_get

    def analytics_http_get(self):
        # common handling for all resource get
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        base_url = bottle.request.urlparts.scheme + '://' + \
            bottle.request.urlparts.netloc + '/analytics/'
        analytics_links = [obj_to_dict(LinkObject(link, base_url + link))
                           for link in self._analytics_links]
        bottle.response.set_header('Content-Type', 'application/json')
        return json.dumps(analytics_links)
    # end analytics_http_get

    def _uves_alarms_http_get(self, is_alarm):
        # common handling for all resource get
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        uve_or_alarm = 'alarms' if is_alarm else 'uves'
        base_url = bottle.request.urlparts.scheme + '://' + \
            bottle.request.urlparts.netloc + '/analytics/%s/' % (uve_or_alarm)
        uvetype_links = []
        for uvetype in UVE_MAP:
            entry = obj_to_dict(LinkObject(uvetype + 's',
                                base_url + uvetype + 's'))
            if is_alarm:
                entry['type'] = base_url + uvetype + 's/types'
            uvetype_links.append(entry)
            
        bottle.response.set_header('Content-Type', 'application/json')
        return json.dumps(uvetype_links)
    # end _uves_alarms_http_get

    def uves_http_get(self):
        return self._uves_alarms_http_get(is_alarm=False)
    # end uves_http_get

    def alarms_http_get(self):
        return self._uves_alarms_http_get(is_alarm=True)
    # end alarms_http_get

    def alarms_ack_http_post(self):
        self._post_common(bottle.request, None)
        if ('application/json' not in bottle.request.headers['Content-Type']):
            self._logger.error('Content-type is not JSON')
            return bottle.HTTPError(_ERRORS[errno.EBADMSG],
                'Content-Type must be JSON')
        self._logger.info('Alarm Acknowledge request: %s' % 
            (bottle.request.json))
        alarm_ack_fields = set(['table', 'name', 'type', 'token'])
        bottle_req_fields = set(bottle.request.json.keys())
        if len(alarm_ack_fields - bottle_req_fields):
            return bottle.HTTPError(_ERRORS[errno.EINVAL],
                'Alarm acknowledge request does not contain the fields '
                '{%s}' % (', '.join(alarm_ack_fields - bottle_req_fields)))
        # Decode generator ip, introspect port and timestamp from the
        # the token field.
        try:
            token = json.loads(base64.b64decode(bottle.request.json['token']))
        except (TypeError, ValueError):
            self._logger.error('Alarm Ack Request: Failed to decode "token"')
            return bottle.HTTPError(_ERRORS[errno.EINVAL],
                'Failed to decode "token"')
        exp_token_fields = set(['host_ip', 'http_port', 'timestamp'])
        actual_token_fields = set(token.keys())
        if len(exp_token_fields - actual_token_fields):
            self._logger.error('Alarm Ack Request: Invalid token value')
            return bottle.HTTPError(_ERRORS[errno.EINVAL],
                'Invalid token value')
        generator_introspect = GeneratorIntrospectUtil(token['host_ip'],
                                                       token['http_port'])
        try:
            res = generator_introspect.send_alarm_ack_request(
                bottle.request.json['table'], bottle.request.json['name'],
                bottle.request.json['type'], token['timestamp'])
        except Exception as e:
            self._logger.error('Alarm Ack Request: Introspect request failed')
            return bottle.HTTPError(_ERRORS[errno.EBUSY],
                'Failed to process the Alarm Ack Request')
        self._logger.debug('Alarm Ack Response: %s' % (res))
        if res['status'] == 'false':
            return bottle.HTTPError(_ERRORS[errno.EIO], res['error_msg'])
        self._logger.info('Alarm Ack Request successfully processed')
        return bottle.HTTPResponse(status=200)
    # end alarms_ack_http_post

    def send_trace_buffer(self, source, module, instance_id, name):
        response = {}
        trace_req = SandeshTraceRequest(name)
        if module not in ModuleIds:
            response['status'] = 'fail'
            response['error'] = 'Invalid module'
            return json.dumps(response)
        module_id = ModuleIds[module]
        node_type = Module2NodeType[module_id]
        node_type_name = NodeTypeNames[node_type]
        if self._state_server.redis_publish(msg_type='send-tracebuffer',
                                            destination=source + ':' + 
                                            node_type_name + ':' + module +
                                            ':' + instance_id,
                                            msg=trace_req):
            response['status'] = 'pass'
        else:
            response['status'] = 'fail'
            response['error'] = 'No connection to Redis'
        return json.dumps(response)
    # end send_trace_buffer

    def tables_process(self):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        base_url = bottle.request.urlparts.scheme + '://' + \
            bottle.request.urlparts.netloc + '/analytics/table/'
        json_links = []
        for i in range(0, len(self._VIRTUAL_TABLES)):
            link = LinkObject(self._VIRTUAL_TABLES[
                              i].name, base_url + self._VIRTUAL_TABLES[i].name)
            tbl_info = obj_to_dict(link)
            tbl_info['type'] = self._VIRTUAL_TABLES[i].schema.type
            if (self._VIRTUAL_TABLES[i].display_name is not None):
                    tbl_info['display_name'] =\
                        self._VIRTUAL_TABLES[i].display_name
            json_links.append(tbl_info)

        bottle.response.set_header('Content-Type', 'application/json')
        return json.dumps(json_links)
    # end tables_process

    def get_purge_cutoff(self, purge_input, start_times):
        # currently not use analytics start time
        # purge_input is assumed to be percent of time since
        # TTL for which data has to be purged
        purge_cutoff = {}
        current_time = UTCTimestampUsec()

        self._logger.error("start times:" + str(start_times))
        analytics_time_range = min(
                (current_time - start_times[SYSTEM_OBJECT_START_TIME]),
                60*60*1000000*self._args.analytics_data_ttl)
        flow_time_range = min(
                (current_time - start_times[SYSTEM_OBJECT_FLOW_START_TIME]),
                60*60*1000000*self._args.analytics_flow_ttl)
        stat_time_range = min(
                (current_time - start_times[SYSTEM_OBJECT_STAT_START_TIME]),
                60*60*1000000*self._args.analytics_statistics_ttl)
        # currently using config audit TTL for message table (to be changed)
        msg_time_range = min(
                (current_time - start_times[SYSTEM_OBJECT_MSG_START_TIME]),
                60*60*1000000*self._args.analytics_config_audit_ttl)

        purge_cutoff['flow_cutoff'] = int(current_time - (float(100 - purge_input)*
                float(flow_time_range)/100.0))
        purge_cutoff['stats_cutoff'] = int(current_time - (float(100 - purge_input)*
                float(stat_time_range)/100.0))
        purge_cutoff['msg_cutoff'] = int(current_time - (float(100 - purge_input)*
                float(msg_time_range)/100.0))
        purge_cutoff['other_cutoff'] = int(current_time - (float(100 - purge_input)*
                float(analytics_time_range)/100.0))

        return purge_cutoff
    #end get_purge_cutoff

    def process_purge_request(self):
        self._post_common(bottle.request, None)

        if ("application/json" not in bottle.request.headers['Content-Type']):
            self._logger.error('Content-type is not JSON')
            response = {
                'status': 'failed', 'reason': 'Content-type is not JSON'}
            return bottle.HTTPResponse(
                json.dumps(response), _ERRORS[errno.EBADMSG],
                {'Content-type': 'application/json'})

        start_times = self._analytics_db._get_analytics_start_time()
        if (start_times == None):
            self._logger.info("Failed to get the analytics start time")
            response = {'status': 'failed',
                        'reason': 'Failed to get the analytics start time'}
            return bottle.HTTPResponse(
                        json.dumps(response), _ERRORS[errno.EIO],
                        {'Content-type': 'application/json'})
        analytics_start_time = start_times[SYSTEM_OBJECT_START_TIME]

        purge_cutoff = {}
        if ("purge_input" in bottle.request.json.keys()):
            value = bottle.request.json["purge_input"]
            if (type(value) is int):
                if ((value <= 100) and (value > 0)):
                    purge_cutoff = self.get_purge_cutoff(float(value), start_times)
                else:
                    response = {'status': 'failed',
                        'reason': 'Valid % range is [1, 100]'}
                    return bottle.HTTPResponse(
                        json.dumps(response), _ERRORS[errno.EBADMSG],
                        {'Content-type': 'application/json'})
            elif (type(value) is unicode):
                try:
                    purge_input = OpServerUtils.convert_to_utc_timestamp_usec(value)

                    if (purge_input <= analytics_start_time):
                        response = {'status': 'failed',
                            'reason': 'purge input is less than analytics start time'}
                        return bottle.HTTPResponse(
                                json.dumps(response), _ERRORS[errno.EIO],
                                {'Content-type': 'application/json'})

                    # cutoff time for purging flow data
                    purge_cutoff['flow_cutoff'] = purge_input
                    # cutoff time for purging stats data
                    purge_cutoff['stats_cutoff'] = purge_input
                    # cutoff time for purging message tables
                    purge_cutoff['msg_cutoff'] = purge_input
                    # cutoff time for purging other tables
                    purge_cutoff['other_cutoff'] = purge_input

                except:
                    response = {'status': 'failed',
                   'reason': 'Valid time formats are: \'%Y %b %d %H:%M:%S.%f\', '
                   '\'now\', \'now-h/m/s\', \'-/h/m/s\' in  purge_input'}
                    return bottle.HTTPResponse(
                        json.dumps(response), _ERRORS[errno.EBADMSG],
                        {'Content-type': 'application/json'})
            else:
                response = {'status': 'failed',
                    'reason': 'Valid purge_input format is % or time'}
                return bottle.HTTPResponse(
                    json.dumps(response), _ERRORS[errno.EBADMSG],
                    {'Content-type': 'application/json'})
        else:
            response = {'status': 'failed',
                        'reason': 'purge_input not specified'}
            return bottle.HTTPResponse(
                json.dumps(response), _ERRORS[errno.EBADMSG],
                {'Content-type': 'application/json'})

        res = self._analytics_db.get_analytics_db_purge_status(
                  self._state_server._redis_list)

        if (res == None):
            purge_request_ip, = struct.unpack('>I', socket.inet_pton(
                                        socket.AF_INET, self._args.host_ip))
            purge_id = str(uuid.uuid1(purge_request_ip))
            resp = self._analytics_db.set_analytics_db_purge_status(purge_id,
                            purge_cutoff)
            if (resp == None):
                gevent.spawn(self.db_purge_operation, purge_cutoff, purge_id)
                response = {'status': 'started', 'purge_id': purge_id}
                return bottle.HTTPResponse(json.dumps(response), 200,
                                   {'Content-type': 'application/json'})
            elif (resp['status'] == 'failed'):
                return bottle.HTTPResponse(json.dumps(resp), _ERRORS[errno.EBUSY],
                                       {'Content-type': 'application/json'})
        elif (res['status'] == 'running'):
            return bottle.HTTPResponse(json.dumps(res), 200,
                                       {'Content-type': 'application/json'})
        elif (res['status'] == 'failed'):
            return bottle.HTTPResponse(json.dumps(res), _ERRORS[errno.EBUSY],
                                       {'Content-type': 'application/json'})
    # end process_purge_request

    def db_purge_operation(self, purge_cutoff, purge_id):
        self._logger.info("purge_id %s START Purging!" % str(purge_id))
        purge_stat = DatabasePurgeStats()
        purge_stat.request_time = UTCTimestampUsec()
        purge_info = DatabasePurgeInfo()
        self._analytics_db.number_of_purge_requests += 1
        purge_info.number_of_purge_requests = \
            self._analytics_db.number_of_purge_requests
        total_rows_deleted, purge_stat.purge_status_details = \
            self._analytics_db.db_purge(purge_cutoff, purge_id)
        self._analytics_db.delete_db_purge_status()

        if (total_rows_deleted > 0):
            # update start times in cassandra
            start_times = {}
            start_times[SYSTEM_OBJECT_START_TIME] = purge_cutoff['other_cutoff']
            start_times[SYSTEM_OBJECT_FLOW_START_TIME] = purge_cutoff['flow_cutoff']
            start_times[SYSTEM_OBJECT_STAT_START_TIME] = purge_cutoff['stats_cutoff']
            start_times[SYSTEM_OBJECT_MSG_START_TIME] = purge_cutoff['msg_cutoff']
            self._analytics_db._update_analytics_start_time(start_times)

        end_time = UTCTimestampUsec()
        duration = end_time - purge_stat.request_time
        purge_stat.purge_id = purge_id
        if (total_rows_deleted < 0):
            purge_stat.purge_status = PurgeStatusString[PurgeStatus.FAILURE]
            self._logger.error("purge_id %s purging Failed" % str(purge_id))
        else:
            purge_stat.purge_status = PurgeStatusString[PurgeStatus.SUCCESS]
            self._logger.info("purge_id %s purging DONE" % str(purge_id))
        purge_stat.rows_deleted = total_rows_deleted
        purge_stat.duration = duration
        purge_info.name  = self._hostname
        purge_info.stats = [purge_stat]
        purge_data = DatabasePurge(data=purge_info, sandesh=self._sandesh)
        purge_data.send(sandesh=self._sandesh)
    #end db_purge_operation

    def _auto_purge(self):
        """ monitor dbusage continuously and purge the db accordingly """
        # wait for 10 minutes before starting to monitor
        gevent.sleep(10*60)

        # continuously monitor and purge
        while True:
            trigger_purge = False
            db_node_usage = self._analytics_db.get_dbusage_info(self._args.rest_api_port)
            self._logger.info("node usage:" + str(db_node_usage) )
            self._logger.info("threshold:" + str(self._args.db_purge_threshold))

            # check database disk usage on each node
            for node in db_node_usage:
                if (int(db_node_usage[node]) > int(self._args.db_purge_threshold)):
                    self._logger.error("Database usage of %d on %s exceeds threshold",
                            db_node_usage[node], node)
                    trigger_purge = True
                    break
                else:
                    self._logger.info("Database usage of %d on %s does not exceed threshold",
                            db_node_usage[node], node)

            # check if there is a purge already going on
            purge_id = str(uuid.uuid1())
            resp = self._analytics_db.get_analytics_db_purge_status(
                      self._state_server._redis_list)

            if (resp != None):
                trigger_purge = False

            if (trigger_purge):
            # trigger purge
                start_times = self._analytics_db._get_analytics_start_time()
                purge_cutoff = self.get_purge_cutoff(
                        (100.0 - float(self._args.db_purge_level)),
                        start_times)
                self._logger.info("Starting purge")
                self.db_purge_operation(purge_cutoff, purge_id)
                self._logger.info("Ending purge")

            gevent.sleep(60*30) # sleep for 30 minutes
    # end _auto_purge



    def _get_analytics_data_start_time(self):
        analytics_start_time = (self._analytics_db._get_analytics_start_time())[SYSTEM_OBJECT_START_TIME]
        response = {'analytics_data_start_time': analytics_start_time}
        return bottle.HTTPResponse(
            json.dumps(response), 200, {'Content-type': 'application/json'})
    # end _get_analytics_data_start_time

    def table_process(self, table):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        base_url = bottle.request.urlparts.scheme + '://' + \
            bottle.request.urlparts.netloc + '/analytics/table/' + table + '/'

        json_links = []
        for i in range(0, len(self._VIRTUAL_TABLES)):
            if (self._VIRTUAL_TABLES[i].name == table):
                link = LinkObject('schema', base_url + 'schema')
                json_links.append(obj_to_dict(link))
                if len(self._VIRTUAL_TABLES[i].columnvalues) > 0:
                    link = LinkObject(
                        'column-values', base_url + 'column-values')
                    json_links.append(obj_to_dict(link))
                break

        bottle.response.set_header('Content-Type', 'application/json')
        return json.dumps(json_links)
    # end table_process

    def table_schema_process(self, table):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        bottle.response.set_header('Content-Type', 'application/json')
        for i in range(0, len(self._VIRTUAL_TABLES)):
            if (self._VIRTUAL_TABLES[i].name == table):
                return json.dumps(self._VIRTUAL_TABLES[i].schema,
                                  default=lambda obj: obj.__dict__)

        return (json.dumps({}))
    # end table_schema_process

    def column_values_process(self, table):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        base_url = bottle.request.urlparts.scheme + '://' + \
            bottle.request.urlparts.netloc + \
            '/analytics/table/' + table + '/column-values/'

        bottle.response.set_header('Content-Type', 'application/json')
        json_links = []
        for i in range(0, len(self._VIRTUAL_TABLES)):
            if (self._VIRTUAL_TABLES[i].name == table):
                for col in self._VIRTUAL_TABLES[i].columnvalues:
                    link = LinkObject(col, base_url + col)
                    json_links.append(obj_to_dict(link))
                break

        return (json.dumps(json_links))
    # end column_values_process

    def generator_info(self, table, column):
        if ((column == MODULE) or (column == SOURCE)):
            sources = []
            moduleids = []
            if self.disc:
                ulist = self._uve_server._redis_uve_list
            else:
                ulist = self.redis_uve_list
            
            for redis_uve in ulist:
                redish = redis.StrictRedis(
                    db=1,
                    host=redis_uve[0],
                    port=redis_uve[1],
                    password=self._args.redis_password)
                try:
                    for key in redish.smembers("NGENERATORS"):
                        source = key.split(':')[0]
                        module = key.split(':')[2]
                        if (sources.count(source) == 0):
                            sources.append(source)
                        if (moduleids.count(module) == 0):
                            moduleids.append(module)
                except Exception as e:
                    self._logger.error('Exception: %s' % e)
            if column == MODULE:
                return moduleids
            elif column == SOURCE:
                return sources
        elif (column == 'Category'):
            return self._CATEGORY_MAP
        elif (column == 'Level'):
            return self._LEVEL_LIST
        elif (column == STAT_OBJECTID_FIELD):
            objtab = None
            for t in _STAT_TABLES:
                stat_table = STAT_VT_PREFIX + "." + \
                    t.stat_type + "." + t.stat_attr
                if (table == stat_table):
                    objtab = t.obj_table
                    break
            if (objtab != None) and (objtab != "None"): 
                return list(self._uve_server.get_uve_list(objtab))

        return []
    # end generator_info

    def column_process(self, table, column):
        (ok, result) = self._get_common(bottle.request)
        if not ok:
            (code, msg) = result
            abort(code, msg)

        bottle.response.set_header('Content-Type', 'application/json')
        for i in range(0, len(self._VIRTUAL_TABLES)):
            if (self._VIRTUAL_TABLES[i].name == table):
                if self._VIRTUAL_TABLES[i].columnvalues.count(column) > 0:
                    return (json.dumps(self.generator_info(table, column)))

        return (json.dumps([]))
    # end column_process

    def start_uve_server(self):
        self._uve_server.run()

    #end start_uve_server

    def start_webserver(self):
        pipe_start_app = bottle.app()
        try:
            bottle.run(app=pipe_start_app, host=self._args.rest_api_ip,
                   port=self._args.rest_api_port, server='gevent')
        except Exception as e:
            self._logger.error("Exception: %s" % e)
            sys.exit()
    # end start_webserver

    def cpu_info_logger(self):
        opserver_cpu_info = CpuInfoData()
        while True:
            mod_cpu_info = ModuleCpuInfo()
            mod_cpu_info.module_id = self._moduleid
            mod_cpu_info.instance_id = self._instance_id
            mod_cpu_info.cpu_info = opserver_cpu_info.get_cpu_info(
                system=False)
            mod_cpu_state = ModuleCpuState()
            mod_cpu_state.name = self._hostname

            # At some point, the following attributes will be deprecated in favor of cpu_info
            mod_cpu_state.module_cpu_info = [mod_cpu_info]
            opserver_cpu_state_trace = ModuleCpuStateTrace(
                    data=mod_cpu_state,
                    sandesh=self._sandesh
                    )
            opserver_cpu_state_trace.send(sandesh=self._sandesh)

            aly_cpu_state = AnalyticsCpuState()
            aly_cpu_state.name = self._hostname

            aly_cpu_info = ProcessCpuInfo()
            aly_cpu_info.module_id= self._moduleid
            aly_cpu_info.inst_id = self._instance_id
            aly_cpu_info.cpu_share = mod_cpu_info.cpu_info.cpu_share
            aly_cpu_info.mem_virt = mod_cpu_info.cpu_info.meminfo.virt
            aly_cpu_info.mem_res = mod_cpu_info.cpu_info.meminfo.res
            aly_cpu_state.cpu_info = [aly_cpu_info]

            aly_cpu_state_trace = AnalyticsCpuStateTrace(
                    data=aly_cpu_state,
                    sandesh=self._sandesh
                    )
            aly_cpu_state_trace.send(sandesh=self._sandesh)

            gevent.sleep(60)
    #end cpu_info_logger

    def disc_cb(self, clist):
        '''
        Analytics node may be brought up/down any time. For UVE aggregation,
        Opserver needs to know the list of all Analytics nodes (redis-uves).
        Periodically poll the Collector list [in lieu of 
        redi-uve nodes] from the discovery. 
        '''
        newlist = []
        for elem in clist:
            ipaddr = elem["ip-address"]
            cpid = 0
            if "pid" in elem:
                cpid = int(elem["pid"])
            newlist.append((ipaddr, self._args.redis_server_port, cpid))
        self._uve_server.update_redis_uve_list(newlist)
        self._state_server.update_redis_list(newlist)

    def disc_agp(self, clist):
        new_agp = {}
        for elem in clist:
            pi = PartInfo(instance_id = elem['instance-id'],
                          ip_address = elem['ip-address'],
                          acq_time = int(elem['acq-time']),
                          port = int(elem['port']))
            partno = int(elem['partition'])
            if partno not in new_agp:
                new_agp[partno] = pi
            else:
                if new_agp[partno] != pi:
                    if pi.acq_time > new_agp[partno].acq_time:
                        new_agp[partno] = pi
        if len(new_agp) == self._args.partitions and \
                len(self.agp) != self._args.partitions:
            ConnectionState.update(conn_type = ConnectionType.UVEPARTITIONS,
                name = 'UVE-Aggregation', status = ConnectionStatus.UP,
                message = 'Partitions:%d' % len(new_agp))
        # TODO: Fix kafka provisioning before setting connection state down
        if len(new_agp) != self._args.partitions:
            ConnectionState.update(conn_type = ConnectionType.UVEPARTITIONS,
                name = 'UVE-Aggregation', status = ConnectionStatus.UP,
                message = 'Partitions:%d' % len(new_agp))
        self.agp = new_agp        

    def get_agp(self):
        return self.agp

def main(args_str=' '.join(sys.argv[1:])):
    opserver = OpServer(args_str)

    opserver._uvedbcache.start()
    opserver._uvedbstream.start()

    gevs = [ 
        gevent.spawn(opserver.start_webserver),
        gevent.spawn(opserver.cpu_info_logger),
        gevent.spawn(opserver.start_uve_server)]

    if opserver.disc:
        sp = ServicePoller(opserver._logger, CollectorTrace, opserver.disc, \
                           COLLECTOR_DISCOVERY_SERVICE_NAME, opserver.disc_cb, \
                           opserver._sandesh)
        sp.start()
        gevs.append(sp)

        sp2 = ServicePoller(opserver._logger, CollectorTrace, opserver.disc, \
                            ALARM_PARTITION_SERVICE_NAME, opserver.disc_agp, \
                            opserver._sandesh)
        sp2.start()
        gevs.append(sp2)

    gevent.joinall(gevs)

    opserver._uvedbstream.kill()
    opserver._uvedbstream.join()
    opserver._uvedbcache.join()

if __name__ == '__main__':
    main()

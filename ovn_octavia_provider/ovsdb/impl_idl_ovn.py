#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import atexit
import contextlib

import netaddr
from neutron_lib import constants as n_const
from neutron_lib import exceptions as n_exc
from oslo_log import log
from ovsdbapp.backend import ovs_idl
from ovsdbapp.backend.ovs_idl import command
from ovsdbapp.backend.ovs_idl import connection
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp.backend.ovs_idl import rowview
from ovsdbapp.backend.ovs_idl import transaction as idl_trans
from ovsdbapp.schema.ovn_northbound import impl_idl as nb_impl_idl
from ovsdbapp.schema.ovn_southbound import impl_idl as sb_impl_idl
import tenacity

from ovn_octavia_provider.common import config
from ovn_octavia_provider.common import exceptions as ovn_exc
from ovn_octavia_provider.common import utils
from ovn_octavia_provider.i18n import _
from ovn_octavia_provider.ovsdb import ovsdb_monitor


LOG = log.getLogger(__name__)


class OvnNbTransaction(idl_trans.Transaction):

    def __init__(self, *args, **kwargs):
        # NOTE(lucasagomes): The bump_nb_cfg parameter is only used by
        # the agents health status check
        self.bump_nb_cfg = kwargs.pop('bump_nb_cfg', False)
        super().__init__(*args, **kwargs)

    def pre_commit(self, txn):
        if not self.bump_nb_cfg:
            return
        self.api.nb_global.increment('nb_cfg')


class Backend(ovs_idl.Backend):

    def is_table_present(self, table_name):
        return table_name in self._tables

    def is_col_present(self, table_name, col_name):
        return self.is_table_present(table_name) and (
            col_name in self._tables[table_name].columns)

    # Check for a column match in the table. If not found do a retry with
    # a stop delay of 10 secs. This function would be useful if the caller
    # wants to verify for the presence of a particular row in the table
    # with the column match before doing any transaction.
    # Eg. We can check if Logical_Switch row is present before adding a
    # logical switch port to it.
    @tenacity.retry(retry=tenacity.retry_if_exception_type(RuntimeError),
                    wait=tenacity.wait_exponential(),
                    stop=tenacity.stop_after_delay(10),
                    reraise=True)
    def check_for_row_by_value_and_retry(self, table, column, match):
        try:
            idlutils.row_by_value(self.idl, table, column, match)
        except idlutils.RowNotFound as e:
            msg = (_("%(match)s does not exist in %(column)s of %(table)s")
                   % {'match': match, 'column': column, 'table': table})
            raise RuntimeError(msg) from e


class OvsdbConnectionUnavailable(n_exc.ServiceUnavailable):
    message = _("OVS database connection to %(db_schema)s failed with error: "
                "'%(error)s'. Verify that the OVS and OVN services are "
                "available and that the 'ovn_nb_connection' and "
                "'ovn_sb_connection' configuration options are correct.")


class FindLbInTableCommand(command.ReadOnlyCommand):
    def __init__(self, api, lb, table):
        super().__init__(api)
        self.lb = lb
        self.table = table

    def run_idl(self, txn):
        self.result = [
            rowview.RowView(item) for item in
            self.api.tables[self.table].rows.values()
            if self.lb in item.load_balancer]


class GetLrsCommand(command.ReadOnlyCommand):
    def run_idl(self, txn):
        self.result = [
            rowview.RowView(item) for item in
            self.api.tables['Logical_Router'].rows.values()]


# NOTE(froyo): remove this class once ovsdbapp manages the IPv6 into [ ]
# https://bugs.launchpad.net/ovsdbapp/+bug/2057471
class DelBackendFromIPPortMapping(command.BaseCommand):
    table = 'Load_Balancer'

    def __init__(self, api, lb, backend_ip):
        super().__init__(api)
        self.lb = lb
        if netaddr.IPNetwork(backend_ip).version == n_const.IP_VERSION_6:
            self.backend_ip = f'[{backend_ip}]'
        else:
            self.backend_ip = backend_ip

    def run_idl(self, txn):
        try:
            ovn_lb = self.api.lookup(self.table, self.lb)
            ovn_lb.delkey('ip_port_mappings', self.backend_ip)
        except Exception:
            LOG.exception("Error deleting backend %s from ip_port_mappings "
                          "for LB uuid %s", str(self.backend_ip), str(self.lb))


# NOTE(froyo): remove this class once ovsdbapp manages the IPv6 into [ ]
# https://bugs.launchpad.net/ovsdbapp/+bug/2057471
class AddBackendToIPPortMapping(command.BaseCommand):
    table = 'Load_Balancer'

    def __init__(self, api, lb, backend_ip, port_name, src_ip):
        super().__init__(api)
        self.lb = lb
        self.backend_ip = backend_ip
        self.port_name = port_name
        self.src_ip = src_ip
        if netaddr.IPNetwork(backend_ip).version == n_const.IP_VERSION_6:
            self.backend_ip = f'[{backend_ip}]'
            self.src_ip = f'[{src_ip}]'

    def run_idl(self, txn):
        try:
            lb = self.api.lookup(self.table, self.lb)
            lb.setkey('ip_port_mappings', self.backend_ip,
                      '%s:%s' % (self.port_name, self.src_ip))
        except Exception:
            LOG.exception("Error adding backend %s to ip_port_mappings "
                          "for LB uuid %s", str(self.backend_ip), str(self.lb))


class OvsdbNbOvnIdl(nb_impl_idl.OvnNbApiIdlImpl, Backend):
    def __init__(self, connection):
        super().__init__(connection)
        self.idl._session.reconnect.set_probe_interval(
            config.get_ovn_ovsdb_probe_interval())

    @property
    def nb_global(self):
        return next(iter(self.tables['NB_Global'].rows.values()))

    def create_transaction(self, check_error=False, log_errors=True,
                           bump_nb_cfg=False):
        return OvnNbTransaction(
            self, self.ovsdb_connection, self.ovsdb_connection.timeout,
            check_error, log_errors, bump_nb_cfg=bump_nb_cfg)

    @contextlib.contextmanager
    def transaction(self, *args, **kwargs):
        """A wrapper on the ovsdbapp transaction to work with revisions.

        This method is just a wrapper around the ovsdbapp transaction
        to handle revision conflicts correctly.
        """
        try:
            with super().transaction(*args, **kwargs) as t:
                yield t
        except ovn_exc.RevisionConflict as e:
            LOG.info('Transaction aborted. Reason: %s', e)

    def find_lb_in_table(self, lb, table):
        return FindLbInTableCommand(self, lb, table)

    def get_lrs(self):
        return GetLrsCommand(self)

    # NOTE(froyo): remove this method once ovsdbapp manages the IPv6 into [ ]
    def lb_del_ip_port_mapping(self, lb_uuid, backend_ip):
        return DelBackendFromIPPortMapping(self, lb_uuid, backend_ip)

    # NOTE(froyo): remove this method once ovsdbapp manages the IPv6 into [ ]
    def lb_add_ip_port_mapping(self, lb_uuid, backend_ip, port_name, src_ip):
        return AddBackendToIPPortMapping(self, lb_uuid, backend_ip, port_name,
                                         src_ip)


class OvsdbSbOvnIdl(sb_impl_idl.OvnSbApiIdlImpl, Backend):
    def __init__(self, connection):
        super().__init__(connection)
        self.idl._session.reconnect.set_probe_interval(
            config.get_ovn_ovsdb_probe_interval())


class OvnNbIdlForLb(ovsdb_monitor.OvnIdl):
    SCHEMA = "OVN_Northbound"
    TABLES = ('Logical_Switch', 'Load_Balancer', 'Load_Balancer_Health_Check',
              'Logical_Router', 'Logical_Switch_Port', 'Logical_Router_Port',
              'Gateway_Chassis', 'NAT', 'HA_Chassis_Group')

    def __init__(self, event_lock_name=None, notifier=True):
        self.conn_string = config.get_ovn_nb_connection()
        ovsdb_monitor.check_and_set_ssl_files(self.SCHEMA)
        helper = self._get_ovsdb_helper(self.conn_string)
        for table in OvnNbIdlForLb.TABLES:
            helper.register_table(table)
        super().__init__(
            driver=None, remote=self.conn_string, schema=helper,
            notifier=notifier)
        self.event_lock_name = event_lock_name
        if self.event_lock_name:
            self.set_lock(self.event_lock_name)

    @utils.retry()
    def _get_ovsdb_helper(self, connection_string):
        return idlutils.get_schema_helper(connection_string, self.SCHEMA)


class OvnSbIdlForLb(ovsdb_monitor.OvnIdl):
    SCHEMA = "OVN_Southbound"
    TABLES = ('Load_Balancer', 'Service_Monitor')

    def __init__(self, event_lock_name=None):
        self.conn_string = config.get_ovn_sb_connection()
        ovsdb_monitor.check_and_set_ssl_files(self.SCHEMA)
        helper = self._get_ovsdb_helper(self.conn_string)
        for table in OvnSbIdlForLb.TABLES:
            helper.register_table(table)
        super().__init__(
            driver=None, remote=self.conn_string, schema=helper)
        self.event_lock_name = event_lock_name
        if self.event_lock_name:
            self.set_lock(self.event_lock_name)
        atexit.register(self.stop)

    @utils.retry()
    def _get_ovsdb_helper(self, connection_string):
        return idlutils.get_schema_helper(connection_string, self.SCHEMA)

    def start(self):
        self.conn = connection.Connection(
            self, timeout=config.get_ovn_ovsdb_timeout())
        return OvsdbSbOvnIdl(self.conn)

    def stop(self):
        # Close the running connection if it has been initalized
        if hasattr(self, 'conn'):
            if not self.conn.stop(timeout=config.get_ovn_ovsdb_timeout()):
                LOG.debug("Connection terminated to OvnSb "
                          "but a thread is still alive")
            del self.conn
        # complete the shutdown for the event handler
        self.notify_handler.shutdown()
        # Close the idl session
        self.close()

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

import sys

from oslo_config import cfg
from oslo_log import log as logging
from ovn_octavia_provider.common import config as ovn_conf
from ovn_octavia_provider import driver

CONF = cfg.CONF
LOG = logging.getLogger(__name__)


def setup_conf():
    conf = cfg.CONF
    ovn_conf.register_opts()
    logging.register_options(CONF)

    try:
        CONF(project='octavia')
    except TypeError:
        LOG.error('Error parsing the configuration values. Please verify.')
        raise
    return conf

def main():
    """Main method for syncing Octavia LBs (OVN provider) with OVN NB DB."""
    setup_conf()
    logging.setup(CONF, 'octavia_ovn_db_sync_util')

    print(">>> Starting OVN Octavia DB sync...")

    args = sys.argv[1:]
    lb_filters = {'provider': 'ovn'}
    if '--debug' in args:
        cfg.CONF.set_override('debug', True)
        args.remove('--debug')
    else:
        cfg.CONF.set_override('debug', False)

    try:
        ovn_driver = driver.OvnProviderDriver()
        print(">>> Calling do_sync() on OvnProviderDriver...")
        ovn_driver.do_sync(**lb_filters)
        print(">>> Sync process complete.")
    except Exception as e:
        print(f">>> ERROR during sync: {e}")
        raise

    print(">>> Finished OVN Octavia DB sync.")

if __name__ == "__main__":
    main()

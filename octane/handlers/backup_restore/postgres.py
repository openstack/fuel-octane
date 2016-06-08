# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import json
import os
import shutil
import six

from fuelclient.objects import node

from octane.handlers.backup_restore import base
from octane import magic_consts
from octane.util import docker
from octane.util import fuel_client
from octane.util import helpers
from octane.util import sql
from octane.util import subprocess


class PostgresArchivatorMeta(type):

    def __init__(cls, name, bases, attr):
        super(PostgresArchivatorMeta, cls).__init__(name, bases, attr)
        cls.container = "postgres"
        if cls.db is not None and cls.cmd is None:
            cls.cmd = ["sudo", "-u", "postgres", "pg_dump", "-C", cls.db]
        if cls.db is not None and cls.filename is None:
            cls.filename = "postgres/{0}.sql".format(cls.db)


@six.add_metaclass(PostgresArchivatorMeta)
class PostgresArchivator(base.CmdArchivator):
    db = None

    def restore(self):
        dump = self.archive.extractfile(self.filename)
        subprocess.call([
            "systemctl", "stop", "docker-{0}.service".format(self.db)
        ])
        docker.stop_container(self.db)
        docker.run_in_container(
            "postgres",
            ["sudo", "-u", "postgres", "dropdb", "--if-exists", self.db],
        )
        with docker.in_container("postgres",
                                 ["sudo", "-u", "postgres", "psql"],
                                 stdin=subprocess.PIPE) as process:
            shutil.copyfileobj(dump, process.stdin)
        docker.start_container(self.db)
        docker.wait_for_container(self.db)
        subprocess.call([
            "systemctl", "start", "docker-{0}.service".format(self.db)
        ])


class NailgunArchivator(PostgresArchivator):
    db = "nailgun"

    def restore(self):
        for args in magic_consts.NAILGUN_ARCHIVATOR_PATCHES:
            docker.apply_patches(*args)
        try:
            super(NailgunArchivator, self).restore()
            self._repair_database_consistency()
            self._create_links_on_remote_logs()
        finally:
            for args in magic_consts.NAILGUN_ARCHIVATOR_PATCHES:
                docker.apply_patches(*args, revert=True)

    def _create_links_on_remote_logs(self):
        domain = helpers.get_astute_dict()["DNS_DOMAIN"]
        dirname = "/var/log/docker-logs/remote/"
        with fuel_client.set_auth_context(self.context):
            pairs = [(n.data["meta"]["system"]["fqdn"], n.data["ip"])
                     for n in node.Node.get_all()]
        docker.run_in_container("rsyslog", ["service", "rsyslog", "stop"])
        try:
            for fqdn, ip_addr in pairs:
                if not fqdn.endswith(domain):
                    continue
                ip_addr_path = os.path.join(dirname, ip_addr)
                fqdn_path = os.path.join(dirname, fqdn)
                if os.path.islink(ip_addr_path):
                    continue
                if os.path.isdir(ip_addr_path):
                    os.rename(ip_addr_path, fqdn_path)
                else:
                    os.mkdir(fqdn_path)
                os.symlink(fqdn, ip_addr_path)
        finally:
            docker.run_in_container("rsyslog", ["service", "rsyslog", "start"])

    def _repair_database_consistency(self):
        values = []
        for line in sql.run_psql_in_container(
                "select id, generated from attributes;", self.db):
            c_id, c_data = line.split("|", 1)
            data = json.loads(c_data)
            data["deployed_before"] = {"value": True}
            values.append("({0}, '{1}')".format(c_id, json.dumps(data)))

        if values:
            sql.run_psql_in_container(
                'update attributes as a set generated = b.generated '
                'from (values {0}) as b(id, generated) '
                'where a.id = b.id;'.format(','.join(values)),
                self.db)


class KeystoneArchivator(PostgresArchivator):
    db = "keystone"

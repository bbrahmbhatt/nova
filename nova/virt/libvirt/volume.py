# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2011 OpenStack LLC.
# All Rights Reserved.
#
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

"""Volume drivers for libvirt."""

import hashlib
import os
import time

from nova import exception
from nova.openstack.common import cfg
from nova.openstack.common import lockutils
from nova.openstack.common import log as logging
from nova import paths
from nova import utils
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import utils as virtutils

LOG = logging.getLogger(__name__)

volume_opts = [
    cfg.IntOpt('num_iscsi_scan_tries',
               default=3,
               help='number of times to rescan iSCSI target to find volume'),
    cfg.StrOpt('rbd_user',
               default=None,
               help='the RADOS client name for accessing rbd volumes'),
    cfg.StrOpt('rbd_secret_uuid',
               default=None,
               help='the libvirt uuid of the secret for the rbd_user'
                    'volumes'),
    cfg.StrOpt('nfs_mount_point_base',
               default=paths.state_path_def('mnt'),
               help='Dir where the nfs volume is mounted on the compute node'),
    cfg.StrOpt('nfs_mount_options',
               default=None,
               help='Mount options passed to the nfs client. See section '
                    'of the nfs man page for details'),
    ]

CONF = cfg.CONF
CONF.register_opts(volume_opts)


class LibvirtBaseVolumeDriver(object):
    """Base class for volume drivers."""
    def __init__(self, connection, is_block_dev):
        self.connection = connection
        self.is_block_dev = is_block_dev

    def connect_volume(self, connection_info, mount_device):
        """Connect the volume. Returns xml for libvirt."""

        conf = vconfig.LibvirtConfigGuestDisk()
        conf.driver_name = virtutils.pick_disk_driver_name(self.is_block_dev)
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.target_dev = mount_device
        conf.target_bus = "virtio"
        conf.serial = connection_info.get('serial')
        return conf

    def disconnect_volume(self, connection_info, mount_device):
        """Disconnect the volume."""
        pass


class LibvirtVolumeDriver(LibvirtBaseVolumeDriver):
    """Class for volumes backed by local file."""
    def __init__(self, connection):
        super(LibvirtVolumeDriver,
              self).__init__(connection, is_block_dev=True)

    def connect_volume(self, connection_info, mount_device):
        """Connect the volume to a local device."""
        conf = super(LibvirtVolumeDriver,
                     self).connect_volume(connection_info, mount_device)
        conf.source_type = "block"
        conf.source_path = connection_info['data']['device_path']
        return conf


class LibvirtFakeVolumeDriver(LibvirtBaseVolumeDriver):
    """Driver to attach fake volumes to libvirt."""
    def __init__(self, connection):
        super(LibvirtFakeVolumeDriver,
              self).__init__(connection, is_block_dev=True)

    def connect_volume(self, connection_info, mount_device):
        """Connect the volume to a fake device."""
        conf = super(LibvirtFakeVolumeDriver,
                     self).connect_volume(connection_info, mount_device)
        conf.source_type = "network"
        conf.source_protocol = "fake"
        conf.source_host = "fake"
        return conf


class LibvirtNetVolumeDriver(LibvirtBaseVolumeDriver):
    """Driver to attach Network volumes to libvirt."""
    def __init__(self, connection):
        super(LibvirtNetVolumeDriver,
              self).__init__(connection, is_block_dev=False)

    def connect_volume(self, connection_info, mount_device):
        conf = super(LibvirtNetVolumeDriver,
                     self).connect_volume(connection_info, mount_device)
        conf.source_type = "network"
        conf.source_protocol = connection_info['driver_volume_type']
        conf.source_host = connection_info['data']['name']
        netdisk_properties = connection_info['data']
        auth_enabled = netdisk_properties.get('auth_enabled')
        if (conf.source_protocol == 'rbd' and
            CONF.rbd_secret_uuid):
            conf.auth_secret_uuid = CONF.rbd_secret_uuid
            auth_enabled = True  # Force authentication locally
            if CONF.rbd_user:
                conf.auth_username = CONF.rbd_user
        if auth_enabled:
            conf.auth_username = (conf.auth_username or
                                  netdisk_properties['auth_username'])
            conf.auth_secret_type = netdisk_properties['secret_type']
            conf.auth_secret_uuid = (conf.auth_secret_uuid or
                                     netdisk_properties['secret_uuid'])
        return conf


class LibvirtISCSIVolumeDriver(LibvirtBaseVolumeDriver):
    """Driver to attach Network volumes to libvirt."""
    def __init__(self, connection):
        super(LibvirtISCSIVolumeDriver,
              self).__init__(connection, is_block_dev=False)

    def _run_iscsiadm(self, iscsi_properties, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = utils.execute('iscsiadm', '-m', 'node', '-T',
                                   iscsi_properties['target_iqn'],
                                   '-p', iscsi_properties['target_portal'],
                                   *iscsi_command, run_as_root=True,
                                   check_exit_code=check_exit_code)
        LOG.debug("iscsiadm %s: stdout=%s stderr=%s" %
                  (iscsi_command, out, err))
        return (out, err)

    def _iscsiadm_update(self, iscsi_properties, property_key, property_value,
                         **kwargs):
        iscsi_command = ('--op', 'update', '-n', property_key,
                         '-v', property_value)
        return self._run_iscsiadm(iscsi_properties, iscsi_command, **kwargs)

    @lockutils.synchronized('connect_volume', 'nova-')
    def connect_volume(self, connection_info, mount_device):
        """Attach the volume to instance_name."""
        conf = super(LibvirtISCSIVolumeDriver,
                     self).connect_volume(connection_info, mount_device)

        iscsi_properties = connection_info['data']
        # NOTE(vish): If we are on the same host as nova volume, the
        #             discovery makes the target so we don't need to
        #             run --op new. Therefore, we check to see if the
        #             target exists, and if we get 255 (Not Found), then
        #             we run --op new. This will also happen if another
        #             volume is using the same target.
        try:
            self._run_iscsiadm(iscsi_properties, ())
        except exception.ProcessExecutionError as exc:
            # iscsiadm returns 21 for "No records found" after version 2.0-871
            if exc.exit_code in [21, 255]:
                self._run_iscsiadm(iscsi_properties, ('--op', 'new'))
            else:
                raise

        if iscsi_properties.get('auth_method'):
            self._iscsiadm_update(iscsi_properties,
                                  "node.session.auth.authmethod",
                                  iscsi_properties['auth_method'])
            self._iscsiadm_update(iscsi_properties,
                                  "node.session.auth.username",
                                  iscsi_properties['auth_username'])
            self._iscsiadm_update(iscsi_properties,
                                  "node.session.auth.password",
                                  iscsi_properties['auth_password'])

        # NOTE(vish): If we have another lun on the same target, we may
        #             have a duplicate login
        self._run_iscsiadm(iscsi_properties, ("--login",),
                           check_exit_code=[0, 255])

        self._iscsiadm_update(iscsi_properties, "node.startup", "automatic")

        host_device = ("/dev/disk/by-path/ip-%s-iscsi-%s-lun-%s" %
                        (iscsi_properties['target_portal'],
                         iscsi_properties['target_iqn'],
                         iscsi_properties.get('target_lun', 0)))

        # The /dev/disk/by-path/... node is not always present immediately
        # TODO(justinsb): This retry-with-delay is a pattern, move to utils?
        tries = 0
        while not os.path.exists(host_device):
            if tries >= CONF.num_iscsi_scan_tries:
                raise exception.NovaException(_("iSCSI device not found at %s")
                                              % (host_device))

            LOG.warn(_("ISCSI volume not yet found at: %(mount_device)s. "
                       "Will rescan & retry.  Try number: %(tries)s") %
                     locals())

            # The rescan isn't documented as being necessary(?), but it helps
            self._run_iscsiadm(iscsi_properties, ("--rescan",))

            tries = tries + 1
            if not os.path.exists(host_device):
                time.sleep(tries ** 2)

        if tries != 0:
            LOG.debug(_("Found iSCSI node %(mount_device)s "
                        "(after %(tries)s rescans)") %
                      locals())

        conf.source_type = "block"
        conf.source_path = host_device
        return conf

    @lockutils.synchronized('connect_volume', 'nova-')
    def disconnect_volume(self, connection_info, mount_device):
        """Detach the volume from instance_name."""
        super(LibvirtISCSIVolumeDriver,
              self).disconnect_volume(connection_info, mount_device)
        iscsi_properties = connection_info['data']
        # NOTE(vish): Only disconnect from the target if no luns from the
        #             target are in use.
        device_prefix = ("/dev/disk/by-path/ip-%s-iscsi-%s-lun-" %
                         (iscsi_properties['target_portal'],
                          iscsi_properties['target_iqn']))
        devices = self.connection.get_all_block_devices()
        devices = [dev for dev in devices if dev.startswith(device_prefix)]
        if not devices:
            self._iscsiadm_update(iscsi_properties, "node.startup", "manual",
                                  check_exit_code=[0, 21, 255])
            self._run_iscsiadm(iscsi_properties, ("--logout",),
                               check_exit_code=[0, 21, 255])
            self._run_iscsiadm(iscsi_properties, ('--op', 'delete'),
                               check_exit_code=[0, 21, 255])


class LibvirtNFSVolumeDriver(LibvirtBaseVolumeDriver):
    """Class implements libvirt part of volume driver for NFS."""

    def __init__(self, connection):
        """Create back-end to nfs."""
        super(LibvirtNFSVolumeDriver,
              self).__init__(connection, is_block_dev=False)

    def connect_volume(self, connection_info, mount_device):
        """Connect the volume. Returns xml for libvirt."""
        conf = super(LibvirtNFSVolumeDriver,
                     self).connect_volume(connection_info, mount_device)
        path = self._ensure_mounted(connection_info['data']['export'])
        path = os.path.join(path, connection_info['data']['name'])
        conf.source_type = 'file'
        conf.source_path = path
        return conf

    def _ensure_mounted(self, nfs_export):
        """
        @type nfs_export: string
        """
        mount_path = os.path.join(CONF.nfs_mount_point_base,
                                  self.get_hash_str(nfs_export))
        self._mount_nfs(mount_path, nfs_export, ensure=True)
        return mount_path

    def _mount_nfs(self, mount_path, nfs_share, ensure=False):
        """Mount nfs export to mount path."""
        if not self._path_exists(mount_path):
            utils.execute('mkdir', '-p', mount_path)

        # Construct the NFS mount command.
        nfs_cmd = ['mount', '-t', 'nfs']
        if CONF.nfs_mount_options is not None:
            nfs_cmd.extend(['-o', CONF.nfs_mount_options])
        nfs_cmd.extend([nfs_share, mount_path])

        try:
            utils.execute(*nfs_cmd, run_as_root=True)
        except exception.ProcessExecutionError as exc:
            if ensure and 'already mounted' in exc.message:
                LOG.warn(_("%s is already mounted"), nfs_share)
            else:
                raise

    @staticmethod
    def get_hash_str(base_str):
        """returns string that represents hash of base_str (in hex format)."""
        return hashlib.md5(base_str).hexdigest()

    @staticmethod
    def _path_exists(path):
        """Check path."""
        try:
            return utils.execute('stat', path, run_as_root=True)
        except exception.ProcessExecutionError:
            return False
